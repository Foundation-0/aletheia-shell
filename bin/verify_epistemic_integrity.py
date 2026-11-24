#!/usr/bin/env python3
"""
`verify_epistemic_integrity.py`: Fail-closed, pure-stdlib epistemic migration verifier.

Verifies a frozen or restored Antigravity + expedition migration bundle with zero
external dependencies, zero floats, zero bare asserts, symlink-refusing opens, and
bounded reads:
  1. Manifest SHA-256 completeness (`MIGRATION_SHA256SUMS`, zero missing/mismatched)
  2. SQLite database integrity (`PRAGMA integrity_check == 'ok'`, zero orphan `-wal`)
  3. Swarm `board.jsonl` strict JSONL & monotonically non-decreasing `seq` (`1..head`)
  4. Swarm `swarm.json` + `members.json` pairing (every non-killed member has its `.db`)

Usage:
  python3 -I -B bin/verify_epistemic_integrity.py [--self-test]
  python3 -I -B bin/verify_epistemic_integrity.py \\
      --manifest MIGRATION_SHA256SUMS \\
      --gemini-dir "$HOME/.gemini/antigravity-cli" \\
      --expedition-dir "$HOME/Projects/expedition" \\
      --cid "<conversation-id>" [--config-dir "$HOME/.gemini/config"] [--json]
"""
from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import stat
import sys
import tempfile
from typing import Any, Sequence
from urllib.parse import quote

SCHEMA = "aletheia.epistemic_integrity.v1"
MAX_TEXT_BYTES = 64 * 1024 * 1024
_CHUNK_BYTES = 65_536
_HEX_LOWER = frozenset("0123456789abcdef")
_AUTH_SKIP_RELS = frozenset({"gemini-antigravity-cli/installation_id"})


class IntegrityError(ValueError):
    """Structured fail-closed verification error (never stripped by python3 -O)."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def require_ok(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise IntegrityError(code, message)


def _is_sha256_hex(token: str) -> bool:
    return len(token) == 64 and all(ch in _HEX_LOWER for ch in token)


def _split_safe_parts(path: Path, label: str) -> list[str]:
    raw = str(path)
    require_ok("\x00" not in raw, "NUL_PATH", f"{label}: NUL byte in path")
    parts = list(path.parts)
    if path.is_absolute():
        parts = parts[1:]
    else:
        cwd = Path(os.getcwd())
        parts = list(cwd.parts)[1:] + parts
    clean: list[str] = []
    for comp in parts:
        if comp in ("", "."):
            continue
        require_ok(comp != "..", "PATH_TRAVERSAL", f"{label}: '..' component forbidden")
        clean.append(comp)
    return clean


def _open_dir_chain(comps: Sequence[str], label: str) -> int:
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
    cur_fd = os.open("/", flags)
    for comp in comps:
        try:
            nxt_fd = os.open(comp, flags, dir_fd=cur_fd)
        except OSError as exc:
            os.close(cur_fd)
            if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                raise IntegrityError("SYMLINK_FORBIDDEN", f"{label}: symlink or non-dir ancestor {comp!r}") from exc
            if exc.errno == errno.ENOENT:
                raise IntegrityError("PATH_NOT_FOUND", f"{label}: missing directory {comp!r}") from exc
            raise IntegrityError("IO_ERROR", f"{label}: cannot open directory {comp!r}: {exc}") from exc
        os.close(cur_fd)
        cur_fd = nxt_fd
    return cur_fd


def _open_regular_fd(path: Path, label: str) -> tuple[int, int]:
    comps = _split_safe_parts(path, label)
    require_ok(bool(comps), "NOT_REGULAR_FILE", f"{label}: root is not a regular file")
    dfd = _open_dir_chain(comps[:-1], label)
    try:
        try:
            fd = os.open(comps[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=dfd)
        except OSError as exc:
            if exc.errno == errno.ELOOP:
                raise IntegrityError("SYMLINK_FORBIDDEN", f"{label}: symlink file forbidden") from exc
            if exc.errno == errno.ENOENT:
                raise IntegrityError("FILE_NOT_FOUND", f"{label}: missing file {path.name!r}") from exc
            raise IntegrityError("IO_ERROR", f"{label}: open failed: {exc}") from exc
    finally:
        os.close(dfd)
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        os.close(fd)
        raise IntegrityError("NOT_REGULAR_FILE", f"{label}: target is not a regular file")
    return fd, st.st_size


def _read_text_safe(path: Path, label: str, max_bytes: int = MAX_TEXT_BYTES) -> str:
    fd, _ = _open_regular_fd(path, label)
    chunks: list[bytes] = []
    total = 0
    try:
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            require_ok(total <= max_bytes, "FILE_TOO_LARGE", f"{label}: exceeds {max_bytes} bytes")
            chunks.append(chunk)
    finally:
        os.close(fd)
    try:
        return b"".join(chunks).decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise IntegrityError("INVALID_UTF8", f"{label}: invalid UTF-8: {exc}") from exc


def _sha256_file_safe(path: Path, label: str) -> tuple[str, int]:
    fd, _ = _open_regular_fd(path, label)
    h = hashlib.sha256()
    total = 0
    try:
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            total += len(chunk)
            h.update(chunk)
    finally:
        os.close(fd)
    return h.hexdigest(), total


def _require_no_surrogates(obj: Any, label: str) -> None:
    stack: list[Any] = [obj]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            require_ok(
                not any(0xD800 <= ord(ch) <= 0xDFFF for ch in item),
                "LONE_SURROGATE",
                f"{label}: lone UTF-16 surrogate forbidden",
            )
        elif isinstance(item, dict):
            for k, v in item.items():
                stack.append(k)
                stack.append(v)
        elif isinstance(item, list):
            stack.extend(item)


def loads_strict_json(raw_text: str, label: str, *, allow_float: bool = False) -> Any:
    require_ok(len(raw_text.strip()) > 0, "EMPTY_JSON", f"{label}: empty JSON input")

    def reject_const(tok: str) -> Any:
        raise IntegrityError("JSON_CONSTANT_FORBIDDEN", f"{label}: non-standard constant {tok!r}")

    def reject_float(tok: str) -> Any:
        raise IntegrityError("JSON_FLOAT_FORBIDDEN", f"{label}: float literal forbidden: {tok!r}")

    def reject_dup_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, v in pairs:
            require_ok(k not in out, "JSON_DUPLICATE_KEY", f"{label}: duplicate key {k!r}")
            out[k] = v
        return out

    kwargs: dict[str, Any] = {
        "parse_constant": reject_const,
        "object_pairs_hook": reject_dup_keys,
    }
    if not allow_float:
        kwargs["parse_float"] = reject_float
    try:
        parsed = json.loads(raw_text, **kwargs)
    except IntegrityError:
        raise
    except Exception as exc:
        raise IntegrityError("JSON_SYNTAX_ERROR", f"{label}: {exc}") from exc
    _require_no_surrogates(parsed, label)
    return parsed


def _resolve_manifest_target(
    relpath: str,
    *,
    root_dir: Path | None,
    gemini_dir: Path,
    config_dir: Path | None,
    expedition_dir: Path,
) -> Path:
    require_ok(not relpath.startswith("/"), "MANIFEST_ABS_PATH", f"absolute manifest path forbidden: {relpath!r}")
    parts = Path(relpath).parts
    require_ok(len(parts) >= 1 and ".." not in parts, "MANIFEST_TRAVERSAL", f"invalid manifest path: {relpath!r}")
    if root_dir is not None:
        return root_dir / relpath
    prefix = parts[0]
    rest = Path(*parts[1:]) if len(parts) > 1 else Path(".")
    if prefix == "gemini-antigravity-cli":
        return gemini_dir / rest
    if prefix == "gemini-config":
        base_cfg = config_dir if config_dir is not None else gemini_dir.parent / "config"
        return base_cfg / rest
    if prefix == "expedition":
        return expedition_dir / rest
    raise IntegrityError("MANIFEST_UNKNOWN_PREFIX", f"unknown manifest root prefix {prefix!r} in {relpath!r}")


def verify_manifest_sha256(
    manifest_path: Path,
    *,
    gemini_dir: Path,
    expedition_dir: Path,
    config_dir: Path | None = None,
    root_dir: Path | None = None,
    skip_auth_files: bool = False,
) -> dict[str, Any]:
    """Verify every `<64-hex>  <relpath>` entry in MIGRATION_SHA256SUMS."""
    text = _read_text_safe(manifest_path, "manifest")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    require_ok(len(lines) >= 1, "EMPTY_MANIFEST", "manifest contains zero entries")
    seen: set[str] = set()
    checked = 0
    skipped = 0
    total_bytes = 0
    for idx, line in enumerate(lines, start=1):
        require_ok("  " in line, "MALFORMED_MANIFEST_LINE", f"manifest:{idx}: expected '<sha256>  <relpath>'")
        digest, relpath = line.split("  ", 1)
        require_ok(_is_sha256_hex(digest), "INVALID_MANIFEST_DIGEST", f"manifest:{idx}: invalid sha256 hex")
        require_ok(relpath not in seen, "DUPLICATE_MANIFEST_ENTRY", f"manifest:{idx}: duplicate path {relpath!r}")
        seen.add(relpath)
        if skip_auth_files and relpath in _AUTH_SKIP_RELS:
            skipped += 1
            continue
        target = _resolve_manifest_target(
            relpath,
            root_dir=root_dir,
            gemini_dir=gemini_dir,
            config_dir=config_dir,
            expedition_dir=expedition_dir,
        )
        actual, nbytes = _sha256_file_safe(target, f"manifest:{relpath}")
        require_ok(actual == digest, "SHA256_MISMATCH", f"sha256 mismatch for {relpath}")
        checked += 1
        total_bytes += nbytes
    return {"entries_checked": checked, "entries_skipped": skipped, "bytes_verified": total_bytes}


def _check_single_sqlite_db(db_path: Path, *, require_clean_wal: bool) -> None:
    fd, _ = _open_regular_fd(db_path, f"sqlite:{db_path.name}")
    os.close(fd)
    wal_path = Path(f"{db_path}-wal")
    if wal_path.exists() or wal_path.is_symlink():
        wal_fd, wal_size = _open_regular_fd(wal_path, f"sqlite-wal:{wal_path.name}")
        os.close(wal_fd)
        if require_clean_wal:
            require_ok(wal_size == 0, "ORPHAN_WAL_NONEMPTY", f"non-empty WAL file present: {wal_path.name}")
    uri = f"file:{quote(str(db_path))}?mode=ro"
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(uri, uri=True)
        rows = conn.execute("PRAGMA integrity_check;").fetchall()
    except sqlite3.DatabaseError as exc:
        raise IntegrityError("SQLITE_CORRUPT", f"SQLite integrity failure on {db_path.name}: {exc}") from exc
    finally:
        if conn is not None:
            conn.close()
    require_ok(rows == [("ok",)], "SQLITE_INTEGRITY_FAILED", f"SQLite integrity_check failed on {db_path.name}: {rows!r}")


def verify_sqlite_databases(gemini_dir: Path, *, require_clean_wal: bool = True) -> dict[str, Any]:
    """Verify every SQLite `.db` under `conversations/` and `conversation_summaries.db`."""
    conv_dir = gemini_dir / "conversations"
    dfd = _open_dir_chain(_split_safe_parts(conv_dir, "conversations"), "conversations")
    os.close(dfd)
    db_paths: list[Path] = sorted(conv_dir.glob("*.db"))
    summaries_db = gemini_dir / "conversation_summaries.db"
    if summaries_db.exists() or summaries_db.is_symlink():
        db_paths.append(summaries_db)
    require_ok(len(db_paths) >= 1, "NO_SQLITE_DBS", "no SQLite databases found under conversations/")
    for dbp in db_paths:
        _check_single_sqlite_db(dbp, require_clean_wal=require_clean_wal)
    return {"databases_verified": len(db_paths), "clean_wal_required": require_clean_wal}


def verify_board_jsonl(board_path: Path) -> dict[str, Any]:
    """Verify strict JSONL parsing and monotonically non-decreasing `seq` (`1..head`)."""
    text = _read_text_safe(board_path, "board.jsonl")
    require_ok(len(text) > 0, "EMPTY_BOARD", "board.jsonl is empty")
    require_ok(text.endswith("\n"), "TRUNCATED_BOARD_LINE", "board.jsonl missing trailing newline")
    lines = text.splitlines()
    prev_seq = 0
    for idx, line in enumerate(lines, start=1):
        require_ok(len(line.strip()) > 0, "BLANK_BOARD_LINE", f"board.jsonl:{idx}: blank line forbidden")
        obj = loads_strict_json(line, f"board.jsonl:{idx}", allow_float=False)
        require_ok(type(obj) is dict, "BOARD_NOT_OBJECT", f"board.jsonl:{idx}: line must be JSON object")
        seq = obj.get("seq")
        require_ok(type(seq) is int and seq >= 1, "INVALID_BOARD_SEQ", f"board.jsonl:{idx}: invalid seq")
        if idx == 1:
            require_ok(seq == 1, "BOARD_SEQ_START", f"board.jsonl:1: expected seq 1, got {seq}")
        else:
            require_ok(seq >= prev_seq, "BOARD_SEQ_REGRESSION", f"board.jsonl:{idx}: seq {seq} < prev {prev_seq}")
        require_ok(type(obj.get("agent")) is str, "INVALID_BOARD_AGENT", f"board.jsonl:{idx}: agent must be str")
        require_ok(type(obj.get("text")) is str, "INVALID_BOARD_TEXT", f"board.jsonl:{idx}: text must be str")
        prev_seq = seq
    return {"messages_verified": len(lines), "head_seq": prev_seq}


def verify_swarm_metadata(gemini_dir: Path, cid: str) -> dict[str, Any]:
    """Verify `swarm.json`, `members.json`, and member `conversation_id` `.db` files."""
    require_ok(len(cid.strip()) > 0 and "/" not in cid and ".." not in cid, "INVALID_CID", "invalid conversation id")
    swarm_dir = gemini_dir / "brain" / cid / ".system_generated" / "swarm"
    dfd = _open_dir_chain(_split_safe_parts(swarm_dir, "swarm_dir"), "swarm_dir")
    os.close(dfd)

    swarm_obj = loads_strict_json(_read_text_safe(swarm_dir / "swarm.json", "swarm.json"), "swarm.json")
    require_ok(type(swarm_obj) is dict, "INVALID_SWARM_JSON", "swarm.json must be JSON object")
    require_ok(type(swarm_obj.get("version")) is int, "INVALID_SWARM_VERSION", "swarm.json missing integer version")
    require_ok(type(swarm_obj.get("swarm_id")) is str and len(swarm_obj["swarm_id"]) > 0, "INVALID_SWARM_ID", "swarm.json missing swarm_id")

    members_obj = loads_strict_json(_read_text_safe(swarm_dir / "members.json", "members.json"), "members.json")
    require_ok(type(members_obj) is dict, "INVALID_MEMBERS_JSON", "members.json must be JSON object")
    members_map = members_obj.get("members")
    require_ok(type(members_map) is dict and len(members_map) >= 1, "INVALID_MEMBERS_MAP", "members.json missing members map")

    conv_dir = gemini_dir / "conversations"
    active_checked = 0
    killed_skipped = 0
    for name, info in sorted(members_map.items()):
        require_ok(type(info) is dict, "INVALID_MEMBER_ENTRY", f"member {name!r} entry must be dict")
        is_killed = info.get("killed") is True or info.get("phase") == "killed"
        if is_killed:
            killed_skipped += 1
            continue
        mcid = info.get("conversation_id")
        require_ok(
            type(mcid) is str and len(mcid) > 0 and "/" not in mcid and ".." not in mcid,
            "INVALID_MEMBER_CID",
            f"member {name!r} missing valid conversation_id",
        )
        db_file = conv_dir / f"{mcid}.db"
        fd, _ = _open_regular_fd(db_file, f"member_db:{name}")
        os.close(fd)
        active_checked += 1

    board_info = verify_board_jsonl(swarm_dir / "board.jsonl")
    return {
        "swarm_id": swarm_obj["swarm_id"],
        "active_members_verified": active_checked,
        "killed_members_skipped": killed_skipped,
        "board": board_info,
    }


def verify_epistemic_integrity(
    *,
    manifest_path: Path,
    gemini_dir: Path,
    expedition_dir: Path,
    cid: str,
    config_dir: Path | None = None,
    root_dir: Path | None = None,
    require_clean_wal: bool = True,
    skip_auth_files: bool = False,
) -> dict[str, Any]:
    """Run all 4 epistemic migration integrity checks fail-closed."""
    manifest_res = verify_manifest_sha256(
        manifest_path,
        gemini_dir=gemini_dir,
        expedition_dir=expedition_dir,
        config_dir=config_dir,
        root_dir=root_dir,
        skip_auth_files=skip_auth_files,
    )
    sqlite_res = verify_sqlite_databases(gemini_dir, require_clean_wal=require_clean_wal)
    swarm_res = verify_swarm_metadata(gemini_dir, cid)
    return {
        "schema": SCHEMA,
        "status": "PASS",
        "cid": cid,
        "manifest": manifest_res,
        "sqlite": sqlite_res,
        "swarm": swarm_res,
    }


def _build_fixture(root: Path, cid: str, worker_cid: str) -> tuple[Path, Path, Path, Path]:
    gem = root / "gemini-antigravity-cli"
    cfg = root / "gemini-config"
    exp = root / "expedition"
    conv = gem / "conversations"
    swarm = gem / "brain" / cid / ".system_generated" / "swarm"
    conv.mkdir(parents=True)
    swarm.mkdir(parents=True)
    cfg.mkdir(parents=True)
    exp.mkdir(parents=True)

    for db_name in (f"{cid}.db", f"{worker_cid}.db", "../conversation_summaries.db"):
        db_p = (conv / db_name).resolve()
        conn = sqlite3.connect(str(db_p))
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, val TEXT);")
        conn.execute("INSERT INTO t(val) VALUES ('ok');")
        conn.commit()
        conn.close()

    (swarm / "swarm.json").write_text(
        json.dumps({"version": 1, "backend": "file", "swarm_id": "sw-test"}) + "\n", encoding="utf-8"
    )
    (swarm / "members.json").write_text(
        json.dumps({
            "title": "Test Swarm",
            "members": {
                "coordinator": {"conversation_id": cid, "killed": False, "phase": "waiting"},
                "worker-a": {"conversation_id": worker_cid, "killed": False, "phase": "waiting"},
                "retired": {"conversation_id": "gone-cid", "killed": True, "phase": "killed"},
            },
        }) + "\n",
        encoding="utf-8",
    )
    (swarm / "board.jsonl").write_text(
        json.dumps({"seq": 1, "agent": "coordinator", "text": "start"}) + "\n"
        + json.dumps({"seq": 2, "agent": "worker-a", "text": "done"}) + "\n",
        encoding="utf-8",
    )
    (cfg / "settings.json").write_text('{"mode":"clean"}\n', encoding="utf-8")
    (exp / "README.md").write_text("# Expedition\n", encoding="utf-8")

    manifest_lines: list[str] = []
    for sub in sorted(root.rglob("*")):
        if sub.is_file():
            rel = sub.relative_to(root).as_posix()
            digest = hashlib.sha256(sub.read_bytes()).hexdigest()
            manifest_lines.append(f"{digest}  {rel}")
    manifest = root / "MIGRATION_SHA256SUMS"
    manifest.write_text("\n".join(manifest_lines) + "\n", encoding="utf-8")
    return manifest, gem, cfg, exp


def run_self_test() -> dict[str, Any]:
    """Deterministic self-test proving PASS on clean bundle and FAIL on 5 fault classes."""
    cid = "00000000-0000-4000-8000-000000000001"
    wcid = "00000000-0000-4000-8000-000000000002"
    controls: dict[str, str] = {}

    with tempfile.TemporaryDirectory(prefix="aletheia_ei_st_") as td:
        base = Path(td)
        clean_dir = base / "clean"
        manifest, gem, cfg, exp = _build_fixture(clean_dir, cid, wcid)
        clean_res = verify_epistemic_integrity(
            manifest_path=manifest, gemini_dir=gem, config_dir=cfg, expedition_dir=exp, cid=cid
        )

        # 1. Tampered SHA-256
        t1 = base / "tamper_sha"
        m1, g1, c1, e1 = _build_fixture(t1, cid, wcid)
        (e1 / "README.md").write_text("# Tampered\n", encoding="utf-8")
        try:
            verify_epistemic_integrity(manifest_path=m1, gemini_dir=g1, config_dir=c1, expedition_dir=e1, cid=cid)
        except IntegrityError as exc:
            controls["tampered_sha256"] = exc.code
        else:
            raise IntegrityError("SELF_TEST_FAILED", "tampered SHA-256 was not rejected")

        # 2. Corrupted SQLite DB
        t2 = base / "corrupt_db"
        _, g2, _, _ = _build_fixture(t2, cid, wcid)
        (g2 / "conversations" / f"{wcid}.db").write_bytes(b"NOT_A_VALID_SQLITE_DB_HEADER_BYTES")
        try:
            verify_sqlite_databases(g2)
        except IntegrityError as exc:
            controls["corrupted_sqlite"] = exc.code
        else:
            raise IntegrityError("SELF_TEST_FAILED", "corrupted SQLite DB was not rejected")

        # 3. Truncated board.jsonl
        t3 = base / "trunc_board"
        _, g3, _, _ = _build_fixture(t3, cid, wcid)
        (g3 / "brain" / cid / ".system_generated" / "swarm" / "board.jsonl").write_text(
            '{"seq":1,"agent":"coordinator","text":"ok"}\n{"seq":2,"agent":"w"', encoding="utf-8"
        )
        try:
            verify_board_jsonl(g3 / "brain" / cid / ".system_generated" / "swarm" / "board.jsonl")
        except IntegrityError as exc:
            controls["truncated_board"] = exc.code
        else:
            raise IntegrityError("SELF_TEST_FAILED", "truncated board.jsonl was not rejected")

        # 4. Missing member DB
        t4 = base / "missing_member_db"
        _, g4, _, _ = _build_fixture(t4, cid, wcid)
        (g4 / "conversations" / f"{wcid}.db").unlink()
        try:
            verify_swarm_metadata(g4, cid)
        except IntegrityError as exc:
            controls["missing_member_db"] = exc.code
        else:
            raise IntegrityError("SELF_TEST_FAILED", "missing member DB was not rejected")

        # 5. Symlinked input file
        t5 = base / "symlink_input"
        m5, g5, c5, e5 = _build_fixture(t5, cid, wcid)
        sym_m = t5 / "sym_manifest"
        sym_m.symlink_to(m5)
        try:
            verify_epistemic_integrity(manifest_path=sym_m, gemini_dir=g5, config_dir=c5, expedition_dir=e5, cid=cid)
        except IntegrityError as exc:
            controls["symlinked_input"] = exc.code
        else:
            raise IntegrityError("SELF_TEST_FAILED", "symlinked manifest was not rejected")

    return {
        "schema": SCHEMA,
        "mode": "self_test",
        "status": "PASS",
        "clean_verified": clean_res["status"] == "PASS",
        "entries_checked": clean_res["manifest"]["entries_checked"],
        "databases_verified": clean_res["sqlite"]["databases_verified"],
        "active_members_verified": clean_res["swarm"]["active_members_verified"],
        "fail_closed_controls": controls,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail-closed epistemic migration integrity verifier.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic self-test suite")
    parser.add_argument("--manifest", type=Path, default=None, help="Path to MIGRATION_SHA256SUMS")
    parser.add_argument("--root-dir", type=Path, default=None, help="Optional single staging payload root")
    parser.add_argument("--gemini-dir", type=Path, default=None, help="Path to antigravity-cli directory")
    parser.add_argument("--config-dir", type=Path, default=None, help="Path to .gemini/config directory")
    parser.add_argument("--expedition-dir", type=Path, default=None, help="Path to expedition directory")
    parser.add_argument("--cid", default=None, help="Primary coordinator conversation ID")
    parser.add_argument("--allow-wal", action="store_true", help="Allow non-empty SQLite WAL files")
    parser.add_argument("--skip-auth-files", action="store_true", help="Skip preserved auth identity files")
    parser.add_argument("--json", action="store_true", help="Emit canonical JSON output")
    args = parser.parse_args(argv)

    run_st = args.self_test or (
        args.manifest is None and args.gemini_dir is None and args.expedition_dir is None and args.cid is None
    )
    try:
        if run_st:
            res = run_self_test()
            print(json.dumps(res, indent=2, sort_keys=True))
            return 0
        require_ok(args.manifest is not None, "MISSING_ARG", "--manifest is required")
        require_ok(args.gemini_dir is not None, "MISSING_ARG", "--gemini-dir is required")
        require_ok(args.expedition_dir is not None, "MISSING_ARG", "--expedition-dir is required")
        require_ok(args.cid is not None, "MISSING_ARG", "--cid is required")
        res = verify_epistemic_integrity(
            manifest_path=args.manifest,
            root_dir=args.root_dir,
            gemini_dir=args.gemini_dir,
            config_dir=args.config_dir,
            expedition_dir=args.expedition_dir,
            cid=args.cid,
            require_clean_wal=not args.allow_wal,
            skip_auth_files=args.skip_auth_files,
        )
        if args.json:
            print(json.dumps(res, indent=2, sort_keys=True))
        else:
            print(
                f"[verify_epistemic_integrity] PASS cid={res['cid']} "
                f"files={res['manifest']['entries_checked']} "
                f"dbs={res['sqlite']['databases_verified']} "
                f"board_head={res['swarm']['board']['head_seq']}"
            )
        return 0
    except IntegrityError as exc:
        err = {"schema": SCHEMA, "status": "ERROR", "code": exc.code, "message": exc.message}
        if args.json:
            print(json.dumps(err, indent=2, sort_keys=True))
        else:
            print(f"[verify_epistemic_integrity] ERROR {exc.code}: {exc.message}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
