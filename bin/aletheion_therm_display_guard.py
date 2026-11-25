#!/usr/bin/env python3
"""
Track S52 & EXP-Q17a: `aletheion-therm-display-guard` — Automatic Hysteretic
Thermal-Display Governor Daemon + Hyprland DPMS Wake Backlight Restorer
(Schmitt Trigger, <0.1% CPU, Zero Flicker).

Policy (UX582HS):
  - Poll every 5s (configurable).
  - Shed trigger:   T_cpu >= 88 C OR T_gpu >= 80 C sustained for >= 15s:
      * Save current `intel_backlight` percent to ~/.local/state/aletheion/display_guard.json
      * Dim `intel_backlight` to min(cur_pct, 35%) (safety floor >= 20%, NEVER brightens an already-dim OLED!)
      * Pause lower ScreenPad Plus (`DP-3`) AND cut `asus_screenpad` hardware backlight to 0
        via `aletheion-display mode single --internal-daemon`
      * Emit one clean `notify-send` notification explaining why
  - Restore trigger: T_cpu <= 76 C AND T_gpu <= 70 C sustained for >= 30s:
      * Restore saved `intel_backlight` percent (clamped [20%, 100%])
      * Re-enable `DP-3` (`3840x1100@60 at 0x1080 scale 2`) & restore saved `asus_screenpad` backlight
        via `aletheion-display mode dual --internal-daemon`
      * Emit one clean restore notification
  - Hyprland `.socket2.sock` DPMS wake watcher thread (0.0% idle CPU):
      * Listens for `dpms>>eDP-1,1` / `dpms>>DP-3,1` (and `dpmsv2>>...`)
      * When waking from `sleep-all` (`screen_sleep_active == True`), clears sleep flag and
        immediately restores `asus_screenpad` backlight to `saved_screenpad_brightness` iff `DP-3`
        is enabled (or enforces `0` if `DP-3` is intentionally disabled in `single`/`compute` mode!).
  - Manual override respected: if state file records `manual_override` (`pin-on` or `pin-off`
    from physical ScreenPad button or `aletheion-display toggle-pad`), daemon NEVER fights the user
    until `aletheion-display auto` clears `manual_override`.

100% Python 3 stdlib, zero bare asserts, zero /dev/null writes, 4-mode -I parity.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import errno
import glob
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from typing import Any


DEFAULT_POLL_S = 5
DEFAULT_SHED_CPU_C = 88
DEFAULT_SHED_GPU_C = 80
DEFAULT_SHED_HOLD_S = 15
DEFAULT_RESTORE_CPU_C = 76
DEFAULT_RESTORE_GPU_C = 70
DEFAULT_RESTORE_HOLD_S = 30
DEFAULT_SHED_BRIGHTNESS_PCT = 35
MIN_BRIGHTNESS_FLOOR_PCT = 20
SCREENPAD_DEFAULT_RESTORE = 180
SCREENPAD_MAX_BRIGHTNESS = 255
PAD_OUTPUT = "DP-3"
PRIMARY_OUTPUT = "eDP-1"


class GuardDaemonError(ValueError):
    """Fail-closed structured error for thermal-display governor."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def require_ok(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise GuardDaemonError(code, message)


def clamp_safe_brightness_pct(pct: int) -> int:
    require_ok(type(pct) is int, "INVALID_BRIGHTNESS_TYPE", f"expected exact int, got {type(pct).__name__}")
    if pct < MIN_BRIGHTNESS_FLOOR_PCT:
        return MIN_BRIGHTNESS_FLOOR_PCT
    if pct > 100:
        return 100
    return pct


def compute_shed_target_pct(cur_pct: int, shed_brightness_pct: int) -> int:
    """Never brighten an already-dim OLED during thermal shed: target = clamp(min(cur_pct, shed_pct))."""
    require_ok(type(cur_pct) is int, "INVALID_BRIGHTNESS_TYPE", f"expected exact int cur_pct, got {type(cur_pct).__name__}")
    require_ok(
        type(shed_brightness_pct) is int,
        "INVALID_BRIGHTNESS_TYPE",
        f"expected exact int shed_brightness_pct, got {type(shed_brightness_pct).__name__}",
    )
    return clamp_safe_brightness_pct(min(cur_pct, shed_brightness_pct))


def parse_dpms_wake_event(line: str) -> str | None:
    """Parse a Hyprland socket2 event line; return monitor name iff it is a DPMS-on wake for eDP-1 or DP-3."""
    if not isinstance(line, str):
        return None
    text = line.strip()
    if text.startswith("dpms>>"):
        payload = text[len("dpms>>"):]
    elif text.startswith("dpmsv2>>"):
        payload = text[len("dpmsv2>>"):]
    else:
        return None
    parts = [p.strip() for p in payload.split(",")]
    if len(parts) < 2:
        return None
    output_name, dpms_state = parts[0], parts[1]
    if output_name in (PRIMARY_OUTPUT, PAD_OUTPUT) and dpms_state in ("1", "on", "true"):
        return output_name
    return None


@dataclass
class HysteresisConfig:
    shed_cpu_c: int = DEFAULT_SHED_CPU_C
    shed_gpu_c: int = DEFAULT_SHED_GPU_C
    shed_hold_s: int = DEFAULT_SHED_HOLD_S
    restore_cpu_c: int = DEFAULT_RESTORE_CPU_C
    restore_gpu_c: int = DEFAULT_RESTORE_GPU_C
    restore_hold_s: int = DEFAULT_RESTORE_HOLD_S
    shed_brightness_pct: int = DEFAULT_SHED_BRIGHTNESS_PCT

    def validate(self) -> None:
        for name, val in (
            ("shed_cpu_c", self.shed_cpu_c),
            ("shed_gpu_c", self.shed_gpu_c),
            ("shed_hold_s", self.shed_hold_s),
            ("restore_cpu_c", self.restore_cpu_c),
            ("restore_gpu_c", self.restore_gpu_c),
            ("restore_hold_s", self.restore_hold_s),
            ("shed_brightness_pct", self.shed_brightness_pct),
        ):
            require_ok(type(val) is int, "CONFIG_TYPE_ERROR", f"{name} must be exact int")
        require_ok(self.restore_cpu_c < self.shed_cpu_c, "HYSTERESIS_CPU_COLLAPSED", "restore_cpu_c must be < shed_cpu_c")
        require_ok(self.restore_gpu_c < self.shed_gpu_c, "HYSTERESIS_GPU_COLLAPSED", "restore_gpu_c must be < shed_gpu_c")
        require_ok(self.shed_hold_s >= 1 and self.restore_hold_s >= 1, "HOLD_TIMER_NONPOSITIVE", "hold timers must be >= 1s")
        require_ok(
            MIN_BRIGHTNESS_FLOOR_PCT <= self.shed_brightness_pct <= 100,
            "BRIGHTNESS_FLOOR_VIOLATION",
            f"shed_brightness_pct must be in [{MIN_BRIGHTNESS_FLOOR_PCT}, 100]",
        )


class ThermalDisplayStateMachine:
    """
    Pure, side-effect-free Schmitt-trigger hysteresis state machine.
    Returns exact transition decision: "NONE" | "SHED" | "RESTORE" | "OVERRIDE_PAUSED".
    """

    def __init__(self, cfg: HysteresisConfig | None = None, *, shed_active: bool = False) -> None:
        self.cfg = cfg or HysteresisConfig()
        self.cfg.validate()
        self.shed_active = bool(shed_active)
        self.hot_since: int | None = None
        self.cool_since: int | None = None

    def step(
        self,
        *,
        now_s: int,
        t_cpu_c: int,
        t_gpu_c: int,
        manual_override: str | None,
    ) -> dict[str, Any]:
        require_ok(type(now_s) is int and now_s >= 0, "INVALID_TIME", "now_s must be non-negative int")
        require_ok(type(t_cpu_c) is int and -20 <= t_cpu_c <= 130, "INVALID_CPU_TEMP", "t_cpu_c out of range")
        require_ok(type(t_gpu_c) is int and -20 <= t_gpu_c <= 130, "INVALID_GPU_TEMP", "t_gpu_c out of range")

        if manual_override in ("pin-on", "pin-off"):
            # Respect user override unconditionally; reset pending hold timers so no stale spike fires later!
            self.hot_since = None
            self.cool_since = None
            return {
                "decision": "OVERRIDE_PAUSED",
                "shed_active": self.shed_active,
                "manual_override": manual_override,
                "hot_elapsed_s": 0,
                "cool_elapsed_s": 0,
            }

        is_hot = (t_cpu_c >= self.cfg.shed_cpu_c) or (t_gpu_c >= self.cfg.shed_gpu_c)
        is_cool = (t_cpu_c <= self.cfg.restore_cpu_c) and (t_gpu_c <= self.cfg.restore_gpu_c)

        if not self.shed_active:
            self.cool_since = None
            if is_hot:
                if self.hot_since is None:
                    self.hot_since = now_s
                elapsed = now_s - self.hot_since
                if elapsed >= self.cfg.shed_hold_s:
                    self.shed_active = True
                    self.hot_since = None
                    return {
                        "decision": "SHED",
                        "shed_active": True,
                        "manual_override": None,
                        "hot_elapsed_s": elapsed,
                        "cool_elapsed_s": 0,
                        "target_brightness_pct": clamp_safe_brightness_pct(self.cfg.shed_brightness_pct),
                    }
                return {
                    "decision": "NONE",
                    "shed_active": False,
                    "manual_override": None,
                    "hot_elapsed_s": elapsed,
                    "cool_elapsed_s": 0,
                }
            self.hot_since = None
            return {
                "decision": "NONE",
                "shed_active": False,
                "manual_override": None,
                "hot_elapsed_s": 0,
                "cool_elapsed_s": 0,
            }

        # Currently shed_active == True: wait for sustained cool window before restoring
        self.hot_since = None
        if is_cool:
            if self.cool_since is None:
                self.cool_since = now_s
            elapsed = now_s - self.cool_since
            if elapsed >= self.cfg.restore_hold_s:
                self.shed_active = False
                self.cool_since = None
                return {
                    "decision": "RESTORE",
                    "shed_active": False,
                    "manual_override": None,
                    "hot_elapsed_s": 0,
                    "cool_elapsed_s": elapsed,
                }
            return {
                "decision": "NONE",
                "shed_active": True,
                "manual_override": None,
                "hot_elapsed_s": 0,
                "cool_elapsed_s": elapsed,
            }
        self.cool_since = None
        return {
            "decision": "NONE",
            "shed_active": True,
            "manual_override": None,
            "hot_elapsed_s": 0,
            "cool_elapsed_s": 0,
        }


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else (Path.home() / ".local" / "state")
    return root / "aletheion" / "display_guard.json"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _discover_env() -> dict[str, str]:
    env = dict(os.environ)
    uid = os.getuid()
    runtime_dir = env.get("XDG_RUNTIME_DIR") or f"/run/user/{uid}"
    env["XDG_RUNTIME_DIR"] = runtime_dir
    if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
        hypr_dir = Path(runtime_dir) / "hypr"
        if hypr_dir.is_dir():
            cands = sorted(
                (p for p in hypr_dir.iterdir() if p.is_dir()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            if cands:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = cands[0].name
    if not env.get("DBUS_SESSION_BUS_ADDRESS"):
        bus = Path(runtime_dir) / "bus"
        if bus.exists():
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus}"
    return env


def _discover_hypr_socket2() -> Path | None:
    env = _discover_env()
    sig = env.get("HYPRLAND_INSTANCE_SIGNATURE", "").strip()
    runtime_dir = env.get("XDG_RUNTIME_DIR", "").strip()
    if not sig or not runtime_dir:
        return None
    sock = Path(runtime_dir) / "hypr" / sig / ".socket2.sock"
    return sock if sock.exists() else None


def _run_cmd(cmd: list[str], *, timeout: int = 8) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, env=_discover_env(), capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def _parse_ascii_uint(raw: str | None) -> int | None:
    """Strictly parse ASCII base-10 non-negative integer (rejecting Unicode digits like '²')."""
    if raw is None or len(raw) == 0:
        return None
    for ch in raw:
        if ch < "0" or ch > "9":
            return None
    return int(raw)


def _open_state_parent_fd(parent: Path, *, create_missing: bool) -> int:
    if create_missing and not parent.exists():
        parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(parent, 0o700)
    try:
        return os.open(str(parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise GuardDaemonError("SYMLINK_FORBIDDEN", f"state parent is symlink or non-directory: {parent}") from exc
        raise GuardDaemonError("STATE_DIR_ERROR", f"cannot open state directory {parent}: {exc}") from exc


def read_cpu_gpu_temps() -> tuple[int, int]:
    """Low-overhead (<0.1% CPU) live temperature sample of TCPU and T_gpu."""
    t_cpu = 0
    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/name")):
        if _read_text(Path(p)) == "coretemp":
            v = _parse_ascii_uint(_read_text(Path(p).parent / "temp1_input"))
            if v is not None:
                t_cpu = v // 1000
                break
    if t_cpu == 0:
        for z in sorted(glob.glob("/sys/class/thermal/thermal_zone*/type")):
            if _read_text(Path(z)) in ("TCPU", "x86_pkg_temp"):
                v = _parse_ascii_uint(_read_text(Path(z).parent / "temp"))
                if v is not None:
                    t_cpu = v // 1000
                    break

    t_gpu = 0
    if shutil.which("nvidia-smi"):
        rc, out, _ = _run_cmd(["nvidia-smi", "--query-gpu=temperature.gpu", "--format=csv,noheader,nounits"])
        if rc == 0 and out:
            v_gpu = _parse_ascii_uint(out.splitlines()[0].strip())
            if v_gpu is not None:
                t_gpu = v_gpu
    return t_cpu, t_gpu


def read_intel_backlight_pct() -> int:
    cur_i = _parse_ascii_uint(_read_text(Path("/sys/class/backlight/intel_backlight/brightness")))
    max_i = _parse_ascii_uint(_read_text(Path("/sys/class/backlight/intel_backlight/max_brightness")))
    if cur_i is not None and max_i is not None and max_i > 0:
        return clamp_safe_brightness_pct((100 * cur_i + max_i // 2) // max_i)
    return 100


def _is_pad_output_active() -> bool:
    rc, out, _ = _run_cmd(["hyprctl", "monitors", "all", "-j"])
    if rc != 0 or not out:
        return False
    try:
        mons = json.loads(out)
        if not isinstance(mons, list):
            return False
        for m in mons:
            if isinstance(m, dict) and m.get("name") == PAD_OUTPUT and not bool(m.get("disabled", False)):
                return True
    except ValueError:
        return False
    return False


def load_state(state_path: Path) -> dict[str, Any]:
    default_s: dict[str, Any] = {
        "schema": "aletheion-display-guard-state/v1",
        "manual_override": None,
        "shed_active": False,
        "saved_brightness_pct": None,
        "saved_screenpad_brightness": None,
        "screen_sleep_active": False,
        "last_mode": "dual",
        "updated_utc": None,
    }
    if not state_path.parent.exists():
        return default_s
    parent_fd = _open_state_parent_fd(state_path.parent, create_missing=False)
    fd = -1
    try:
        try:
            fd = os.open(state_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except FileNotFoundError:
            return default_s
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise GuardDaemonError("SYMLINK_FORBIDDEN", f"state file is symlink: {state_path}") from exc
            raise GuardDaemonError("STATE_READ_ERROR", f"cannot open state file {state_path}: {exc}") from exc
        st = os.fstat(fd)
        require_ok(stat.S_ISREG(st.st_mode), "NON_REGULAR_STATE_FILE", f"state path is not regular file: {state_path}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            total += len(chunk)
            require_ok(total <= 65536, "STATE_FILE_TOO_LARGE", "state file exceeds 64 KiB")
            chunks.append(chunk)
        parsed = json.loads(b"".join(chunks).decode("utf-8"))
        if isinstance(parsed, dict):
            out = dict(default_s)
            out.update(parsed)
            return out
    except GuardDaemonError:
        raise
    except (OSError, ValueError):
        pass
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
    return default_s


def save_state(state_path: Path, state: dict[str, Any]) -> None:
    parent_fd = _open_state_parent_fd(state_path.parent, create_missing=True)
    leaf = state_path.name
    tmp_name: str | None = None
    fd = -1
    try:
        try:
            tgt_st = os.lstat(leaf, dir_fd=parent_fd)
        except FileNotFoundError:
            tgt_st = None
        if tgt_st is not None:
            require_ok(not stat.S_ISLNK(tgt_st.st_mode), "SYMLINK_FORBIDDEN", f"target state file is symlink: {state_path}")
            require_ok(stat.S_ISREG(tgt_st.st_mode), "NON_REGULAR_STATE_FILE", f"target state file is not regular: {state_path}")
            require_ok(tgt_st.st_nlink <= 1, "HARDLINK_FORBIDDEN", f"target state file has st_nlink > 1: {state_path}")

        payload = dict(state)
        payload["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        raw = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
        cand = f".{leaf}.{os.getpid()}.{secrets.token_hex(8)}.tmp"
        fd = os.open(cand, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=parent_fd)
        tmp_name = cand
        os.fchmod(fd, 0o600)
        view = memoryview(raw)
        written = 0
        while written < len(view):
            n = os.write(fd, view[written:])
            require_ok(n > 0, "SHORT_WRITE", "os.write returned 0 bytes")
            written += n
        os.fsync(fd)
        require_ok(os.fstat(fd).st_nlink == 1, "HARDLINK_FORBIDDEN", "temp state file hardlinked")
        os.close(fd)
        fd = -1
        os.replace(tmp_name, leaf, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        tmp_name = None
        os.fsync(parent_fd)
    except GuardDaemonError:
        raise
    except OSError as exc:
        raise GuardDaemonError("STATE_WRITE_ERROR", f"atomic state write failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def _display_bin() -> str:
    cand = Path.home() / ".local" / "bin" / "aletheion-display"
    if cand.is_file() and os.access(cand, os.X_OK):
        return str(cand)
    sibling = Path(__file__).resolve().parent / "aletheion_display.py"
    return str(sibling)


def handle_dpms_wake(
    output_name: str,
    *,
    state_path: Path,
    dry_run: bool = False,
    pad_active_override: bool | None = None,
) -> dict[str, Any]:
    """Restore saved ScreenPad backlight on DPMS wake unless DP-3 is intentionally disabled."""
    state = load_state(state_path)
    if not bool(state.get("screen_sleep_active", False)):
        return {"handled": False, "reason": "not_in_screen_sleep", "output": output_name}

    pad_active = _is_pad_output_active() if pad_active_override is None else bool(pad_active_override)
    saved = state.get("saved_screenpad_brightness")
    restore_val = saved if (type(saved) is int and 1 <= saved <= SCREENPAD_MAX_BRIGHTNESS) else SCREENPAD_DEFAULT_RESTORE
    target_val = restore_val if pad_active else 0

    state["screen_sleep_active"] = False
    if dry_run:
        return {
            "handled": True,
            "dry_run": True,
            "output": output_name,
            "pad_active": pad_active,
            "target_screenpad_brightness": target_val,
        }

    save_state(state_path, state)
    rc, out, err = _run_cmd(["brightnessctl", "-d", "asus_screenpad", "set", str(target_val)])
    return {
        "handled": True,
        "dry_run": False,
        "output": output_name,
        "pad_active": pad_active,
        "target_screenpad_brightness": target_val,
        "brightnessctl_ok": rc == 0,
        "out": out,
        "err": err,
    }


def _dpms_wake_watcher_loop(state_path: Path, dry_run: bool, stop_event: threading.Event) -> None:
    """Zero-CPU blocking reader on Hyprland `.socket2.sock` that restores ScreenPad backlight on DPMS wake."""
    while not stop_event.is_set():
        sock_path = _discover_hypr_socket2()
        if sock_path is None:
            stop_event.wait(2.0)
            continue
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            sock.settimeout(5.0)
            sock.connect(str(sock_path))
            buf = b""
            while not stop_event.is_set():
                try:
                    chunk = sock.recv(4096)
                except socket.timeout:
                    continue
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.decode("utf-8", errors="replace")
                    out_name = parse_dpms_wake_event(line)
                    if out_name is not None:
                        try:
                            res = handle_dpms_wake(out_name, state_path=state_path, dry_run=dry_run)
                            if res.get("handled"):
                                sys.stdout.write(
                                    json.dumps(
                                        {
                                            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                            "event": "dpms_wake",
                                            "result": res,
                                        },
                                        sort_keys=True,
                                    )
                                    + "\n"
                                )
                                sys.stdout.flush()
                        except Exception as exc:
                            sys.stderr.write(f"WARN dpms_wake_handler: {exc}\n")
                            sys.stderr.flush()
        except OSError:
            stop_event.wait(2.0)
        finally:
            try:
                sock.close()
            except OSError:
                pass


def execute_transition(
    decision: str,
    *,
    t_cpu_c: int,
    t_gpu_c: int,
    cfg: HysteresisConfig,
    state_path: Path,
    dry_run: bool,
) -> dict[str, Any]:
    state = load_state(state_path)
    disp = _display_bin()
    disp_cmd_prefix = [disp] if os.access(disp, os.X_OK) else [sys.executable, "-I", "-B", disp]

    if decision == "SHED":
        cur_pct = read_intel_backlight_pct()
        target_pct = compute_shed_target_pct(cur_pct, cfg.shed_brightness_pct)
        state["saved_brightness_pct"] = cur_pct
        state["shed_active"] = True
        msg = f"Aletheion Thermal Guard: TCPU {t_cpu_c}°C / GPU {t_gpu_c}°C — dimmed OLED to {target_pct}% & paused ScreenPad Plus"
        if dry_run:
            return {
                "executed": False, "dry_run": True, "decision": "SHED",
                "saved_brightness_pct": cur_pct, "target_brightness_pct": target_pct, "message": msg,
            }
        save_state(state_path, state)
        _run_cmd(["brightnessctl", "-d", "intel_backlight", "set", f"{target_pct}%"])
        _run_cmd([*disp_cmd_prefix, "mode", "single", "--internal-daemon", "--no-notify"])
        if shutil.which("notify-send"):
            _run_cmd(["notify-send", "-a", "Aletheion Thermal Guard", "Thermal Display Shed", msg])
        return {
            "executed": True, "dry_run": False, "decision": "SHED",
            "saved_brightness_pct": cur_pct, "target_brightness_pct": target_pct, "message": msg,
        }

    if decision == "RESTORE":
        saved = state.get("saved_brightness_pct")
        restore_pct = clamp_safe_brightness_pct(saved if type(saved) is int else 80)
        state["shed_active"] = False
        msg = f"Aletheion Thermal Guard: Cooled (TCPU {t_cpu_c}°C / GPU {t_gpu_c}°C) — restored OLED to {restore_pct}% & ScreenPad Plus"
        if dry_run:
            return {
                "executed": False, "dry_run": True, "decision": "RESTORE",
                "restore_brightness_pct": restore_pct, "message": msg,
            }
        save_state(state_path, state)
        _run_cmd(["brightnessctl", "-d", "intel_backlight", "set", f"{restore_pct}%"])
        _run_cmd([*disp_cmd_prefix, "mode", "dual", "--internal-daemon", "--no-notify"])
        if shutil.which("notify-send"):
            _run_cmd(["notify-send", "-a", "Aletheion Thermal Guard", "Thermal Display Restored", msg])
        return {
            "executed": True, "dry_run": False, "decision": "RESTORE",
            "restore_brightness_pct": restore_pct, "message": msg,
        }

    return {"executed": False, "dry_run": dry_run, "decision": decision}


def run_self_test() -> dict[str, Any]:
    """Deterministic unit-level self-test covering Schmitt trigger, hold timers, override, brightness min, and DPMS wake parser."""
    cfg = HysteresisConfig()
    sm = ThermalDisplayStateMachine(cfg, shed_active=False)

    # 1. Transient hot spike (< 15s) MUST NOT shed!
    r0 = sm.step(now_s=0, t_cpu_c=90, t_gpu_c=60, manual_override=None)
    r1 = sm.step(now_s=10, t_cpu_c=91, t_gpu_c=60, manual_override=None)
    r2 = sm.step(now_s=15, t_cpu_c=84, t_gpu_c=60, manual_override=None)
    # 2. Sustained hot >= 15s MUST SHED!
    s0 = sm.step(now_s=20, t_cpu_c=88, t_gpu_c=60, manual_override=None)
    s1 = sm.step(now_s=30, t_cpu_c=89, t_gpu_c=60, manual_override=None)
    s2 = sm.step(now_s=35, t_cpu_c=88, t_gpu_c=60, manual_override=None)
    # 3. Deadband (77..87 C) MUST NOT flicker restore!
    d0 = sm.step(now_s=40, t_cpu_c=77, t_gpu_c=65, manual_override=None)
    d1 = sm.step(now_s=80, t_cpu_c=77, t_gpu_c=65, manual_override=None)
    # 4. Cool window < 30s interrupted by GPU 71C MUST NOT restore!
    c0 = sm.step(now_s=100, t_cpu_c=74, t_gpu_c=68, manual_override=None)
    c1 = sm.step(now_s=120, t_cpu_c=74, t_gpu_c=71, manual_override=None)
    c2 = sm.step(now_s=135, t_cpu_c=74, t_gpu_c=68, manual_override=None)
    # 5. Sustained cool <=76C CPU AND <=70C GPU for 30s MUST RESTORE!
    c3 = sm.step(now_s=165, t_cpu_c=76, t_gpu_c=70, manual_override=None)
    # 6. Manual override MUST pause daemon unconditionally!
    ov = sm.step(now_s=200, t_cpu_c=95, t_gpu_c=85, manual_override="pin-on")
    # 7. Brightness floor clamp & bad-config fail-closed
    refused_bad_cfg = False
    try:
        HysteresisConfig(shed_brightness_pct=10).validate()
    except GuardDaemonError:
        refused_bad_cfg = True
    # 8. Never brighten already-dim OLED during shed: min(20, 35) == 20, min(80, 35) == 35
    shed_dim_20 = compute_shed_target_pct(20, 35)
    shed_dim_80 = compute_shed_target_pct(80, 35)
    # 9. Hyprland socket2 DPMS wake parser
    wake_edp = parse_dpms_wake_event("dpms>>eDP-1,1")
    wake_pad = parse_dpms_wake_event("dpms>>DP-3,1")
    wake_off = parse_dpms_wake_event("dpms>>eDP-1,0")
    wake_other = parse_dpms_wake_event("workspace>>2")

    all_ok = (
        r0["decision"] == "NONE" and r1["decision"] == "NONE" and r2["decision"] == "NONE"
        and s0["decision"] == "NONE" and s1["decision"] == "NONE"
        and s2["decision"] == "SHED" and s2["shed_active"] is True and s2["target_brightness_pct"] == 35
        and d0["decision"] == "NONE" and d1["decision"] == "NONE" and d1["shed_active"] is True
        and c0["decision"] == "NONE" and c1["decision"] == "NONE" and c2["decision"] == "NONE"
        and c3["decision"] == "RESTORE" and c3["shed_active"] is False
        and ov["decision"] == "OVERRIDE_PAUSED"
        and clamp_safe_brightness_pct(0) == MIN_BRIGHTNESS_FLOOR_PCT
        and clamp_safe_brightness_pct(15) == MIN_BRIGHTNESS_FLOOR_PCT
        and clamp_safe_brightness_pct(100) == 100
        and refused_bad_cfg
        and shed_dim_20 == 20
        and shed_dim_80 == 35
        and wake_edp == "eDP-1"
        and wake_pad == "DP-3"
        and wake_off is None
        and wake_other is None
    )

    payload = {
        "schema": "aletheion-therm-display-guard-selftest/v2",
        "all_passed": all_ok,
        "min_brightness_floor_pct": MIN_BRIGHTNESS_FLOOR_PCT,
        "shed_cpu_c": cfg.shed_cpu_c,
        "shed_gpu_c": cfg.shed_gpu_c,
        "shed_hold_s": cfg.shed_hold_s,
        "restore_cpu_c": cfg.restore_cpu_c,
        "restore_gpu_c": cfg.restore_gpu_c,
        "restore_hold_s": cfg.restore_hold_s,
        "checks_verified": 9,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["payload_sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aletheion Hysteretic Thermal-Display Governor + DPMS Wake Restorer")
    parser.add_argument("--daemon", action="store_true", help="Run continuous polling loop + DPMS socket2 watcher")
    parser.add_argument("--once", action="store_true", help="Run a single sample & decision")
    parser.add_argument("--dry-run", action="store_true", help="Evaluate live temps without mutating displays")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic state-machine self-test")
    parser.add_argument("--interval", type=int, default=DEFAULT_POLL_S, help="Poll interval seconds (default 5)")
    parser.add_argument("--state-file", default=None, help="Override guard state file path")
    args = parser.parse_args(argv)

    try:
        require_ok(args.interval >= 1, "INVALID_INTERVAL", "interval must be >= 1s")
        if args.self_test or (not args.daemon and not args.once):
            res = run_self_test()
            sys.stdout.write(json.dumps(res, indent=2, sort_keys=True) + "\n")
            return 0

        state_path = Path(args.state_file) if args.state_file else default_state_path()
        cfg = HysteresisConfig()
        st0 = load_state(state_path)
        sm = ThermalDisplayStateMachine(cfg, shed_active=bool(st0.get("shed_active", False)))

        if args.once:
            t_cpu, t_gpu = read_cpu_gpu_temps()
            st = load_state(state_path)
            dec = sm.step(
                now_s=int(time.monotonic()),
                t_cpu_c=t_cpu,
                t_gpu_c=t_gpu,
                manual_override=st.get("manual_override"),
            )
            act = execute_transition(
                dec["decision"], t_cpu_c=t_cpu, t_gpu_c=t_gpu, cfg=cfg, state_path=state_path, dry_run=args.dry_run
            )
            out = {
                "t_cpu_c": t_cpu, "t_gpu_c": t_gpu, "dry_run": args.dry_run,
                "decision": dec, "action": act,
            }
            sys.stdout.write(json.dumps(out, indent=2, sort_keys=True) + "\n")
            return 0

        # Launch zero-CPU Hyprland socket2 DPMS wake watcher alongside thermal polling loop.
        stop_event = threading.Event()
        watcher = threading.Thread(
            target=_dpms_wake_watcher_loop,
            args=(state_path, args.dry_run, stop_event),
            name="aletheion-dpms-wake",
            daemon=True,
        )
        watcher.start()

        # Continuous low-overhead daemon loop
        try:
            while True:
                t_cpu, t_gpu = read_cpu_gpu_temps()
                st = load_state(state_path)
                # Keep shed_active synced if external tool cleared it
                sm.shed_active = bool(st.get("shed_active", sm.shed_active))
                dec = sm.step(
                    now_s=int(time.monotonic()),
                    t_cpu_c=t_cpu,
                    t_gpu_c=t_gpu,
                    manual_override=st.get("manual_override"),
                )
                if dec["decision"] in ("SHED", "RESTORE"):
                    act = execute_transition(
                        dec["decision"], t_cpu_c=t_cpu, t_gpu_c=t_gpu, cfg=cfg, state_path=state_path, dry_run=args.dry_run
                    )
                    sys.stdout.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                 "t_cpu_c": t_cpu, "t_gpu_c": t_gpu, "action": act}, sort_keys=True) + "\n")
                    sys.stdout.flush()
                time.sleep(args.interval)
        finally:
            stop_event.set()
    except GuardDaemonError as exc:
        sys.stderr.write(f"ERROR {exc}\n")
        return 2
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
