#!/usr/bin/env python3
"""PostToolUse lifecycle hook for `aletheia-deck-bridge`.

Syncs live swarm board messages, worker roster, EXPERIMENTATION_QUEUE.md cards,
SHARED_WORKSPACE.md metadata, and `mlos gpu-lease` status atomically into
`~/.local/state/aletheia-deck/swarm_feed.json` so `aletheia-deck` on ScreenPad
Plus stays live without needing Alt+S.

Also suppresses Coordinator self-edit echoes on `SHARED_WORKSPACE.md` by
updating `last_seen_doc_sha256` whenever `edit_file` / `write_file` modifies it.

Strictly sanctioned boundary: read-only on `transcriptPath` tail, user-owned
cartography files, and `mlos` lease status. Stdlib-only, zero bare asserts,
fast (<200 ms), fail-open `{}` on any error.
"""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import sys
import time
from typing import Any, Iterator


TAIL_READ_BYTES = 786432
MAX_FEED_MESSAGES = 80
MAX_MSG_PREVIEW = 360
MAX_MSG_FULL = 4000
MAX_DOC_BYTES = 262144

_TRUNCATED_PATH_RE = re.compile(r"file://(/[^ \n\r]+/output\.txt)")
_GPU_RE = re.compile(r"\b(RTX\s*3080|GPU|cuda|sm_86|mlos\s+gpu-lease|VRAM)\b", re.IGNORECASE)
_AUDIT_RE = re.compile(r"\b(AUDIT-\d+|PREREG-|CS-2[0-9]|FALSIFIED|CONFIRMED|audit-check)\b", re.IGNORECASE)
_DONE_RE = re.compile(r"(\[done\]|\bSHIPPED\b|\bCOMPLETE\b|\bMERGED\b)", re.IGNORECASE)
_BACKTICK_BOLD_RE = re.compile(r"[*`]+")


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


@contextmanager
def _state_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, mode=0o700, exist_ok=True)
    lock_path = state_dir / ".bridge.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _fsync_dir(directory: Path) -> None:
    dir_fd = -1
    try:
        dir_fd = os.open(str(directory), os.O_RDONLY | os.O_DIRECTORY)
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        if dir_fd >= 0:
            os.close(dir_fd)


def _atomic_write_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    closed = False
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
        os.close(fd)
        closed = True
        os.replace(str(tmp), str(path))
        _fsync_dir(path.parent)
    except Exception:
        if not closed:
            os.close(fd)
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=0o600)


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        if not path.is_file() or path.stat().st_size > 2 * 1024 * 1024:
            return {}
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _strip_md(text: str) -> str:
    return _BACKTICK_BOLD_RE.sub("", text).strip()


def _classify_message(text: str) -> list[str]:
    tags: list[str] = []
    if _DONE_RE.search(text):
        tags.append("done")
    if _GPU_RE.search(text):
        tags.append("gpu")
    if _AUDIT_RE.search(text):
        tags.append("audit")
    return tags


def _normalize_message(seq: int, agent: str, text: str, ts: str) -> dict[str, Any]:
    clean_full = text.strip()
    if len(clean_full) > MAX_MSG_FULL:
        clean_full = clean_full[: MAX_MSG_FULL - 16].rstrip() + " ... [truncated]"
    first_line = clean_full.splitlines()[0] if clean_full else ""
    preview = _strip_md(first_line)
    if len(preview) > MAX_MSG_PREVIEW:
        preview = preview[: MAX_MSG_PREVIEW - 3].rstrip() + "..."
    return {
        "seq": seq,
        "agent": str(agent or "unknown")[:48],
        "time": str(ts or "")[:32],
        "preview": preview,
        "text": clean_full,
        "tags": _classify_message(clean_full),
    }


def _extract_json_objects_from_generic(content: str) -> list[dict[str, Any]]:
    """Extract JSON payload objects from a GENERIC step output (and truncated output.txt if referenced)."""
    objs: list[dict[str, Any]] = []
    texts_to_scan = [content]
    match = _TRUNCATED_PATH_RE.search(content)
    if match:
        p = Path(match.group(1))
        try:
            if p.is_file() and p.stat().st_size <= 512 * 1024:
                texts_to_scan.append(p.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            pass

    for txt in texts_to_scan:
        for line in txt.splitlines():
            s = line.strip()
            if not (s.startswith("{") and s.endswith("}")):
                continue
            try:
                parsed = json.loads(s)
            except ValueError:
                continue
            if isinstance(parsed, dict):
                objs.append(parsed)
    return objs


def _decode_maybe_json_string(val: Any) -> str:
    if not isinstance(val, str):
        return str(val or "")
    s = val.strip()
    if len(s) >= 2 and s.startswith('"') and s.endswith('"'):
        try:
            decoded = json.loads(s)
            if isinstance(decoded, str):
                return decoded
        except ValueError:
            pass
    return val


def parse_transcript_tail(
    transcript_path: Path,
    *,
    existing_messages: dict[int, dict[str, Any]],
    existing_workers: dict[str, dict[str, Any]],
    existing_title: str = "",
) -> tuple[str, int, dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Read-only tail scan of `transcript.jsonl` extracting swarm board messages & worker roster."""
    messages = dict(existing_messages)
    workers = dict(existing_workers)
    title = existing_title
    head = max(messages.keys(), default=0)

    if not transcript_path.is_file():
        return title, head, messages, workers

    try:
        size = transcript_path.stat().st_size
        start = max(0, size - TAIL_READ_BYTES)
        with transcript_path.open("rb") as fh:
            fh.seek(start)
            raw = fh.read(TAIL_READ_BYTES)
    except OSError:
        return title, head, messages, workers

    lines = raw.decode("utf-8", errors="replace").splitlines()
    if start > 0 and lines:
        lines = lines[1:]

    pending_posts: list[tuple[str, str]] = []  # (text, created_at)
    for line in lines:
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if not isinstance(entry, dict):
            continue

        etype = entry.get("type")
        created_at = str(entry.get("created_at") or "")
        if etype == "PLANNER_RESPONSE":
            tcs = entry.get("tool_calls")
            if isinstance(tcs, list):
                for tc in tcs:
                    if not isinstance(tc, dict):
                        continue
                    tname = tc.get("name")
                    targs = tc.get("args") if isinstance(tc.get("args"), dict) else {}
                    if tname == "post_message":
                        txt = _decode_maybe_json_string(targs.get("text", ""))
                        if txt:
                            pending_posts.append((txt, created_at))
                    elif tname == "swarm":
                        act = _decode_maybe_json_string(targs.get("action", ""))
                        wname = _decode_maybe_json_string(targs.get("name", ""))
                        if act == "add" and wname:
                            wmodel = _decode_maybe_json_string(targs.get("model", "")) or "default"
                            workers[wname] = {
                                "name": wname,
                                "state": "working",
                                "model": wmodel,
                                "last_post_seq": workers.get(wname, {}).get("last_post_seq", 0),
                                "last_post_time": created_at,
                            }
                        elif act == "kill" and wname and wname in workers:
                            workers[wname]["state"] = "killed"
        elif etype == "GENERIC":
            content = entry.get("content")
            if not isinstance(content, str):
                continue
            for obj in _extract_json_objects_from_generic(content):
                if isinstance(obj.get("messages"), list):
                    obj_head = obj.get("head")
                    if type(obj_head) is int and obj_head > head:
                        head = obj_head
                    for m in obj["messages"]:
                        if not isinstance(m, dict):
                            continue
                        seq = m.get("seq")
                        if type(seq) is int and seq > 0:
                            agent = str(m.get("agent") or "unknown")
                            msg_time = str(m.get("time") or created_at)
                            messages[seq] = _normalize_message(seq, agent, str(m.get("text") or ""), msg_time)
                            if seq > head:
                                head = seq
                            if agent != "coordinator":
                                cur_w = workers.get(agent, {"name": agent, "state": "waiting", "model": ""})
                                if seq >= int(cur_w.get("last_post_seq") or 0):
                                    cur_w["last_post_seq"] = seq
                                    cur_w["last_post_time"] = msg_time
                                workers[agent] = cur_w
                elif isinstance(obj.get("members"), list):
                    if isinstance(obj.get("title"), str) and obj["title"]:
                        title = obj["title"]
                    obj_head = obj.get("head")
                    if type(obj_head) is int and obj_head > head:
                        head = obj_head
                    for mem in obj["members"]:
                        if not isinstance(mem, dict):
                            continue
                        wname = str(mem.get("name") or "")
                        if not wname or mem.get("killed") is True:
                            if wname in workers and mem.get("killed") is True:
                                workers.pop(wname, None)
                            continue
                        workers[wname] = {
                            "name": wname,
                            "state": str(mem.get("state") or "waiting"),
                            "model": str(mem.get("model_display_name") or mem.get("model") or ""),
                            "last_post_seq": int(mem.get("last_post_seq") or 0),
                            "last_post_time": str(mem.get("last_post_time") or ""),
                        }
                elif obj.get("status") == "posted" and type(obj.get("seq")) is int:
                    seq = obj["seq"]
                    if seq > head:
                        head = seq
                    if pending_posts:
                        txt, ts = pending_posts.pop(0)
                        if seq not in messages:
                            messages[seq] = _normalize_message(seq, "coordinator", txt, ts)

    return title, head, messages, workers


def parse_experimentation_queue(queue_path: Path) -> list[dict[str, Any]]:
    """Parse `cartography/EXPERIMENTATION_QUEUE.md` markdown table rows into UI cards."""
    if not queue_path.is_file():
        return []
    try:
        raw = queue_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    cards: list[dict[str, Any]] = []
    for line in raw.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 6:
            continue
        qid = _strip_md(cells[0])
        if not qid.startswith("EXP-Q"):
            continue
        status_raw = _strip_md(cells[5])
        su = status_raw.upper()
        if "RUNNING" in su:
            kind = "running"
        elif "QUEUED" in su or "PREFLIGHT" in su:
            kind = "queued"
        elif "SHIPPED" in su or "LIVE" in su:
            kind = "shipped"
        elif "COMPLETE" in su:
            kind = "complete"
        else:
            kind = "other"
        cards.append(
            {
                "id": qid,
                "date": _strip_md(cells[1]),
                "insight": _strip_md(cells[2])[:260],
                "protocol": _strip_md(cells[3])[:140],
                "workers": _strip_md(cells[4])[:120],
                "status": status_raw[:160],
                "status_kind": kind,
            }
        )
    return cards


def read_gpu_lease_status() -> dict[str, Any]:
    """Read compact RTX 3080 lease state with ZERO GPU touch."""
    mlos_bin = shutil.which("mlos") or str(Path.home() / ".local" / "bin" / "mlos")
    if Path(mlos_bin).is_file() and os.access(mlos_bin, os.X_OK):
        try:
            env = dict(os.environ)
            env["CUDA_VISIBLE_DEVICES"] = ""
            proc = subprocess.run(
                [mlos_bin, "gpu-lease", "--status"],
                env=env,
                capture_output=True,
                text=True,
                timeout=0.8,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                parsed = json.loads(proc.stdout.strip())
                if isinstance(parsed, dict):
                    return {
                        "held": bool(parsed.get("held", False)),
                        "holder": parsed.get("holder"),
                        "source": "mlos",
                    }
        except Exception:
            pass
    return {"held": False, "holder": None, "source": "unavailable"}


def _is_worker_transcript(transcript_raw: Any) -> bool:
    if not isinstance(transcript_raw, str) or not transcript_raw.strip():
        return False
    p = Path(transcript_raw)
    if not p.is_file():
        return False
    try:
        with p.open("rb") as fh:
            head = fh.read(16384).decode("utf-8", errors="replace")
        return (
            "You have just started. Your assignment (recorded on the board" in head
            or "<swarm_worker>" in head
        )
    except OSError:
        return False


def _sync_coordinator_doc_hash(payload: dict[str, Any], cartography_dir: Path, state_dir: Path) -> None:
    """If the Coordinator itself just edited SHARED_WORKSPACE.md, record its new hash so PreInvocation won't echo it."""
    if _is_worker_transcript(payload.get("transcriptPath")):
        return
    tc = payload.get("toolCall")
    if not isinstance(tc, dict):
        return
    tname = str(tc.get("name") or "")
    if tname not in ("edit_file", "write_file"):
        return
    args = tc.get("args")
    if not isinstance(args, dict):
        return
    target = str(args.get("Path") or args.get("path") or "")
    doc_path = (cartography_dir / "SHARED_WORKSPACE.md").resolve()
    try:
        if not target or Path(target).resolve() != doc_path:
            return
    except OSError:
        return
    if not doc_path.is_file():
        return
    try:
        with _state_lock(state_dir):
            digest = hashlib.sha256(doc_path.read_bytes()[:MAX_DOC_BYTES]).hexdigest()
            st = _read_json_file(state_dir / "bridge_state.json")
            st["last_seen_doc_sha256"] = digest
            st["updated_utc"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            _atomic_write_json(state_dir / "bridge_state.json", st)
            _atomic_write_text(state_dir / "last_seen_doc_sha256", f"{digest}\n")
    except OSError:
        pass


def sync_deck_state(
    payload: dict[str, Any],
    *,
    cartography_dir: Path | None = None,
    state_dir: Path | None = None,
    skip_mlos: bool = False,
) -> dict[str, Any]:
    cdir = cartography_dir if cartography_dir is not None else default_cartography_dir()
    sdir = state_dir if state_dir is not None else default_state_dir()
    sdir.mkdir(parents=True, mode=0o700, exist_ok=True)

    _sync_coordinator_doc_hash(payload, cdir, sdir)

    feed_path = sdir / "swarm_feed.json"
    prev = _read_json_file(feed_path)

    existing_msgs: dict[int, dict[str, Any]] = {}
    for m in prev.get("messages", []) if isinstance(prev.get("messages"), list) else []:
        if isinstance(m, dict) and type(m.get("seq")) is int:
            existing_msgs[m["seq"]] = m

    existing_workers: dict[str, dict[str, Any]] = {}
    for w in prev.get("workers", []) if isinstance(prev.get("workers"), list) else []:
        if isinstance(w, dict) and isinstance(w.get("name"), str):
            existing_workers[w["name"]] = w

    title = str(prev.get("title") or "Aletheia Swarm")
    head = int(prev.get("head") or 0)

    transcript_raw = payload.get("transcriptPath")
    if isinstance(transcript_raw, str) and transcript_raw.strip():
        tpath = Path(transcript_raw)
        title, parsed_head, existing_msgs, existing_workers = parse_transcript_tail(
            tpath,
            existing_messages=existing_msgs,
            existing_workers=existing_workers,
            existing_title=title,
        )
        if parsed_head > head:
            head = parsed_head

    # Keep latest MAX_FEED_MESSAGES sorted ascending by seq.
    sorted_seqs = sorted(existing_msgs.keys())[-MAX_FEED_MESSAGES:]
    msg_list = [existing_msgs[s] for s in sorted_seqs]

    # Sort workers: working first, then alphabetical.
    state_rank = {"working": 0, "waiting": 1, "stopped": 2, "killed": 3}
    worker_list = sorted(
        (w for w in existing_workers.values() if w.get("state") != "killed"),
        key=lambda w: (state_rank.get(str(w.get("state")), 9), str(w.get("name", ""))),
    )

    queue_cards = parse_experimentation_queue(cdir / "EXPERIMENTATION_QUEUE.md")
    gpu_lease = {"held": False, "holder": None, "source": "skipped"} if skip_mlos else read_gpu_lease_status()

    doc_path = cdir / "SHARED_WORKSPACE.md"
    doc_sha = ""
    if doc_path.is_file():
        try:
            doc_sha = hashlib.sha256(doc_path.read_bytes()[:MAX_DOC_BYTES]).hexdigest()
        except OSError:
            pass

    ack = _read_json_file(sdir / "inbox_ack.json")
    feed = {
        "schema": "aletheia-deck-swarm-feed/v1",
        "updated_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "title": title,
        "head": head,
        "workers": worker_list,
        "messages": msg_list,
        "queue": queue_cards,
        "gpu_lease": gpu_lease,
        "shared_workspace_sha256": doc_sha,
        "inbox_ack": {
            "last_read_utc": ack.get("last_read_utc"),
            "read_count": len(ack.get("read_ids", [])) if isinstance(ack.get("read_ids"), list) else 0,
        },
    }
    _atomic_write_json(feed_path, feed)
    return feed


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload: dict[str, Any] = {}
        if raw.strip():
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                payload = parsed
        sync_deck_state(payload)
    except Exception:
        pass
    sys.stdout.write("{}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
