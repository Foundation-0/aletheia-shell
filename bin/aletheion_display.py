#!/usr/bin/env python3
"""
Track S52 & EXP-Q17a: `aletheion-display` — Safe, Reversible Omarchy 4.5.0
Hyprland Display Power-Gating, True Hardware `asus_screenpad` Backlight Off,
and Zero-Burn-In Instant-Wake Screen Sleep Controller for ASUS ZenBook Pro Duo UX582HS.

Hardware facts (live-verified on UX582HS):
  - Primary upper OLED:  eDP-1  (EDID modes: 3840x2160@60 ONLY -> kept at native EDID scale 2 for zero blanking risk!)
  - Lower ScreenPad Plus: DP-3  (EDID modes: 3840x1100@60, 1920x550@60, or disabled via Omarchy Lua hyprctl eval)
  - Primary backlight:   intel_backlight (0..528, controllable via `brightnessctl` logind D-Bus without sudo)
  - ScreenPad backlight: asus_screenpad (0..255, controllable WITHOUT sudo via `brightnessctl -d asus_screenpad set <0..255>`
                         through systemd-logind D-Bus! Read `actual_brightness`, NOT raw `brightness` which carries
                         asus-nb-wmi upper-byte flags like 130898 = 0x1FF52 on boot!)
  - Shared state file:   ~/.local/state/aletheion/display_guard.json (manual_override + saved backlight + sleep handshake)

100% Python 3 stdlib, zero bare asserts, zero /dev/null writes, 4-mode -I parity.
"""
from __future__ import annotations

import argparse
import errno
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import stat
import subprocess
import sys
import time
from typing import Any


PRIMARY_OUTPUT = "eDP-1"
PAD_OUTPUT = "DP-3"
PRIMARY_MODE_SAFE = "3840x2160@60"
PRIMARY_POS = "0x0"
PRIMARY_SCALE = 2
PAD_MODE_FULL = "3840x1100@60"
PAD_MODE_HALF = "1920x550@60"
PAD_POS = "0x1080"
PAD_SCALE_FULL = 2
PAD_SCALE_HALF = 1
MIN_BRIGHTNESS_FLOOR_PCT = 20
SCREENPAD_MAX_BRIGHTNESS_FALLBACK = 255
SCREENPAD_DEFAULT_DUAL_BRIGHTNESS = 180
SCREENPAD_DEFAULT_HALF_BRIGHTNESS = 140
DEFAULT_SLEEP_DEBOUNCE_MS = 350
MAX_SLEEP_DEBOUNCE_MS = 3000

VALID_MODES = ("dual", "pad-half", "single", "compute")


class DisplayError(ValueError):
    """Fail-closed structured error for display controller."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def require_ok(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise DisplayError(code, message)


def default_state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    root = Path(base) if base else (Path.home() / ".local" / "state")
    return root / "aletheion" / "display_guard.json"


def _discover_hypr_env() -> dict[str, str]:
    """Ensure HYPRLAND_INSTANCE_SIGNATURE & XDG_RUNTIME_DIR are populated even inside systemd --user."""
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


def _run_cmd(cmd: list[str], *, timeout: int = 8) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(
            cmd,
            env=_discover_hypr_env(),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


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
            raise DisplayError("SYMLINK_FORBIDDEN", f"state parent is symlink or non-directory: {parent}") from exc
        raise DisplayError("STATE_DIR_ERROR", f"cannot open state directory {parent}: {exc}") from exc


def load_guard_state(state_path: Path) -> dict[str, Any]:
    default_state: dict[str, Any] = {
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
        return default_state
    parent_fd = _open_state_parent_fd(state_path.parent, create_missing=False)
    fd = -1
    try:
        try:
            fd = os.open(state_path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent_fd)
        except FileNotFoundError:
            return default_state
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise DisplayError("SYMLINK_FORBIDDEN", f"state file is symlink: {state_path}") from exc
            raise DisplayError("STATE_READ_ERROR", f"cannot open state file {state_path}: {exc}") from exc
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
            merged = dict(default_state)
            merged.update(parsed)
            return merged
    except DisplayError:
        raise
    except (OSError, ValueError):
        pass
    finally:
        if fd >= 0:
            os.close(fd)
        os.close(parent_fd)
    return default_state


def save_guard_state(state_path: Path, state: dict[str, Any]) -> None:
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
    except DisplayError:
        raise
    except OSError as exc:
        raise DisplayError("STATE_WRITE_ERROR", f"atomic state write failed: {exc}") from exc
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp_name is not None:
            try:
                os.unlink(tmp_name, dir_fd=parent_fd)
            except OSError:
                pass
        os.close(parent_fd)


def build_lua_monitor_cmd(output: str, *, disabled: bool, mode: str = "", position: str = "", scale: int = 2) -> str:
    """Construct validated Omarchy 4.5.0 Hyprland Lua monitor rule string."""
    require_ok(type(output) is str and bool(re.fullmatch(r"[A-Za-z0-9._-]+", output)), "UNSAFE_OUTPUT", f"unsafe output name {output!r}")
    if disabled:
        require_ok(output != PRIMARY_OUTPUT, "REFUSE_DISABLE_PRIMARY", "never disable primary OLED eDP-1")
        return f'hl.monitor({{ output = "{output}", disabled = true }})'
    require_ok(type(mode) is str and bool(re.fullmatch(r"[0-9]+x[0-9]+@[0-9]+(\.[0-9]+)?", mode)), "UNSAFE_MODE", f"unsafe mode {mode!r}")
    require_ok(type(position) is str and bool(re.fullmatch(r"-?[0-9]+x-?[0-9]+", position)), "UNSAFE_POS", f"unsafe pos {position!r}")
    require_ok(type(scale) is int and 1 <= scale <= 4, "UNSAFE_SCALE", f"unsafe scale {scale!r}")
    return f'hl.monitor({{ output = "{output}", disabled = false, mode = "{mode}", position = "{position}", scale = {scale} }})'


def query_monitors() -> list[dict[str, Any]]:
    rc, out, _ = _run_cmd(["hyprctl", "monitors", "all", "-j"])
    if rc != 0 or not out:
        return []
    try:
        data = json.loads(out)
        return data if isinstance(data, list) else []
    except ValueError:
        return []


def is_output_active(monitors: list[dict[str, Any]], name: str) -> bool:
    for m in monitors:
        if m.get("name") == name and not bool(m.get("disabled", False)):
            return True
    return False


def read_screenpad_actual_brightness(sysfs_dir: Path | None = None) -> int | None:
    """Read true 0..max hardware brightness from asus_screenpad/actual_brightness.

    The raw `brightness` node on asus-nb-wmi carries kernel power flags in upper bytes
    (e.g. 130898 == 0x1FF52 where low byte 0x52 == 82 == actual_brightness), so always
    prefer `actual_brightness` and fall back to masking `brightness` only when absent.
    """
    base = sysfs_dir if sysfs_dir is not None else Path("/sys/class/backlight/asus_screenpad")
    if not base.exists():
        return None
    max_i = _parse_ascii_uint(_read_text(base / "max_brightness"))
    max_val = max_i if (max_i is not None and max_i > 0) else SCREENPAD_MAX_BRIGHTNESS_FALLBACK
    actual = _parse_ascii_uint(_read_text(base / "actual_brightness"))
    if actual is not None and actual <= max_val:
        return actual
    raw = _parse_ascii_uint(_read_text(base / "brightness"))
    if raw is not None:
        if raw <= max_val:
            return raw
        low = raw & 0xFF
        if low <= max_val:
            return low
    return None


def resolve_screenpad_restore_brightness(state: dict[str, Any], default_value: int) -> int:
    """Return validated saved ScreenPad backlight (1..255) or the provided default."""
    require_ok(
        type(default_value) is int and 1 <= default_value <= SCREENPAD_MAX_BRIGHTNESS_FALLBACK,
        "INVALID_SCREENPAD_DEFAULT",
        f"default screenpad brightness must be int in 1..255, got {default_value!r}",
    )
    saved = state.get("saved_screenpad_brightness")
    if type(saved) is int and 1 <= saved <= SCREENPAD_MAX_BRIGHTNESS_FALLBACK:
        return saved
    return default_value


def get_backlight_info() -> dict[str, Any]:
    info: dict[str, Any] = {}
    has_brightnessctl = shutil.which("brightnessctl") is not None
    for dev in ("intel_backlight", "asus_screenpad"):
        p = Path("/sys/class/backlight") / dev
        if not p.exists():
            continue
        raw_i = _parse_ascii_uint(_read_text(p / "brightness"))
        actual_i = _parse_ascii_uint(_read_text(p / "actual_brightness"))
        max_i = _parse_ascii_uint(_read_text(p / "max_brightness"))
        bl_power = _parse_ascii_uint(_read_text(p / "bl_power"))
        cur_i = actual_i if (dev == "asus_screenpad" and actual_i is not None) else raw_i
        if cur_i is None and raw_i is not None and max_i is not None and raw_i > max_i:
            cur_i = raw_i & 0xFF
        pct: int | None = None
        if cur_i is not None and max_i is not None and max_i > 0 and cur_i <= max_i:
            pct = (100 * cur_i + max_i // 2) // max_i
        info[dev] = {
            "current": cur_i,
            "actual_brightness": actual_i,
            "raw_brightness": raw_i,
            "max": max_i,
            "percent": pct,
            "bl_power": bl_power,
            "writable": os.access(p / "brightness", os.W_OK) or has_brightnessctl,
            "brightnessctl_available": has_brightnessctl,
        }
    return info


def get_thermal_brief() -> dict[str, Any]:
    cpu_c: int | None = None
    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/name")):
        if _read_text(Path(p)) == "coretemp":
            v = _parse_ascii_uint(_read_text(Path(p).parent / "temp1_input"))
            if v is not None:
                cpu_c = v // 1000
                break
    gpu_c: int | None = None
    gpu_w: str | None = None
    rc, out, _ = _run_cmd(["nvidia-smi", "--query-gpu=temperature.gpu,power.draw", "--format=csv,noheader,nounits"])
    if rc == 0 and out:
        parts = [x.strip() for x in out.splitlines()[0].split(",")]
        if parts:
            gpu_c = _parse_ascii_uint(parts[0])
        if len(parts) > 1:
            gpu_w = parts[1]
    bat_w = _parse_ascii_uint(_read_text(Path("/sys/class/power_supply/BAT0/power_now")))
    bat_lim = _parse_ascii_uint(_read_text(Path("/sys/class/power_supply/BAT0/charge_control_end_threshold")))
    return {
        "cpu_pkg_c": cpu_c,
        "gpu_c": gpu_c,
        "gpu_power_draw": gpu_w,
        "battery_power_uw": bat_w if bat_w is not None else 0,
        "battery_charge_limit_pct": bat_lim,
    }


def _try_set_screenpad_backlight(value: int, *, dry_run: bool, verify_off: bool = True) -> dict[str, Any]:
    """Set `asus_screenpad` hardware LED backlight via `brightnessctl` (systemd-logind D-Bus, no sudo)."""
    require_ok(type(value) is int, "INVALID_SCREENPAD_VALUE", f"screenpad brightness must be exact int, got {type(value).__name__}")
    bl_dir = Path("/sys/class/backlight/asus_screenpad")
    if not bl_dir.exists():
        return {"attempted": False, "reason": "device_absent"}
    max_i = _parse_ascii_uint(_read_text(bl_dir / "max_brightness"))
    max_val = max_i if (max_i is not None and max_i > 0) else SCREENPAD_MAX_BRIGHTNESS_FALLBACK
    clamped = max(0, min(max_val, value))
    cmd = ["brightnessctl", "-d", "asus_screenpad", "set", str(clamped)]
    if dry_run:
        return {"attempted": False, "dry_run": True, "target_value": clamped, "method": "brightnessctl", "cmd": cmd}
    if shutil.which("brightnessctl") is not None:
        rc, out, err = _run_cmd(cmd)
        verified_actual: int | None = None
        if rc == 0 and verify_off and clamped == 0:
            # ASUS WMI smooth-fades the ScreenPad LEDs to 0 over ~300-400 ms; poll briefly until settled.
            for _ in range(6):
                verified_actual = read_screenpad_actual_brightness(bl_dir)
                if verified_actual == 0:
                    break
                time.sleep(0.1)
        elif rc == 0:
            verified_actual = read_screenpad_actual_brightness(bl_dir)
        return {
            "attempted": True,
            "ok": rc == 0,
            "rc": rc,
            "method": "brightnessctl",
            "target_value": clamped,
            "verified_actual_brightness": verified_actual,
            "out": out,
            "err": err,
        }
    # Fallback only if brightnessctl binary is absent and sysfs happens to be writable.
    bl_node = bl_dir / "brightness"
    if not os.access(bl_node, os.W_OK):
        return {"attempted": False, "reason": "brightnessctl_absent_and_sysfs_non_writable"}
    try:
        bl_node.write_text(f"{clamped}\n", encoding="utf-8")
        return {
            "attempted": True,
            "ok": True,
            "method": "sysfs",
            "target_value": clamped,
            "verified_actual_brightness": read_screenpad_actual_brightness(bl_dir),
        }
    except OSError as exc:
        return {"attempted": True, "ok": False, "method": "sysfs", "target_value": clamped, "error": str(exc)}


def set_primary_brightness_pct(pct: int, *, dry_run: bool) -> dict[str, Any]:
    require_ok(type(pct) is int, "INVALID_BRIGHTNESS_TYPE", "brightness percent must be exact int")
    clamped = max(MIN_BRIGHTNESS_FLOOR_PCT, min(100, pct))
    cmd = ["brightnessctl", "-d", "intel_backlight", "set", f"{clamped}%"]
    if dry_run:
        return {"dry_run": True, "requested_pct": pct, "applied_pct": clamped, "cmd": cmd}
    rc, out, err = _run_cmd(cmd)
    return {"ok": rc == 0, "rc": rc, "requested_pct": pct, "applied_pct": clamped, "out": out, "err": err}


def send_notification(title: str, body: str, *, dry_run: bool) -> dict[str, Any]:
    cmd = ["notify-send", "-a", "Aletheion Display", title, body]
    if dry_run or shutil.which("notify-send") is None:
        return {"sent": False, "dry_run": dry_run, "title": title, "body": body}
    rc, _, err = _run_cmd(cmd)
    return {"sent": rc == 0, "rc": rc, "err": err}


def _apply_hypr_lua(lua_expr: str, *, dry_run: bool) -> dict[str, Any]:
    cmd = ["hyprctl", "eval", lua_expr]
    if dry_run:
        return {"dry_run": True, "lua": lua_expr, "ok": True}
    rc, out, err = _run_cmd(cmd)
    return {"ok": rc == 0, "rc": rc, "lua": lua_expr, "out": out, "err": err}


def apply_mode(
    mode: str,
    *,
    state_path: Path,
    dry_run: bool = False,
    internal_daemon: bool = False,
    notify: bool = True,
    dim_pct: int | None = None,
) -> dict[str, Any]:
    require_ok(mode in VALID_MODES, "INVALID_MODE", f"mode must be in {VALID_MODES}, got {mode!r}")

    state = load_guard_state(state_path)
    actions: list[dict[str, Any]] = []
    # Always ensure primary eDP-1 stays safely at native EDID 3840x2160@60 scale 2!
    edp_lua = build_lua_monitor_cmd(
        PRIMARY_OUTPUT, disabled=False, mode=PRIMARY_MODE_SAFE, position=PRIMARY_POS, scale=PRIMARY_SCALE
    )

    if mode == "dual":
        pad_lua = build_lua_monitor_cmd(
            PAD_OUTPUT, disabled=False, mode=PAD_MODE_FULL, position=PAD_POS, scale=PAD_SCALE_FULL
        )
        actions.append(_apply_hypr_lua(edp_lua, dry_run=dry_run))
        actions.append(_apply_hypr_lua(pad_lua, dry_run=dry_run))
        restore_val = resolve_screenpad_restore_brightness(state, SCREENPAD_DEFAULT_DUAL_BRIGHTNESS)
        bl_res = _try_set_screenpad_backlight(restore_val, dry_run=dry_run)
        override_val = "pin-on"
        notif_msg = f"Dual 4K + ScreenPad Plus (3840x1100@60, backlight {restore_val}/255) active"
    elif mode == "pad-half":
        pad_lua = build_lua_monitor_cmd(
            PAD_OUTPUT, disabled=False, mode=PAD_MODE_HALF, position=PAD_POS, scale=PAD_SCALE_HALF
        )
        actions.append(_apply_hypr_lua(edp_lua, dry_run=dry_run))
        actions.append(_apply_hypr_lua(pad_lua, dry_run=dry_run))
        restore_val = resolve_screenpad_restore_brightness(state, SCREENPAD_DEFAULT_HALF_BRIGHTNESS)
        bl_res = _try_set_screenpad_backlight(restore_val, dry_run=dry_run)
        override_val = "pin-on"
        notif_msg = f"ScreenPad Plus half-res (1920x550@60, backlight {restore_val}/255)"
    elif mode == "single":
        cur_pad_bl = read_screenpad_actual_brightness()
        if type(cur_pad_bl) is int and cur_pad_bl > 0:
            state["saved_screenpad_brightness"] = cur_pad_bl
        pad_lua = build_lua_monitor_cmd(PAD_OUTPUT, disabled=True)
        actions.append(_apply_hypr_lua(pad_lua, dry_run=dry_run))
        bl_res = _try_set_screenpad_backlight(0, dry_run=dry_run)
        override_val = "pin-off"
        notif_msg = "ScreenPad Plus (DP-3) paused & LED backlight OFF — single OLED active"
    else:  # compute
        cur_pad_bl = read_screenpad_actual_brightness()
        if type(cur_pad_bl) is int and cur_pad_bl > 0:
            state["saved_screenpad_brightness"] = cur_pad_bl
        pad_lua = build_lua_monitor_cmd(PAD_OUTPUT, disabled=True)
        actions.append(_apply_hypr_lua(edp_lua, dry_run=dry_run))
        actions.append(_apply_hypr_lua(pad_lua, dry_run=dry_run))
        bl_res = _try_set_screenpad_backlight(0, dry_run=dry_run)
        override_val = "pin-off"
        notif_msg = "Compute mode: DP-3 & LED backlight OFF, eDP-1 native EDID 3840x2160@60"

    dim_res = None
    if dim_pct is not None:
        dim_res = set_primary_brightness_pct(dim_pct, dry_run=dry_run)

    # Verify primary eDP-1 remained active when running live
    primary_verified = True
    if not dry_run:
        mons_after = query_monitors()
        if mons_after:
            primary_verified = is_output_active(mons_after, PRIMARY_OUTPUT)
            if not primary_verified:
                # Emergency safety recovery: re-enable eDP-1 immediately!
                _apply_hypr_lua(edp_lua, dry_run=False)
                mons_rec = query_monitors()
                primary_verified = is_output_active(mons_rec, PRIMARY_OUTPUT)
            require_ok(primary_verified, "PRIMARY_DISPLAY_RECOVERY_FAILED", "eDP-1 is not active after mode switch")

    state["last_mode"] = mode
    state["screen_sleep_active"] = False
    if not internal_daemon:
        state["manual_override"] = override_val
    if not dry_run:
        save_guard_state(state_path, state)

    notif_res = send_notification("Aletheion Display", notif_msg, dry_run=(dry_run or not notify))
    return {
        "action": f"mode:{mode}",
        "dry_run": dry_run,
        "internal_daemon": internal_daemon,
        "edp1_mode_policy": "edid_native_safe_3840x2160",
        "primary_verified_active": primary_verified,
        "actions": actions,
        "screenpad_backlight": bl_res,
        "saved_screenpad_brightness": state.get("saved_screenpad_brightness"),
        "dim_result": dim_res,
        "manual_override": state["manual_override"],
        "notification": notif_res,
    }


def cmd_sleep_all(
    *,
    state_path: Path,
    debounce_ms: int = DEFAULT_SLEEP_DEBOUNCE_MS,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Turn off both eDP-1 OLED (DPMS off) and asus_screenpad LED backlight (0).

    Waits `debounce_ms` (default 350ms) before DPMS off so the mouse-button release
    event from clicking the top-bar button does not immediately re-wake Hyprland DPMS.
    Any subsequent mouse move, key press, or touch wakes instantaneously and the
    Hyprland socket2 watcher restores the saved ScreenPad backlight if DP-3 is active.
    """
    require_ok(
        type(debounce_ms) is int and 0 <= debounce_ms <= MAX_SLEEP_DEBOUNCE_MS,
        "INVALID_DEBOUNCE_MS",
        f"debounce_ms must be int in 0..{MAX_SLEEP_DEBOUNCE_MS}, got {debounce_ms!r}",
    )
    state = load_guard_state(state_path)
    cur_pad_bl = read_screenpad_actual_brightness()
    if type(cur_pad_bl) is int and cur_pad_bl > 0:
        state["saved_screenpad_brightness"] = cur_pad_bl
    state["screen_sleep_active"] = True

    if dry_run:
        return {
            "action": "sleep-all",
            "dry_run": True,
            "debounce_ms": debounce_ms,
            "saved_screenpad_brightness": state.get("saved_screenpad_brightness"),
            "screenpad_backlight": _try_set_screenpad_backlight(0, dry_run=True),
            "dpms_cmd": ["hyprctl", "dispatch", 'hl.dsp.dpms({ action = "disable" })'],
            "screen_sleep_active": True,
        }

    # Persist state BEFORE cutting DPMS so socket2 wake watcher sees screen_sleep_active=True.
    save_guard_state(state_path, state)
    if debounce_ms > 0:
        time.sleep(debounce_ms / 1000.0)
    bl_res = _try_set_screenpad_backlight(0, dry_run=False, verify_off=False)
    rc, out, err = _run_cmd(["hyprctl", "dispatch", 'hl.dsp.dpms({ action = "disable" })'])
    if rc != 0 or "error:" in out.lower():
        rc, out, err = _run_cmd(["hyprctl", "dispatch", "dpms", "off"])
    return {
        "action": "sleep-all",
        "dry_run": False,
        "debounce_ms": debounce_ms,
        "saved_screenpad_brightness": state.get("saved_screenpad_brightness"),
        "screenpad_backlight": bl_res,
        "dpms": {"ok": rc == 0 and "error:" not in out.lower(), "rc": rc, "out": out, "err": err},
        "screen_sleep_active": True,
    }


def cmd_wake_restore(*, state_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """Restore saved ScreenPad backlight after DPMS wake iff DP-3 is active."""
    state = load_guard_state(state_path)
    mons = query_monitors()
    pad_active = is_output_active(mons, PAD_OUTPUT)
    was_sleeping = bool(state.get("screen_sleep_active", False))

    if pad_active:
        target = resolve_screenpad_restore_brightness(state, SCREENPAD_DEFAULT_DUAL_BRIGHTNESS)
    else:
        target = 0

    bl_res = _try_set_screenpad_backlight(target, dry_run=dry_run, verify_off=(target == 0))
    state["screen_sleep_active"] = False
    if not dry_run:
        save_guard_state(state_path, state)
    return {
        "action": "wake-restore",
        "dry_run": dry_run,
        "was_sleeping": was_sleeping,
        "screenpad_active": pad_active,
        "target_brightness": target,
        "screenpad_backlight": bl_res,
    }


def cmd_focus_deck(*, state_path: Path, dry_run: bool = False) -> dict[str, Any]:
    """Ensure DP-3 + backlight are enabled, then focus or launch `aletheia-deck` on DP-3 workspace 19."""
    mons = query_monitors()
    pad_on = is_output_active(mons, PAD_OUTPUT)
    enable_res: dict[str, Any] | None = None
    bl_restore: dict[str, Any] | None = None
    if not pad_on:
        enable_res = apply_mode("dual", state_path=state_path, dry_run=dry_run, notify=False)
    else:
        cur_bl = read_screenpad_actual_brightness()
        if cur_bl == 0:
            state = load_guard_state(state_path)
            restore_val = resolve_screenpad_restore_brightness(state, SCREENPAD_DEFAULT_DUAL_BRIGHTNESS)
            bl_restore = _try_set_screenpad_backlight(restore_val, dry_run=dry_run)

    rc_c, out_c, _ = _run_cmd(["hyprctl", "clients", "-j"])
    deck_addr: str | None = None
    if rc_c == 0 and out_c:
        try:
            clients = json.loads(out_c)
            if isinstance(clients, list):
                for c in clients:
                    cls = str(c.get("class") or c.get("initialClass") or "").lower()
                    if cls == "aletheia-deck":
                        deck_addr = str(c.get("address") or "")
                        break
        except ValueError:
            deck_addr = None

    if dry_run:
        return {
            "action": "focus-deck",
            "dry_run": True,
            "pad_was_on": pad_on,
            "enable_result": enable_res,
            "backlight_restore": bl_restore,
            "existing_address": deck_addr,
        }

    if deck_addr:
        require_ok(bool(re.fullmatch(r"0x[0-9a-fA-F]+", deck_addr)), "UNSAFE_ADDRESS", f"unexpected address {deck_addr!r}")
        rc, out, err = _run_cmd(["hyprctl", "dispatch", f'hl.dsp.focus({{ window = "address:{deck_addr}" }})'])
        if rc != 0 or "error:" in out.lower():
            rc, out, err = _run_cmd(["hyprctl", "dispatch", "focuswindow", f"address:{deck_addr}"])
        return {
            "action": "focus-deck",
            "dry_run": False,
            "mode": "focused",
            "address": deck_addr,
            "backlight_restore": bl_restore,
            "ok": rc == 0 and "error:" not in out.lower(),
            "out": out,
            "err": err,
        }

    # Resolve installed binary cleanly without relying on shell expansion.
    cand_bins = [
        shutil.which("aletheia-deck"),
        str(Path.home() / ".local" / "bin" / "aletheia-deck"),
        str(Path.home() / "Work" / "aletheia-deck" / "build" / "aletheia-deck"),
    ]
    deck_bin = next((b for b in cand_bins if b and Path(b).is_file() and os.access(b, os.X_OK)), None)
    if deck_bin is None:
        send_notification("Aletheia Deck", "aletheia-deck binary not installed yet", dry_run=False)
        return {"action": "focus-deck", "dry_run": False, "mode": "missing_binary", "ok": False}

    # Hyprland window rule pins aletheia-deck to DP-3 workspace 19 (slot 9 of block 1).
    safe_bin = deck_bin.replace("\\", "\\\\").replace('"', '\\"')
    rc, out, err = _run_cmd(["hyprctl", "eval", f'hl.exec_cmd("{safe_bin}")'])
    return {
        "action": "focus-deck",
        "dry_run": False,
        "mode": "launched",
        "binary": deck_bin,
        "backlight_restore": bl_restore,
        "ok": rc == 0 and "error:" not in out.lower(),
        "out": out,
        "err": err,
    }


def cmd_status(state_path: Path) -> dict[str, Any]:
    mons = query_monitors()
    mon_summary = [
        {
            "name": m.get("name"),
            "width": m.get("width"),
            "height": m.get("height"),
            "refreshRate": m.get("refreshRate"),
            "x": m.get("x"),
            "y": m.get("y"),
            "scale": m.get("scale"),
            "disabled": bool(m.get("disabled", False)),
            "dpmsStatus": m.get("dpmsStatus"),
        }
        for m in mons
    ]
    return {
        "schema": "aletheion-display-status/v1",
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "primary_active": is_output_active(mons, PRIMARY_OUTPUT),
        "screenpad_active": is_output_active(mons, PAD_OUTPUT),
        "monitors": mon_summary,
        "backlights": get_backlight_info(),
        "thermal": get_thermal_brief(),
        "guard_state": load_guard_state(state_path),
    }


def run_self_test() -> dict[str, Any]:
    """Pure deterministic self-test of Lua command builder, safety floor, backlight, and sleep-all."""
    l_edp = build_lua_monitor_cmd(PRIMARY_OUTPUT, disabled=False, mode=PRIMARY_MODE_SAFE, position=PRIMARY_POS, scale=2)
    l_pad_off = build_lua_monitor_cmd(PAD_OUTPUT, disabled=True)
    l_pad_half = build_lua_monitor_cmd(PAD_OUTPUT, disabled=False, mode=PAD_MODE_HALF, position=PAD_POS, scale=1)

    clamped0 = set_primary_brightness_pct(0, dry_run=True)
    clamped150 = set_primary_brightness_pct(150, dry_run=True)

    restored_saved = resolve_screenpad_restore_brightness({"saved_screenpad_brightness": 95}, 180)
    restored_default = resolve_screenpad_restore_brightness({"saved_screenpad_brightness": 0}, 180)
    sleep_dry = cmd_sleep_all(state_path=Path("/nonexistent/display_guard.json"), debounce_ms=350, dry_run=True)

    refusals = 0
    for fn in (
        lambda: build_lua_monitor_cmd(PRIMARY_OUTPUT, disabled=True),
        lambda: build_lua_monitor_cmd('DP-3"; os.execute("id")--', disabled=True),
        lambda: build_lua_monitor_cmd(PAD_OUTPUT, disabled=False, mode="bogus", position="0x1080", scale=2),
        lambda: build_lua_monitor_cmd(PAD_OUTPUT, disabled=False, mode=PAD_MODE_FULL, position="0x1080", scale=0),
        lambda: set_primary_brightness_pct(True, dry_run=True),  # type: ignore[arg-type]
        lambda: _try_set_screenpad_backlight(True, dry_run=True),  # type: ignore[arg-type]
        lambda: cmd_sleep_all(state_path=Path("/nonexistent/display_guard.json"), debounce_ms=-1, dry_run=True),
    ):
        try:
            fn()
        except DisplayError:
            refusals += 1

    all_ok = (
        'output = "eDP-1"' in l_edp
        and "disabled = false" in l_edp
        and 'output = "DP-3", disabled = true' in l_pad_off
        and 'mode = "1920x550@60"' in l_pad_half
        and "scale = 1" in l_pad_half
        and clamped0["applied_pct"] == MIN_BRIGHTNESS_FLOOR_PCT
        and clamped150["applied_pct"] == 100
        and restored_saved == 95
        and restored_default == 180
        and sleep_dry["action"] == "sleep-all"
        and sleep_dry["debounce_ms"] == 350
        and refusals == 7
    )

    payload = {
        "schema": "aletheion-display-selftest/v2",
        "all_passed": all_ok,
        "min_brightness_floor_pct": MIN_BRIGHTNESS_FLOOR_PCT,
        "refusals_verified": refusals,
        "lua_samples": {"edp_safe": l_edp, "pad_off": l_pad_off, "pad_half": l_pad_half},
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["payload_sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aletheion UX582HS Display & ScreenPad Plus Controller")
    parser.add_argument(
        "command",
        nargs="?",
        default="self-test",
        choices=(
            "status",
            "toggle-pad",
            "mode",
            "cycle-compute",
            "pin-on",
            "pin-off",
            "auto",
            "sleep-all",
            "wake-restore",
            "focus-deck",
            "self-test",
        ),
    )
    parser.add_argument("mode_arg", nargs="?", default=None, help="Mode for `mode` command (dual|pad-half|single|compute)")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without mutating live displays")
    parser.add_argument("--internal-daemon", action="store_true", help="Do not record manual_override (for daemon)")
    parser.add_argument("--no-notify", action="store_true", help="Suppress desktop notification")
    parser.add_argument("--dim-pct", type=int, default=None, help="Optional primary OLED brightness percent (>=20)")
    parser.add_argument("--debounce-ms", type=int, default=DEFAULT_SLEEP_DEBOUNCE_MS, help="Click-release debounce ms for sleep-all (default 350)")
    parser.add_argument("--state-file", default=None, help="Override guard state JSON path")
    args = parser.parse_args(argv)

    state_path = Path(args.state_file) if args.state_file else default_state_path()
    try:
        if args.command == "self-test":
            res = run_self_test()
        elif args.command == "status":
            res = cmd_status(state_path)
        elif args.command == "sleep-all":
            res = cmd_sleep_all(state_path=state_path, debounce_ms=args.debounce_ms, dry_run=args.dry_run)
        elif args.command == "wake-restore":
            res = cmd_wake_restore(state_path=state_path, dry_run=args.dry_run)
        elif args.command == "focus-deck":
            res = cmd_focus_deck(state_path=state_path, dry_run=args.dry_run)
        elif args.command == "mode":
            require_ok(args.mode_arg in VALID_MODES, "MISSING_OR_INVALID_MODE", f"choose mode in {VALID_MODES}")
            res = apply_mode(
                args.mode_arg,
                state_path=state_path,
                dry_run=args.dry_run,
                internal_daemon=args.internal_daemon,
                notify=not args.no_notify,
                dim_pct=args.dim_pct,
            )
        elif args.command == "toggle-pad":
            mons = query_monitors()
            pad_on = is_output_active(mons, PAD_OUTPUT)
            target_mode = "single" if pad_on else "dual"
            res = apply_mode(
                target_mode,
                state_path=state_path,
                dry_run=args.dry_run,
                internal_daemon=args.internal_daemon,
                notify=not args.no_notify,
            )
        elif args.command == "cycle-compute":
            mons = query_monitors()
            pad_on = is_output_active(mons, PAD_OUTPUT)
            target_mode = "compute" if pad_on else "dual"
            res = apply_mode(
                target_mode,
                state_path=state_path,
                dry_run=args.dry_run,
                internal_daemon=args.internal_daemon,
                notify=not args.no_notify,
            )
        elif args.command == "pin-on":
            res = apply_mode(
                "dual",
                state_path=state_path,
                dry_run=args.dry_run,
                internal_daemon=False,
                notify=not args.no_notify,
            )
        elif args.command == "pin-off":
            res = apply_mode(
                "single",
                state_path=state_path,
                dry_run=args.dry_run,
                internal_daemon=False,
                notify=not args.no_notify,
            )
        else:  # auto (validated by argparse choices)
            state = load_guard_state(state_path)
            state["manual_override"] = None
            if not args.dry_run:
                save_guard_state(state_path, state)
            notif = send_notification(
                "Aletheion Display",
                "Automatic hysteretic thermal-display control re-enabled",
                dry_run=(args.dry_run or args.no_notify),
            )
            res = {"action": "auto", "dry_run": args.dry_run, "manual_override": None, "notification": notif}
    except DisplayError as exc:
        sys.stderr.write(f"ERROR {exc}\n")
        return 2

    sys.stdout.write(json.dumps(res, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
