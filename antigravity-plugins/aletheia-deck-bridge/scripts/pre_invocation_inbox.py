#!/usr/bin/env python3
"""PreInvocation lifecycle hook for `aletheia-deck-bridge`.

Detects unread entries appended to `PI_INBOX.jsonl` or external edits to
`SHARED_WORKSPACE.md` from Aletheia Deck on ScreenPad Plus (`DP-3`) and injects
a concise, sanitized `[aletheia-deck shared workspace update]` systemMessage
into the Coordinator's turn.

Strictly sanctioned boundary: reads only user-owned workspace/state files and
the read-only `transcriptPath` supplied on stdin. Stdlib-only, zero bare asserts,
fast (<150 ms), and strictly fail-open `{}` on any I/O or parse error.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
import time
from typing import Any


MAX_INBOX_BYTES_PER_TURN = 32768
MAX_INBOX_ITEMS_PER_TURN = 25
MAX_ITEM_TEXT_CHARS = 1800
MAX_DOC_BYTES = 262144
MAX_DOC_EXCERPT_CHARS = 2800
MAX_ACK_IDS = 200

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_TAG_RE = re.compile(r"</?(?:SYSTEM_MESSAGE|USER_REQUEST|swarm_worker|script|img|iframe)[^>]*>", re.IGNORECASE)


def default_cartography_dir() -> Path:
    override = os.environ.get("ALETHEIA_CARTOGRAPHY_DIR", "").strip()
    if override:
        return Path(override)
    return Path.home() / "Projects" / "expedition" / "cartography"


def default_state_dir() -> Path:
    override = os.environ.get("ALETHEIA_DECK_STATE_DIR", "").strip()
    if override:
        return Path(override)
    xdg_state = os.environ.get("XDG_STATE_HOME", "").strip()
    if xdg_state:
        return Path(xdg_state) / "aletheia-deck"
    return Path.home() / ".local" / "state" / "aletheia-deck"


def sanitize_untrusted_text(raw: Any, *, max_len: int = MAX_ITEM_TEXT_CHARS) -> str:
    """Neutralize control chars and markup/tag spoofing while preserving readable text."""
    text = str(raw if raw is not None else "")
    text = _CTRL_RE.sub("", text)
    text = _TAG_RE.sub("[redacted-tag]", text)
    text = text.replace("<", "\u2039").replace(">", "\u203a")
    text = text.strip()
    if len(text) > max_len:
        text = text[: max_len - 16].rstrip() + " ... [truncated]"
    return text


def _atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        os.fchmod(fd, mode)
        raw = content.encode("utf-8")
        view = memoryview(raw)
        pos = 0
        while pos < len(view):
            n = os.write(fd, view[pos:])
            if n <= 0:
                raise OSError("short write")
            pos += n
        os.fsync(fd)
    except Exception:
        os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    else:
        os.close(fd)
        os.replace(str(tmp), str(path))


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 131072:
            return {}
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _is_swarm_worker_transcript(transcript_raw: Any, conv_id: str, state_dir: Path) -> bool:
    """Return True iff the transcript clearly belongs to a spawned swarm worker rather than Coordinator/PI."""
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return False
    cache_path = state_dir / "role_cache.json"
    cache = _read_json_file(cache_path)
    if conv_id and conv_id in cache and isinstance(cache[conv_id], bool):
        return cache[conv_id]

    tpath = Path(transcript_raw)
    if not tpath.is_file():
        return False
    is_worker = False
    try:
        with tpath.open("rb") as fh:
            head = fh.read(8192).decode("utf-8", errors="replace")
        if "You have just started. Your assignment (recorded on the board" in head:
            is_worker = True
        elif '"type": "USER_INPUT"' in head or '"type":"USER_INPUT"' in head:
            is_worker = False
    except OSError:
        return False

    if conv_id:
        if len(cache) > 64:
            cache = {}
        cache[conv_id] = is_worker
        try:
            _atomic_write_json(cache_path, cache)
        except OSError:
            pass
    return is_worker


def _read_offset_file(state_dir: Path) -> int:
    p = state_dir / "last_inbox_offset"
    try:
        raw = p.read_text(encoding="utf-8").strip()
        if raw.isdigit():
            return int(raw)
    except OSError:
        pass
    st = _read_json_file(state_dir / "bridge_state.json")
    off = st.get("last_inbox_offset", 0)
    return off if (type(off) is int and off >= 0) else 0


def _read_doc_sha_file(state_dir: Path) -> str:
    p = state_dir / "last_seen_doc_sha256"
    try:
        raw = p.read_text(encoding="utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{64}", raw):
            return raw
    except OSError:
        pass
    st = _read_json_file(state_dir / "bridge_state.json")
    sha = st.get("last_seen_doc_sha256", "")
    return sha if isinstance(sha, str) else ""


def _save_cursors(state_dir: Path, *, inbox_offset: int, doc_sha256: str) -> None:
    st = _read_json_file(state_dir / "bridge_state.json")
    st["schema"] = "aletheia-deck-bridge-state/v1"
    st["last_inbox_offset"] = inbox_offset
    st["last_seen_doc_sha256"] = doc_sha256
    st["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _atomic_write_json(state_dir / "bridge_state.json", st)
    _atomic_write_text(state_dir / "last_inbox_offset", f"{inbox_offset}\n")
    _atomic_write_text(state_dir / "last_seen_doc_sha256", f"{doc_sha256}\n")


def _update_ack(state_dir: Path, *, new_ids: list[str], inbox_offset: int, doc_sha256: str) -> None:
    ack_path = state_dir / "inbox_ack.json"
    ack = _read_json_file(ack_path)
    existing = ack.get("read_ids", [])
    if not isinstance(existing, list):
        existing = []
    seen = {str(x) for x in existing if isinstance(x, str)}
    merged = [str(x) for x in existing if isinstance(x, str)]
    for item_id in new_ids:
        if item_id and item_id not in seen:
            merged.append(item_id)
            seen.add(item_id)
    if len(merged) > MAX_ACK_IDS:
        merged = merged[-MAX_ACK_IDS:]
    payload = {
        "schema": "aletheia-deck-inbox-ack/v1",
        "last_read_offset": inbox_offset,
        "last_seen_doc_sha256": doc_sha256,
        "last_read_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "read_ids": merged,
    }
    _atomic_write_json(ack_path, payload)


def _read_new_inbox_entries(inbox_path: Path, start_offset: int) -> tuple[int, list[dict[str, Any]]]:
    if not inbox_path.is_file():
        return start_offset, []
    try:
        size = inbox_path.stat().st_size
    except OSError:
        return start_offset, []

    offset = 0 if size < start_offset else start_offset
    if size == offset:
        return offset, []

    items: list[dict[str, Any]] = []
    new_offset = offset
    bytes_read = 0
    try:
        with inbox_path.open("rb") as fh:
            fh.seek(offset)
            while len(items) < MAX_INBOX_ITEMS_PER_TURN and bytes_read < MAX_INBOX_BYTES_PER_TURN:
                line_start = fh.tell()
                raw_line = fh.readline(8192)
                if not raw_line:
                    break
                if not raw_line.endswith(b"\n"):
                    # Wait for writer to finish newline before consuming.
                    new_offset = line_start
                    break
                new_offset = fh.tell()
                bytes_read += len(raw_line)
                stripped = raw_line.decode("utf-8", errors="replace").strip()
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                except ValueError:
                    obj = {"kind": "note", "text": stripped}
                if isinstance(obj, dict):
                    items.append(obj)
                else:
                    items.append({"kind": "note", "text": str(obj)})
    except OSError:
        return start_offset, []
    return new_offset, items


def _extract_workspace_sections(text: str) -> str:
    """Return a concise sanitized excerpt of PI-editable sections from SHARED_WORKSPACE.md."""
    focus_headers = (
        "## Shared Canvas",
        "## PI Quick Notes",
        "## Active Track Priorities",
    )
    sections: list[str] = []
    cur_header = ""
    cur_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if cur_header and any(cur_header.startswith(h) for h in focus_headers):
                body = "\n".join(cur_lines).strip()
                if body:
                    sections.append(f"{cur_header}\n{body}")
            cur_header = line.strip()
            cur_lines = []
        elif cur_header:
            cur_lines.append(line)
    if cur_header and any(cur_header.startswith(h) for h in focus_headers):
        body = "\n".join(cur_lines).strip()
        if body:
            sections.append(f"{cur_header}\n{body}")
    excerpt = "\n\n".join(sections) if sections else text.strip()
    return sanitize_untrusted_text(excerpt, max_len=MAX_DOC_EXCERPT_CHARS)


def process_event(
    payload: dict[str, Any],
    *,
    cartography_dir: Path | None = None,
    state_dir: Path | None = None,
) -> dict[str, Any]:
    cdir = cartography_dir if cartography_dir is not None else default_cartography_dir()
    sdir = state_dir if state_dir is not None else default_state_dir()
    sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

    conv_id = str(payload.get("conversationId") or "").strip()
    if _is_swarm_worker_transcript(payload.get("transcriptPath"), conv_id, sdir):
        return {}

    inbox_path = cdir / "PI_INBOX.jsonl"
    doc_path = cdir / "SHARED_WORKSPACE.md"

    old_offset = _read_offset_file(sdir)
    old_doc_sha = _read_doc_sha_file(sdir)

    new_offset, new_items = _read_new_inbox_entries(inbox_path, old_offset)

    cur_doc_sha = old_doc_sha
    doc_changed = False
    doc_text = ""
    if doc_path.is_file():
        try:
            raw_bytes = doc_path.read_bytes()[:MAX_DOC_BYTES]
            cur_doc_sha = hashlib.sha256(raw_bytes).hexdigest()
            if old_doc_sha and cur_doc_sha != old_doc_sha:
                doc_changed = True
                doc_text = raw_bytes.decode("utf-8", errors="replace")
            elif not old_doc_sha:
                # First-ever observation establishes baseline without spurious alert.
                doc_changed = False
        except OSError:
            pass

    if not new_items and not doc_changed:
        if new_offset != old_offset or cur_doc_sha != old_doc_sha:
            _save_cursors(sdir, inbox_offset=new_offset, doc_sha256=cur_doc_sha)
        return {}

    parts: list[str] = [
        "[aletheia-deck shared workspace update] Live input arrived from the PI on ScreenPad Plus (`aletheia-deck`)."
    ]
    ack_ids: list[str] = []
    if new_items:
        parts.append(f"Unread PI_INBOX.jsonl entries ({len(new_items)}):")
        for idx, item in enumerate(new_items, 1):
            item_id = str(item.get("id") or f"off-{new_offset}-{idx}")
            ack_ids.append(item_id)
            kind = sanitize_untrusted_text(item.get("kind") or "message", max_len=40)
            ts = sanitize_untrusted_text(item.get("ts_utc") or "", max_len=32)
            text = sanitize_untrusted_text(item.get("text") or "", max_len=MAX_ITEM_TEXT_CHARS)
            prefix = f"- [{kind}" + (f" @ {ts}" if ts else "") + f"] (id={sanitize_untrusted_text(item_id, max_len=48)}): "
            parts.append(prefix + text)

    if doc_changed:
        parts.append(f"SHARED_WORKSPACE.md updated by PI (sha256={cur_doc_sha[:16]}...):")
        parts.append(_extract_workspace_sections(doc_text))
        parts.append(
            "Please acknowledge/act on these notes (capturing any research insights under SOP-12 "
            "and updating `## Coordinator Live Pinboard` in `cartography/SHARED_WORKSPACE.md` when appropriate)."
        )

    _save_cursors(sdir, inbox_offset=new_offset, doc_sha256=cur_doc_sha)
    _update_ack(sdir, new_ids=ack_ids, inbox_offset=new_offset, doc_sha256=cur_doc_sha)

    return {
        "injectSteps": [
            {
                "systemMessage": {
                    "systemMessage": "\n".join(parts),
                }
            }
        ]
    }


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            return 0
        result = process_event(payload)
        sys.stdout.write(json.dumps(result, separators=(",", ":")) + "\n")
    except Exception:
        sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
