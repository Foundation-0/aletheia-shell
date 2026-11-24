#!/usr/bin/env python3
"""
`toroid-hud.py`: Zero-float Continual-Toroid ScreenPad Plus Telemetry Dashboard.

Pure Python 3 stdlib (`int` and `fractions.Fraction` only, zero floats, zero bare asserts).
Reads swarm `board.jsonl`, `UNIFIED_OPERATOR_ATLAS.md`, `members.json`, `/proc/meminfo`,
and optional `nvidia-smi` telemetry to emit deterministic JSON (`--once --json`) or an
ANSI dashboard loop (`--watch`) for the ASUS Zenbook Pro Duo lower ScreenPad Plus.

Usage:
  python3 -I -B bin/toroid-hud.py [--self-test]
  python3 -I -B bin/toroid-hud.py --once --json --board <board.jsonl> [--members <members.json>] [--atlas <atlas.md>]
  python3 -I -B bin/toroid-hud.py --watch --interval 5 --auto-discover --gemini-dir "$HOME/.gemini/antigravity-cli"
"""
from __future__ import annotations

import argparse
import errno
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import sys
import time
from typing import Any, Sequence

SCHEMA = "aletheia.toroid_hud.v1"
MAX_FILE_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 65_536
_ASCII_DIGITS = frozenset("0123456789")

_CARTOGRAPHER_ROLES = ("cartographer", "portal-keeper", "portal_keeper")
_WEAVER_ROLES = ("weaver", "synthesizer")
_POLOIDAL_ROLES = _CARTOGRAPHER_ROLES + _WEAVER_ROLES

_TRACK_RE = re.compile(r"\b(?:Track\s+S|track-s)(\d+[a-z]?)\b", re.IGNORECASE)
_ATLAS_TRACK_RE = re.compile(r"\bS(\d+[a-z]?)\b")
_SIGNAL_RE = re.compile(
    r"\b(?:VERIFIED|FROZEN|obstruction|blind[- ]spot|counterexample|Schur|Krein|Hodge|Frobenius|Landauer)\b",
    re.IGNORECASE,
)


class HudError(ValueError):
    """Structured fail-closed HUD error (never stripped by python3 -O)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def require_ok(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise HudError(code, message)


def _is_ascii_uint(s: str) -> bool:
    return len(s) >= 1 and all(ch in _ASCII_DIGITS for ch in s)


def _fmt_ratio_pct(num: int, den: int) -> str:
    """Format exact integer percentage to one decimal place without floats."""
    if den == 0:
        return "0.0%"
    tenths = (num * 1000) // den
    whole, frac = divmod(tenths, 10)
    return f"{whole}.{frac}%"


def _split_safe_parts(path: Path, label: str) -> list[str]:
    raw = str(path)
    require_ok("\x00" not in raw, "NUL_PATH", f"{label}: NUL byte in path")
    parts = list(path.parts)
    if path.is_absolute():
        parts = parts[1:]
    else:
        parts = list(Path(os.getcwd()).parts)[1:] + parts
    clean: list[str] = []
    for comp in parts:
        if comp in ("", "."):
            continue
        require_ok(comp != "..", "PATH_TRAVERSAL", f"{label}: '..' forbidden")
        clean.append(comp)
    return clean


def _read_regular_text(
    path: Path,
    label: str,
    *,
    allow_proc: bool = False,
    max_bytes: int = MAX_FILE_BYTES,
) -> str:
    comps = _split_safe_parts(path, label)
    require_ok(bool(comps), "NOT_REGULAR_FILE", f"{label}: root is not a file")
    dir_flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    cur_fd = os.open("/", dir_flags)
    for comp in comps[:-1]:
        try:
            nxt_fd = os.open(comp, dir_flags, dir_fd=cur_fd)
        except OSError as exc:
            os.close(cur_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise HudError("SYMLINK_FORBIDDEN", f"{label}: symlink ancestor {comp!r}") from exc
            if exc.errno == errno.ENOENT:
                raise HudError("FILE_NOT_FOUND", f"{label}: missing directory {comp!r}") from exc
            raise HudError("IO_ERROR", f"{label}: open dir failed: {exc}") from exc
        os.close(cur_fd)
        cur_fd = nxt_fd
    try:
        try:
            fd = os.open(comps[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=cur_fd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise HudError("SYMLINK_FORBIDDEN", f"{label}: symlink leaf forbidden") from exc
            if exc.errno == errno.ENOENT:
                raise HudError("FILE_NOT_FOUND", f"{label}: missing file") from exc
            raise HudError("IO_ERROR", f"{label}: open failed: {exc}") from exc
    finally:
        os.close(cur_fd)
    chunks: list[bytes] = []
    total = 0
    try:
        st = os.fstat(fd)
        if not allow_proc:
            require_ok(stat.S_ISREG(st.st_mode), "NOT_REGULAR_FILE", f"{label}: not a regular file")
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            require_ok(total <= max_bytes, "FILE_TOO_LARGE", f"{label}: exceeds size cap")
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise HudError("INVALID_UTF8", f"{label}: invalid UTF-8: {exc}") from exc


def loads_strict_json(raw_text: str, label: str) -> Any:
    require_ok(len(raw_text.strip()) > 0, "EMPTY_JSON", f"{label}: empty JSON")

    def reject_const(tok: str) -> Any:
        raise HudError("JSON_CONSTANT_FORBIDDEN", f"{label}: forbidden constant {tok!r}")

    def reject_float(tok: str) -> Any:
        raise HudError("JSON_FLOAT_FORBIDDEN", f"{label}: float literal forbidden: {tok!r}")

    def reject_dups(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            require_ok(k not in out, "JSON_DUPLICATE_KEY", f"{label}: duplicate key {k!r}")
            out[k] = v
        return out

    try:
        return json.loads(
            raw_text,
            parse_constant=reject_const,
            parse_float=reject_float,
            object_pairs_hook=reject_dups,
        )
    except HudError:
        raise
    except Exception as exc:
        raise HudError("JSON_SYNTAX_ERROR", f"{label}: {exc}") from exc


def parse_board_jsonl_text(text: str) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = []
    for idx, line in enumerate(text.splitlines(), start=1):
        s = line.strip()
        if not s:
            continue
        obj = loads_strict_json(s, f"board:{idx}")
        require_ok(type(obj) is dict, "BOARD_NOT_OBJECT", f"board:{idx}: expected JSON object")
        seq = obj.get("seq")
        require_ok(type(seq) is int and seq >= 1, "INVALID_BOARD_SEQ", f"board:{idx}: invalid seq")
        agent = obj.get("agent")
        require_ok(type(agent) is str, "INVALID_BOARD_AGENT", f"board:{idx}: invalid agent")
        body = obj.get("text")
        require_ok(type(body) is str, "INVALID_BOARD_TEXT", f"board:{idx}: invalid text")
        msgs.append(obj)
    return msgs


def _has_role(agent: str, roles: tuple[str, ...]) -> bool:
    low = agent.lower()
    return any(tok in low for tok in roles)


def compute_toroid_metrics(
    msgs: list[dict[str, Any]],
    *,
    atlas_text: str = "",
    window_size: int = 50,
    max_cartographer_gap: int = 80,
    max_weaver_gap: int = 120,
) -> dict[str, Any]:
    """Compute pure-integer sliding-window rotational transform iota_W and CS-01/CS-22/CS-23."""
    require_ok(type(window_size) is int and window_size >= 1, "INVALID_WINDOW", "window_size must be >= 1")
    require_ok(type(max_cartographer_gap) is int and max_cartographer_gap >= 1, "INVALID_GAP", "max_cartographer_gap >= 1")
    require_ok(type(max_weaver_gap) is int and max_weaver_gap >= 1, "INVALID_GAP", "max_weaver_gap >= 1")

    head_seq = max((m["seq"] for m in msgs), default=0)
    tail = msgs[-window_size:] if len(msgs) > window_size else msgs
    w_len = len(tail)
    n_poloidal = sum(1 for m in tail if _has_role(m["agent"], _POLOIDAL_ROLES))
    iota = Fraction(n_poloidal, w_len) if w_len > 0 else Fraction(0, 1)

    last_cart = max((m["seq"] for m in msgs if _has_role(m["agent"], _CARTOGRAPHER_ROLES)), default=0)
    last_weav = max((m["seq"] for m in msgs if _has_role(m["agent"], _WEAVER_ROLES)), default=0)
    last_fold = max(last_cart, last_weav)
    cart_gap = head_seq - last_cart
    weav_gap = head_seq - last_weav

    # CS-01: Continual rotational-transform & silence-gap status
    if (w_len >= 10 and n_poloidal == 0) or cart_gap > max_cartographer_gap or weav_gap > max_weaver_gap:
        cs01_status = "ALERT"
    elif (w_len > 0 and n_poloidal * 25 < w_len) or cart_gap * 2 > max_cartographer_gap:
        cs01_status = "WARNING"
    else:
        cs01_status = "CLEAR"

    # CS-22: Stranded verified tracks absent from atlas
    atlas_tracks = {"S" + m.group(1).lower() for m in _ATLAS_TRACK_RE.finditer(atlas_text)}
    board_tracks: set[str] = set()
    for m in msgs:
        if "VERIFIED" in m["text"] or "FROZEN" in m["text"].upper():
            for tm in _TRACK_RE.finditer(m["text"]):
                board_tracks.add("S" + tm.group(1).lower())
    stranded = sorted(board_tracks - atlas_tracks)
    if len(stranded) >= 3:
        cs22_status = "ALERT"
    elif len(stranded) >= 1:
        cs22_status = "WARNING"
    else:
        cs22_status = "CLEAR"

    # CS-23: Unfolded candidate signals after last poloidal fold
    unfolded_seqs: list[int] = []
    for m in msgs:
        if m["seq"] > last_fold and _SIGNAL_RE.search(m["text"]) is not None:
            unfolded_seqs.append(m["seq"])
    sig_digest = hashlib.sha256(",".join(str(s) for s in unfolded_seqs).encode("utf-8")).hexdigest()[:16]
    if len(unfolded_seqs) >= 10:
        cs23_status = "ALERT"
    elif len(unfolded_seqs) >= 3:
        cs23_status = "WARNING"
    else:
        cs23_status = "CLEAR"

    return {
        "head_seq": head_seq,
        "message_count": len(msgs),
        "window_size": window_size,
        "window_len": w_len,
        "poloidal_in_window": n_poloidal,
        "iota_w": f"{iota.numerator}/{iota.denominator}",
        "iota_pct": _fmt_ratio_pct(n_poloidal, w_len),
        "last_cartographer_seq": last_cart,
        "cartographer_gap": cart_gap,
        "last_weaver_seq": last_weav,
        "weaver_gap": weav_gap,
        "stranded_tracks": stranded,
        "unfolded_signal_count": len(unfolded_seqs),
        "unfolded_signal_sha256_16": sig_digest,
        "gates": {
            "CS-01": cs01_status,
            "CS-22": cs22_status,
            "CS-23": cs23_status,
        },
    }


def parse_meminfo_text(text: str) -> dict[str, Any]:
    """Parse Linux `/proc/meminfo` text into exact integer MiB telemetry."""
    kv: dict[str, int] = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        parts = rest.strip().split()
        if parts and _is_ascii_uint(parts[0]):
            kv[key.strip()] = int(parts[0])
    mem_total_kib = kv.get("MemTotal", 0)
    mem_avail_kib = kv.get("MemAvailable", kv.get("MemFree", 0))
    swap_total_kib = kv.get("SwapTotal", 0)
    swap_free_kib = kv.get("SwapFree", 0)
    mem_used_kib = max(0, mem_total_kib - mem_avail_kib)
    swap_used_kib = max(0, swap_total_kib - swap_free_kib)
    return {
        "mem_total_mib": mem_total_kib // 1024,
        "mem_used_mib": mem_used_kib // 1024,
        "mem_avail_mib": mem_avail_kib // 1024,
        "swap_total_mib": swap_total_kib // 1024,
        "swap_used_mib": swap_used_kib // 1024,
    }


def parse_nvidia_smi_output(text: str, *, expect_zero_desktop_vram: bool = False) -> dict[str, Any]:
    """Parse `nvidia-smi --query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu --format=csv,noheader,nounits`."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"present": False, "status": "none", "gpus": [], "ring0_vram_alert": False}
    gpus: list[dict[str, Any]] = []
    ring0_alert = False
    for ln in lines:
        cols = [c.strip() for c in ln.split(",")]
        require_ok(len(cols) == 5, "INVALID_GPU_CSV", f"expected 5 nvidia-smi columns, got {len(cols)}")
        name = cols[0]
        require_ok(
            _is_ascii_uint(cols[1]) and _is_ascii_uint(cols[2]) and _is_ascii_uint(cols[3]) and _is_ascii_uint(cols[4]),
            "INVALID_GPU_METRIC",
            f"non-integer nvidia-smi fields: {cols[1:]!r}",
        )
        vram_used = int(cols[1])
        vram_total = int(cols[2])
        temp_c = int(cols[3])
        util_pct = int(cols[4])
        if expect_zero_desktop_vram and vram_used > 0:
            ring0_alert = True
        gpus.append({
            "name": name,
            "vram_used_mib": vram_used,
            "vram_total_mib": vram_total,
            "temp_c": temp_c,
            "util_pct": util_pct,
        })
    return {
        "present": True,
        "status": "ALERT_RING0_VRAM" if ring0_alert else "OK",
        "gpus": gpus,
        "ring0_vram_alert": ring0_alert,
    }


def probe_gpu_live(*, expect_zero_desktop_vram: bool = False, nvidia_smi_bin: str = "nvidia-smi") -> dict[str, Any]:
    resolved = shutil.which(nvidia_smi_bin)
    if resolved is None:
        return {"present": False, "status": "none", "gpus": [], "ring0_vram_alert": False}
    proc = subprocess.run(
        [
            resolved,
            "--query-gpu=name,memory.used,memory.total,temperature.gpu,utilization.gpu",
            "--format=csv,noheader,nounits",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return {"present": False, "status": "none", "gpus": [], "ring0_vram_alert": False}
    return parse_nvidia_smi_output(proc.stdout, expect_zero_desktop_vram=expect_zero_desktop_vram)


def summarize_roster(members_obj: dict[str, Any]) -> dict[str, int]:
    members_map = members_obj.get("members")
    require_ok(type(members_map) is dict, "INVALID_MEMBERS_MAP", "members.json missing members dict")
    counts = {"total": 0, "working": 0, "waiting": 0, "stopped": 0, "killed": 0}
    for info in members_map.values():
        require_ok(type(info) is dict, "INVALID_MEMBER_ENTRY", "member entry must be dict")
        counts["total"] += 1
        phase = str(info.get("phase", "stopped")).lower()
        if info.get("killed") is True or phase == "killed":
            counts["killed"] += 1
        elif phase in ("working", "running"):
            wait_since = info.get("wait_since")
            if type(wait_since) is int and wait_since > 0:
                counts["waiting"] += 1
            else:
                counts["working"] += 1
        elif phase == "waiting":
            counts["waiting"] += 1
        else:
            counts["stopped"] += 1
    return counts


def auto_discover_swarm(gemini_dir: Path) -> tuple[Path, Path | None]:
    brain_dir = gemini_dir / "brain"
    candidates = sorted(brain_dir.glob("*/.system_generated/swarm/board.jsonl"))
    require_ok(len(candidates) >= 1, "SWARM_NOT_FOUND", f"no swarm board.jsonl found under {brain_dir}")
    board_p = candidates[-1]
    members_p = board_p.parent / "members.json"
    return board_p, members_p if members_p.is_file() else None


def collect_snapshot(
    *,
    board_path: Path,
    members_path: Path | None = None,
    atlas_path: Path | None = None,
    meminfo_path: Path = Path("/proc/meminfo"),
    gpu_csv_override: str | None = None,
    expect_zero_desktop_vram: bool = False,
    window_size: int = 50,
) -> dict[str, Any]:
    msgs = parse_board_jsonl_text(_read_regular_text(board_path, "board.jsonl"))
    atlas_text = _read_regular_text(atlas_path, "atlas.md") if atlas_path is not None else ""
    toroid = compute_toroid_metrics(msgs, atlas_text=atlas_text, window_size=window_size)
    roster = (
        summarize_roster(loads_strict_json(_read_regular_text(members_path, "members.json"), "members.json"))
        if members_path is not None
        else {"total": 0, "working": 0, "waiting": 0, "stopped": 0, "killed": 0}
    )
    mem_text = _read_regular_text(meminfo_path, "meminfo", allow_proc=True) if meminfo_path.exists() else ""
    memory = parse_meminfo_text(mem_text)
    gpu = (
        parse_nvidia_smi_output(gpu_csv_override, expect_zero_desktop_vram=expect_zero_desktop_vram)
        if gpu_csv_override is not None
        else probe_gpu_live(expect_zero_desktop_vram=expect_zero_desktop_vram)
    )
    return {
        "schema": SCHEMA,
        "status": "OK",
        "toroid": toroid,
        "roster": roster,
        "memory": memory,
        "gpu": gpu,
    }


def render_ansi_dashboard(snap: dict[str, Any]) -> str:
    t = snap["toroid"]
    g = t["gates"]
    r = snap["roster"]
    m = snap["memory"]
    gpu = snap["gpu"]
    gpu_line = (
        "GPU: none (CPU-only host)"
        if not gpu["present"]
        else " | ".join(
            f"{d['name']} VRAM={d['vram_used_mib']}/{d['vram_total_mib']} MiB {d['temp_c']}C util={d['util_pct']}%"
            for d in gpu["gpus"]
        )
        + f" [Ring0={gpu['status']}]"
    )
    return "\n".join([
        "\033[1;36m=== ALETHEIA CONTINUAL-TOROID HUD (ScreenPad Plus) ===\033[0m",
        f"Board Head: #{t['head_seq']} ({t['message_count']} msgs) | iota_W(W={t['window_len']}) = {t['iota_w']} ({t['iota_pct']})",
        f"Silence Gaps: cartographer={t['cartographer_gap']} (last #{t['last_cartographer_seq']}) | weaver={t['weaver_gap']} (last #{t['last_weaver_seq']})",
        f"Gates: CS-01={g['CS-01']} | CS-22={g['CS-22']} (stranded={len(t['stranded_tracks'])}) | CS-23={g['CS-23']} (unfolded={t['unfolded_signal_count']} sha={t['unfolded_signal_sha256_16']})",
        f"Roster: total={r['total']} working={r['working']} waiting={r['waiting']} stopped={r['stopped']} killed={r['killed']}",
        f"Memory: RAM={m['mem_used_mib']}/{m['mem_total_mib']} MiB (avail={m['mem_avail_mib']} MiB) | Swap={m['swap_used_mib']}/{m['swap_total_mib']} MiB",
        gpu_line,
    ])


def run_self_test() -> dict[str, Any]:
    """Deterministic zero-host-jitter self-test for 3-mode isolated parity."""
    sample_board = "\n".join([
        json.dumps({"seq": 1, "agent": "session-cartographer", "text": "Mapped Track S10 VERIFIED"}),
        json.dumps({"seq": 2, "agent": "peer-weaver", "text": "Wove Track S10 into atlas"}),
        json.dumps({"seq": 3, "agent": "verifier", "text": "Track S10 Schur check VERIFIED"}),
    ])
    sample_atlas = "# Unified Operator Atlas\nCovers Track S10.\n"
    sample_mem = "MemTotal:       32768000 kB\nMemAvailable:   24576000 kB\nSwapTotal:       8192000 kB\nSwapFree:        8192000 kB\n"
    sample_gpu_ok = "NVIDIA GeForce RTX 3080 Laptop GPU, 0, 8192, 42, 0\n"
    sample_gpu_bad = "NVIDIA GeForce RTX 3080 Laptop GPU, 148, 8192, 49, 4\n"

    msgs = parse_board_jsonl_text(sample_board)
    toroid = compute_toroid_metrics(msgs, atlas_text=sample_atlas, window_size=50)
    memory = parse_meminfo_text(sample_mem)
    gpu_ok = parse_nvidia_smi_output(sample_gpu_ok, expect_zero_desktop_vram=True)
    gpu_bad = parse_nvidia_smi_output(sample_gpu_bad, expect_zero_desktop_vram=True)
    gpu_none = parse_nvidia_smi_output("", expect_zero_desktop_vram=True)
    roster = summarize_roster({
        "members": {
            "coordinator": {"phase": "working", "killed": False},
            "session-cartographer": {"phase": "waiting", "killed": False},
            "retired": {"phase": "killed", "killed": True},
        }
    })
    return {
        "schema": SCHEMA,
        "mode": "self_test",
        "status": "PASS",
        "toroid": toroid,
        "roster": roster,
        "memory": memory,
        "gpu_ring0_ok": gpu_ok["status"],
        "gpu_ring0_violation_caught": gpu_bad["ring0_vram_alert"],
        "gpu_cpu_host_status": gpu_none["status"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Zero-float continual-toroid ScreenPad Plus HUD.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic self-test")
    parser.add_argument("--once", action="store_true", help="Emit single snapshot and exit")
    parser.add_argument("--watch", action="store_true", help="Run live ANSI refresh loop")
    parser.add_argument("--interval", type=int, default=5, help="Watch refresh interval in seconds")
    parser.add_argument("--max-iterations", type=int, default=0, help="Optional cap on watch iterations (0=unbounded)")
    parser.add_argument("--board", type=Path, default=None, help="Path to swarm board.jsonl")
    parser.add_argument("--members", type=Path, default=None, help="Optional path to swarm members.json")
    parser.add_argument("--atlas", type=Path, default=None, help="Optional path to UNIFIED_OPERATOR_ATLAS.md")
    parser.add_argument("--meminfo-path", type=Path, default=Path("/proc/meminfo"), help="Path to meminfo file")
    parser.add_argument("--auto-discover", action="store_true", help="Locate swarm board under --gemini-dir")
    parser.add_argument("--gemini-dir", type=Path, default=None, help="Path to antigravity-cli dir for auto-discover")
    parser.add_argument("--window", type=int, default=50, help="Sliding window size W for iota_W")
    parser.add_argument("--expect-zero-desktop-vram", action="store_true", help="Flag GPU VRAM > 0 MiB as Ring-0 alert")
    parser.add_argument("--json", action="store_true", help="Emit canonical JSON output")
    args = parser.parse_args(argv)

    run_st = args.self_test or (not args.once and not args.watch)
    try:
        if run_st:
            print(json.dumps(run_self_test(), indent=2, sort_keys=True))
            return 0
        board_p = args.board
        members_p = args.members
        if args.auto_discover:
            gdir = args.gemini_dir if args.gemini_dir is not None else Path.home() / ".gemini" / "antigravity-cli"
            board_p, disc_members = auto_discover_swarm(gdir)
            if members_p is None:
                members_p = disc_members
        require_ok(board_p is not None, "MISSING_BOARD", "--board or --auto-discover is required")
        require_ok(args.interval >= 1, "INVALID_INTERVAL", "--interval must be >= 1")
        require_ok(args.max_iterations >= 0, "INVALID_ITERATIONS", "--max-iterations must be >= 0")

        if args.once:
            snap = collect_snapshot(
                board_path=board_p,
                members_path=members_p,
                atlas_path=args.atlas,
                meminfo_path=args.meminfo_path,
                expect_zero_desktop_vram=args.expect_zero_desktop_vram,
                window_size=args.window,
            )
            if args.json:
                print(json.dumps(snap, indent=2, sort_keys=True))
            else:
                print(render_ansi_dashboard(snap))
            return 0

        iters = 0
        while True:
            snap = collect_snapshot(
                board_path=board_p,
                members_path=members_p,
                atlas_path=args.atlas,
                meminfo_path=args.meminfo_path,
                expect_zero_desktop_vram=args.expect_zero_desktop_vram,
                window_size=args.window,
            )
            print(render_ansi_dashboard(snap))
            iters += 1
            if args.max_iterations > 0 and iters >= args.max_iterations:
                return 0
            time.sleep(args.interval)
    except HudError as exc:
        err = {"schema": SCHEMA, "status": "ERROR", "code": exc.code, "message": exc.message}
        if args.json:
            print(json.dumps(err, indent=2, sort_keys=True))
        else:
            print(f"[toroid-hud] ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
