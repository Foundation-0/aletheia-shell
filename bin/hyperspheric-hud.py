#!/usr/bin/env python3
"""Aletheion S^3 Hyperspheric Epistemic Telemetry HUD (`bin/hyperspheric-hud.py`).

Pure-Python-stdlib, zero-float S^3 Hyperspheric epistemic integration HUD complementing
`bin/toroid-hud.py`. Ingests an epistemic web index JSON, message board JSONL, member
roster JSON, optional host-contention environment receipts, and optional read-only
hybrid epistemic RAG SQLite databases, emitting canonical JSON or a compact 4-line
plain-text summary formatted for a ScreenPad Plus terminal pane.

Policy invariant: zero IEEE-754 float literals in AST, zero bare asserts, and strict
fail-closed rejection of uncontrolled laptop energy/wattage claims (temperature stability
and switch-chatter telemetry only).
"""

from __future__ import annotations

import argparse
from fractions import Fraction
import hashlib
import hmac
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
from typing import Any, Dict, FrozenSet, List, Optional, Sequence, Tuple
import unicodedata

SCHEMA_HUD = "aletheion.hyperspheric_hud.v1"
TELEMETRY_SCOPE_POLICY = "TEMPERATURE_AND_CHATTER_ONLY"
MAX_JSON_DEPTH = 32
MAX_INT_ABS = 10**18

DEFAULT_POLOIDAL_ROLES: FrozenSet[str] = frozenset({"cartographer", "weaver", "compiler", "archivist"})
_ALLOWED_POWER_BANDS: FrozenSet[str] = frozenset({"ac", "battery", "unknown"})
_HEX64_RE = re.compile(r"^[0-9a-f]{64}$")

_FORBIDDEN_KEY_TOKENS: FrozenSet[str] = frozenset(
    {
        "w",
        "j",
        "watt",
        "watts",
        "watthour",
        "watthours",
        "whr",
        "whrs",
        "pw",
        "nw",
        "uw",
        "mw",
        "kw",
        "gw",
        "tw",
        "wh",
        "kwh",
        "mwh",
        "gwh",
        "twh",
        "pj",
        "nj",
        "uj",
        "mj",
        "kj",
        "gj",
        "tj",
        "joule",
        "joules",
        "picowatt",
        "picowatts",
        "nanowatt",
        "nanowatts",
        "microwatt",
        "microwatts",
        "milliwatt",
        "milliwatts",
        "kilowatt",
        "kilowatts",
        "megawatt",
        "megawatts",
        "gigawatt",
        "gigawatts",
        "terawatt",
        "terawatts",
        "kilojoule",
        "kilojoules",
        "millijoule",
        "millijoules",
        "microjoule",
        "microjoules",
        "megajoule",
        "megajoules",
        "tdp",
        "pwr",
        "power",
        "energy",
        "wattage",
    }
)
_FORBIDDEN_COMPACT_KEY_RE = re.compile(
    r"(?:watt|joule|energy|power|tdp|pwr|kwh|mwh|gwh|twh|whr|mjpertok|jpertok|ujpertok|wpertok|whpertok)"
)
_PFX = r"(?:p|n|u|\u03bc|m|k|g|t|pico|nano|micro|milli|kilo|mega|giga|tera)"
_UNIT = r"(?:watt[^a-z0-9]*hours?|watthours?|watts?|joules?|whrs?|wh|w|j|tdp)"
_SEP = r"[^a-z0-9]*"
_NUM = r"\d[\d.,]*(?:e[+-]?\d+)?"
_FORBIDDEN_VALUE_RE = re.compile(
    "(?:"
    rf"{_NUM}{_SEP}(?:{_PFX}{_SEP})?{_UNIT}(?![a-z0-9])(?!/\s)"
    rf"|(?<![a-z0-9])(?:{_PFX}{_SEP})?{_UNIT}\s*[:=_]\s*{_NUM}"
    rf"|(?<![a-z0-9])tdp[^a-z0-9]*{_NUM}"
    rf"|^[^a-z0-9]*(?:{_PFX}{_SEP})?{_UNIT}[^a-z0-9]*$"
    r"|\b(?:joules?|watts?|mj|uj|kwh|wh)\s*(?:per|/)\s*tok(?:en)?s?\b"
    r"|\b(?:less|lower|reduced|fewer)\s+(?:in\s+)?(?:energy|power)\b"
    r"|\b(?:energy|power)\s+(?:use|usage|reduction|savings?|dropped|consumption|draw)\b"
    ")"
)


class HypersphericHudError(ValueError):
    """Fail-closed error for Aletheion Hyperspheric HUD."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"[{code}] {detail}")


def require_ok(cond: bool, code: str, detail: str) -> None:
    if not cond:
        raise HypersphericHudError(code, detail)


def is_hex64(val: Any) -> bool:
    return type(val) is str and _HEX64_RE.fullmatch(val) is not None


def check_nonneg_int(val: Any, name: str) -> int:
    require_ok(type(val) is int and 0 <= val <= MAX_INT_ABS, "BAD_NONNEG_INT", f"{name} must be non-negative int")
    return val


def frac_str(x: Fraction) -> str:
    return f"{x.numerator}/{x.denominator}"


def fmt_pct_tenths(num: int, den: int) -> str:
    """Exact integer percentage to one decimal place (zero floats)."""
    n = check_nonneg_int(num, "num")
    d = check_nonneg_int(den, "den")
    if d == 0:
        return "0.0%"
    tenths = (n * 1000) // d
    whole, frac = divmod(tenths, 10)
    return f"{whole}.{frac}%"


def canonical_json_bytes(obj: Any) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("utf-8")


def _fold_and_reject_non_ascii_letters(text: str) -> str:
    """NFKC -> NFKD -> drop Cf/Mn -> reject non-ASCII letters (except micro sign)."""
    nfkd = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    stripped = "".join(ch for ch in nfkd if unicodedata.category(ch) not in ("Cf", "Mn"))
    require_ok(
        not any(ord(ch) > 127 and ch.isalpha() and ch not in ("\u03bc", "\u00b5") for ch in stripped),
        "NON_ASCII_POLICY_TEXT",
        "Non-ASCII letters forbidden in policy-scanned telemetry text",
    )
    return stripped


def _normalize_policy_text(text: str) -> str:
    return _fold_and_reject_non_ascii_letters(text).casefold()


def _check_policy_key_value(key: str, val: Any) -> None:
    clean_k = _fold_and_reject_non_ascii_letters(key)
    if clean_k.casefold() == "power_band":
        require_ok(type(val) is str and _normalize_policy_text(val).strip() in _ALLOWED_POWER_BANDS, "BAD_POWER_BAND", "power_band must be ac|battery|unknown")
        return
    spaced_k = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", clean_k).replace("_", " ").casefold()
    tokens = [tok for tok in re.split(r"[^a-z0-9\u03bc]+", spaced_k) if tok]
    collapsed = "".join(tokens)
    if (
        _FORBIDDEN_COMPACT_KEY_RE.search(collapsed) is not None
        or _FORBIDDEN_VALUE_RE.search(spaced_k) is not None
        or any(tok in _FORBIDDEN_KEY_TOKENS for tok in tokens)
    ):
        raise HypersphericHudError("FORBIDDEN_ENERGY_KEY", f"Forbidden uncontrolled energy/power key: {key}")


def _check_policy_string_value(val: str) -> None:
    norm_s = _normalize_policy_text(val)
    require_ok(_FORBIDDEN_VALUE_RE.search(norm_s) is None, "FORBIDDEN_ENERGY_VAL", f"Forbidden energy/power claim: {val}")


def validate_no_floats_or_energy_claims(root: Any, max_depth: int = MAX_JSON_DEPTH) -> None:
    """Iterative depth-capped tree validator rejecting floats, non-JSON containers, and energy/power claims."""
    depth_cap = check_nonneg_int(max_depth, "max_depth")
    stack: List[Tuple[Any, int]] = [(root, 0)]
    while stack:
        node, depth = stack.pop()
        require_ok(depth <= depth_cap, "JSON_DEPTH", "JSON nesting exceeds cap")
        t = type(node)
        if t is float:
            raise HypersphericHudError("FLOAT_FORBIDDEN", "Float forbidden in zero-float HUD payload")
        if t is dict:
            for k, v in node.items():
                require_ok(type(k) is str, "BAD_KEY", "Dict key must be str")
                _check_policy_key_value(k, v)
                stack.append((v, depth + 1))
        elif t is list:
            for item in node:
                stack.append((item, depth + 1))
        elif t is str:
            _check_policy_string_value(node)
        elif t in (int, bool) or node is None:
            continue
        else:
            raise HypersphericHudError("BAD_JSON_TYPE", f"Unsupported container/value type {t.__name__}")


def _check_safe_path(path: Path, label: str) -> None:
    raw = str(path)
    require_ok("\x00" not in raw and ".." not in path.parts, "UNSAFE_PATH", f"{label} contains NUL or '..'")


def summarize_epistemic_web_index(data: Any) -> Dict[str, Any]:
    """Compute exact pure-Q S^3 Hyperspheric integration metrics from an epistemic web index JSON."""
    require_ok(type(data) is dict, "BAD_INDEX_ROOT", "Web index root must be a dict")
    node_count = check_nonneg_int(data.get("node_count"), "node_count")
    cross_edges = check_nonneg_int(data.get("total_cross_org_edges"), "total_cross_org_edges")
    hist_edges = check_nonneg_int(data.get("total_historical_edges"), "total_historical_edges")
    op_tax = check_nonneg_int(data.get("operator_taxonomy_count"), "operator_taxonomy_count")
    obs_tax = check_nonneg_int(data.get("obstruction_taxonomy_count"), "obstruction_taxonomy_count")
    org_counts = data.get("org_counts")
    bridge_matrix = data.get("cross_org_bridge_matrix")
    idx_sha = data.get("index_sha256")
    require_ok(type(org_counts) is dict and len(org_counts) >= 1, "BAD_ORG_COUNTS", "org_counts must be non-empty dict")
    require_ok(type(bridge_matrix) is dict, "BAD_BRIDGE_MATRIX", "cross_org_bridge_matrix must be dict")
    require_ok(is_hex64(idx_sha), "BAD_INDEX_SHA", "index_sha256 must be 64-char lowercase hex")

    n_orgs = len(org_counts)
    max_org_pairs = (n_orgs * (n_orgs - 1)) // 2
    active_org_pairs = 0
    for bk, bv in bridge_matrix.items():
        require_ok(type(bk) is str, "BAD_BRIDGE_KEY", "Bridge key must be str")
        cnt = check_nonneg_int(bv, f"bridge[{bk}]")
        if cnt > 0:
            active_org_pairs += 1

    hist_share = Fraction(hist_edges, cross_edges) if cross_edges > 0 else Fraction(0, 1)
    bridge_density = Fraction(active_org_pairs, max_org_pairs) if max_org_pairs > 0 else Fraction(0, 1)
    mean_cross_deg = Fraction(cross_edges, node_count) if node_count > 0 else Fraction(0, 1)

    return {
        "index_sha256": idx_sha,
        "node_count": node_count,
        "total_cross_org_edges": cross_edges,
        "total_historical_edges": hist_edges,
        "operator_taxonomy_count": op_tax,
        "obstruction_taxonomy_count": obs_tax,
        "org_bucket_count": n_orgs,
        "active_org_bridge_pairs": active_org_pairs,
        "max_org_bridge_pairs": max_org_pairs,
        "historical_interposition_share_q": frac_str(hist_share),
        "historical_interposition_share_pct": fmt_pct_tenths(hist_edges, cross_edges),
        "bridge_pair_density_q": frac_str(bridge_density),
        "bridge_pair_density_pct": fmt_pct_tenths(active_org_pairs, max_org_pairs),
        "mean_cross_org_degree_q": frac_str(mean_cross_deg),
    }


def summarize_board_and_members(
    board_lines: Sequence[str],
    members_obj: Optional[Dict[str, Any]] = None,
    poloidal_roles: FrozenSet[str] = DEFAULT_POLOIDAL_ROLES,
) -> Dict[str, Any]:
    """Compute exact pure-Z poloidal vs toroidal message integration and active member counts."""
    require_ok(type(board_lines) in (list, tuple), "BAD_BOARD_LINES", "board_lines must be list/tuple")
    require_ok(type(poloidal_roles) in (set, frozenset, list, tuple), "BAD_POLOIDAL_ROLES", "poloidal_roles must be set/frozenset/list/tuple")
    total_msgs = 0
    poloidal_msgs = 0
    done_msgs = 0
    for line in board_lines:
        require_ok(type(line) is str, "BAD_BOARD_LINE_TYPE", "Each board line must be str")
        stripped = line.strip()
        if not stripped:
            continue
        try:
            rec = json.loads(stripped)
        except ValueError as exc:
            raise HypersphericHudError("BAD_BOARD_JSONL", f"Malformed board JSONL line: {exc}") from exc
        require_ok(type(rec) is dict, "BAD_BOARD_REC", "Board line must be JSON object")
        total_msgs += 1
        agent = str(rec.get("agent", ""))
        text = str(rec.get("text", ""))
        if agent in poloidal_roles:
            poloidal_msgs += 1
        if "[done]" in text:
            done_msgs += 1

    members_total = 0
    members_working = 0
    if members_obj is not None:
        require_ok(type(members_obj) is dict, "BAD_MEMBERS_ROOT", "members.json must be dict")
        roster = members_obj.get("members", [])
        require_ok(type(roster) is list, "BAD_MEMBERS_LIST", "members field must be list")
        members_total = len(roster)
        members_working = sum(1 for m in roster if type(m) is dict and m.get("state") == "working")

    return {
        "board_messages": total_msgs,
        "poloidal_messages": poloidal_msgs,
        "done_messages": done_msgs,
        "poloidal_share_pct": fmt_pct_tenths(poloidal_msgs, total_msgs),
        "members_total": members_total,
        "members_working": members_working,
    }


def inspect_contention_hook(data: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Validate optional host-contention environment receipt (zero uncontrolled energy claims)."""
    if data is None:
        return {"status": "not_provided", "contended": False, "power_band": "unknown"}
    validate_no_floats_or_energy_claims(data)
    require_ok(type(data) is dict, "BAD_CONTENTION_ROOT", "Contention receipt must be dict")
    env = data.get("environment", data)
    require_ok(type(env) is dict, "BAD_CONTENTION_ENV", "Contention environment must be dict")
    contended = env.get("contended")
    require_ok(type(contended) is bool, "BAD_CONTENDED_FLAG", "contended flag must be bool")
    band = _normalize_policy_text(env.get("power_band", "unknown")).strip()
    return {
        "status": "wired",
        "contended": contended,
        "power_band": band,
        "telemetry_scope_policy": TELEMETRY_SCOPE_POLICY,
    }


def inspect_rag_sqlite_hook(db_path: Optional[Path], query: Optional[str] = None) -> Dict[str, Any]:
    """Read-only probe for optional hybrid epistemic RAG SQLite graph database."""
    empty = {
        "status": "not_wired_yet",
        "tables": 0,
        "nodes_rows": 0,
        "edges_rows": 0,
        "dense_embeddings_rows": 0,
        "verified_cert_nodes": 0,
        "org_buckets": 0,
        "edge_type_counts": {},
        "meta_schema_version": "unknown",
        "query_matches": [],
    }
    if db_path is None:
        return empty
    require_ok(isinstance(db_path, Path), "BAD_RAG_PATH", "rag_db must be Path or None")
    require_ok(query is None or type(query) is str, "BAD_RAG_QUERY", "rag_query must be str or None")
    _check_safe_path(db_path, "rag_db")
    if not db_path.is_file():
        missing = dict(empty)
        missing["status"] = "missing_db"
        return missing
    uri = f"file:{db_path.resolve()}?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
        try:
            cur = conn.cursor()
            tables = {row[0] for row in cur.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
            n_nodes = 0
            verified_nodes = 0
            org_buckets = 0
            if "nodes" in tables:
                n_nodes = int(cur.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
                cols = {row[1] for row in cur.execute("PRAGMA table_info(nodes)").fetchall()}
                if "is_verified_cert" in cols:
                    verified_nodes = int(cur.execute("SELECT COUNT(*) FROM nodes WHERE is_verified_cert = 1").fetchone()[0])
                if "org" in cols:
                    org_buckets = int(cur.execute("SELECT COUNT(DISTINCT org) FROM nodes").fetchone()[0])
            n_edges = 0
            edge_types: Dict[str, int] = {}
            if "edges" in tables:
                n_edges = int(cur.execute("SELECT COUNT(*) FROM edges").fetchone()[0])
                ecols = {row[1] for row in cur.execute("PRAGMA table_info(edges)").fetchall()}
                if "edge_type" in ecols:
                    for etype, cnt in cur.execute("SELECT edge_type, COUNT(*) FROM edges GROUP BY edge_type ORDER BY edge_type").fetchall():
                        edge_types[str(etype)] = int(cnt)
            n_dense = int(cur.execute("SELECT COUNT(*) FROM dense_embeddings").fetchone()[0]) if "dense_embeddings" in tables else 0
            meta_ver = "unknown"
            if "meta" in tables:
                row = cur.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                if row is not None:
                    meta_ver = str(row[0])
            q_matches: List[str] = []
            if query is not None and "fts_chunks" in tables:
                cleaned = re.sub(r"[^A-Za-z0-9_ ]+", " ", query).strip()
                if cleaned:
                    rows = cur.execute(
                        "SELECT node_id FROM fts_chunks WHERE fts_chunks MATCH ? ORDER BY node_id ASC LIMIT 5",
                        (cleaned,),
                    ).fetchall()
                    q_matches = [str(r[0]) for r in rows]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise HypersphericHudError("BAD_RAG_SQLITE", f"Failed reading RAG sqlite: {exc}") from exc
    return {
        "status": "wired",
        "tables": len(tables),
        "nodes_rows": n_nodes,
        "edges_rows": n_edges,
        "dense_embeddings_rows": n_dense,
        "verified_cert_nodes": verified_nodes,
        "org_buckets": org_buckets,
        "edge_type_counts": edge_types,
        "meta_schema_version": meta_ver,
        "query_matches": q_matches,
    }


def build_hud_snapshot(
    web_index_data: Dict[str, Any],
    *,
    board_lines: Sequence[str] = (),
    members_obj: Optional[Dict[str, Any]] = None,
    poloidal_roles: FrozenSet[str] = DEFAULT_POLOIDAL_ROLES,
    contention_data: Optional[Dict[str, Any]] = None,
    rag_db_path: Optional[Path] = None,
    rag_query: Optional[str] = None,
    captured_unix_s: int = 0,
) -> Dict[str, Any]:
    """Build deterministic zero-float S^3 Hyperspheric HUD snapshot with canonical snapshot_sha256."""
    ts = check_nonneg_int(captured_unix_s, "captured_unix_s")
    web_summary = summarize_epistemic_web_index(web_index_data)
    board_summary = summarize_board_and_members(board_lines, members_obj, poloidal_roles=poloidal_roles)
    contention_summary = inspect_contention_hook(contention_data)
    rag_summary = inspect_rag_sqlite_hook(rag_db_path, query=rag_query)

    payload: Dict[str, Any] = {
        "schema": SCHEMA_HUD,
        "zero_floats": True,
        "telemetry_scope_policy": TELEMETRY_SCOPE_POLICY,
        "hyperspheric_web": web_summary,
        "board_dynamics": board_summary,
        "contention_hook": contention_summary,
        "rag_hook": rag_summary,
    }
    snap_sha = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    snap = dict(payload)
    snap["captured_unix_s"] = ts
    snap["snapshot_sha256"] = snap_sha
    validate_hud_snapshot(snap)
    return snap


def _validate_nested_section(sec: Any, name: str, int_keys: Sequence[str], str_keys: Sequence[str]) -> Dict[str, Any]:
    require_ok(type(sec) is dict, "BAD_SECTION_TYPE", f"{name} must be dict")
    for k in int_keys:
        require_ok(k in sec, "MISSING_SECTION_KEY", f"{name} missing {k}")
        check_nonneg_int(sec[k], f"{name}.{k}")
    for k in str_keys:
        require_ok(k in sec and type(sec[k]) is str, "BAD_SECTION_STR", f"{name}.{k} must be str")
    return sec


def validate_hud_snapshot(snap: Any) -> None:
    """Fail-closed validation of HUD snapshot root, nested section schemas, and snapshot_sha256."""
    validate_no_floats_or_energy_claims(snap)
    require_ok(type(snap) is dict, "BAD_SNAP_ROOT", "HUD snapshot must be dict")
    req = (
        "schema",
        "zero_floats",
        "telemetry_scope_policy",
        "hyperspheric_web",
        "board_dynamics",
        "contention_hook",
        "rag_hook",
        "captured_unix_s",
        "snapshot_sha256",
    )
    for k in req:
        require_ok(k in snap, "MISSING_KEY", f"Missing key {k}")
    require_ok(set(snap.keys()) == set(req), "EXTRA_KEY", "Unexpected top-level HUD snapshot keys")
    require_ok(snap["schema"] == SCHEMA_HUD, "BAD_SCHEMA", "HUD schema mismatch")
    require_ok(snap["zero_floats"] is True, "BAD_ZERO_FLOATS", "zero_floats must be True")
    require_ok(snap["telemetry_scope_policy"] == TELEMETRY_SCOPE_POLICY, "BAD_SCOPE_POLICY", "Invalid telemetry scope policy")
    check_nonneg_int(snap["captured_unix_s"], "captured_unix_s")

    web = _validate_nested_section(
        snap["hyperspheric_web"],
        "hyperspheric_web",
        (
            "node_count",
            "total_cross_org_edges",
            "total_historical_edges",
            "operator_taxonomy_count",
            "obstruction_taxonomy_count",
            "org_bucket_count",
            "active_org_bridge_pairs",
            "max_org_bridge_pairs",
        ),
        (
            "index_sha256",
            "historical_interposition_share_q",
            "historical_interposition_share_pct",
            "bridge_pair_density_q",
            "bridge_pair_density_pct",
            "mean_cross_org_degree_q",
        ),
    )
    require_ok(is_hex64(web["index_sha256"]), "BAD_INDEX_SHA", "hyperspheric_web.index_sha256 must be 64-char lowercase hex")

    _validate_nested_section(
        snap["board_dynamics"],
        "board_dynamics",
        ("board_messages", "poloidal_messages", "done_messages", "members_total", "members_working"),
        ("poloidal_share_pct",),
    )

    cont = _validate_nested_section(snap["contention_hook"], "contention_hook", (), ("status", "power_band"))
    require_ok(type(cont.get("contended")) is bool, "BAD_CONTENDED_FLAG", "contention_hook.contended must be bool")

    rag = _validate_nested_section(
        snap["rag_hook"],
        "rag_hook",
        ("tables", "nodes_rows", "edges_rows", "dense_embeddings_rows", "verified_cert_nodes", "org_buckets"),
        ("status", "meta_schema_version"),
    )
    require_ok(type(rag.get("edge_type_counts")) is dict, "BAD_RAG_EDGE_TYPES", "rag_hook.edge_type_counts must be dict")
    for ek, ev in rag["edge_type_counts"].items():
        check_nonneg_int(ev, f"rag_hook.edge_type_counts[{ek}]")
    require_ok(type(rag.get("query_matches")) is list, "BAD_RAG_MATCHES", "rag_hook.query_matches must be list")
    for qm in rag["query_matches"]:
        require_ok(type(qm) is str, "BAD_RAG_MATCH_ITEM", "rag_hook.query_matches items must be str")

    payload = {k: snap[k] for k in snap if k not in ("captured_unix_s", "snapshot_sha256")}
    exp_sha = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
    got_sha = snap["snapshot_sha256"]
    require_ok(is_hex64(got_sha) and hmac.compare_digest(got_sha, exp_sha), "SNAP_SHA_MISMATCH", "snapshot_sha256 mismatch")


def render_screenpad_panel(snap: Dict[str, Any]) -> str:
    """Compact deterministic 4-line plain-text summary formatted for a ScreenPad Plus terminal pane."""
    validate_hud_snapshot(snap)
    w = snap["hyperspheric_web"]
    s = snap["board_dynamics"]
    c = snap["contention_hook"]
    r = snap["rag_hook"]
    lines = [
        f"ALETHEION S^3 HYPERSPHERIC HUD | sha={snap['snapshot_sha256'][:12]}",
        f"WEB: nodes={w['node_count']} cross_edges={w['total_cross_org_edges']} hist_edges={w['total_historical_edges']} ({w['historical_interposition_share_pct']}) bridges={w['active_org_bridge_pairs']}/{w['max_org_bridge_pairs']} ({w['bridge_pair_density_pct']})",
        f"BOARD: msgs={s['board_messages']} poloidal={s['poloidal_messages']} ({s['poloidal_share_pct']}) done={s['done_messages']} working={s['members_working']}/{s['members_total']}",
        f"HOOKS: contention={c['status']}(contended={c['contended']},band={c['power_band']}) | rag={r['status']}(nodes={r['nodes_rows']},edges={r['edges_rows']},certs={r['verified_cert_nodes']})",
    ]
    return "\n".join(lines) + "\n"


def run_selftest() -> Dict[str, Any]:
    """Deterministic offline pure-Z/Q self-test (no external filesystem dependencies)."""
    sample_web = {
        "index_sha256": "a" * 64,
        "node_count": 777,
        "total_cross_org_edges": 64951,
        "total_historical_edges": 29227,
        "operator_taxonomy_count": 24,
        "obstruction_taxonomy_count": 10,
        "org_counts": {"org_a": 100, "org_b": 50, "historical_corpus": 200},
        "cross_org_bridge_matrix": {"ab": 10, "ac": 20, "bc": 30},
    }
    sample_board = [
        json.dumps({"agent": "cartographer", "text": "[done] web pass"}),
        json.dumps({"agent": "worker", "text": "[done] verify"}),
    ]
    sample_members = {"members": [{"name": "worker", "state": "working"}, {"name": "weaver", "state": "waiting"}]}
    sample_cont = {"environment": {"contended": False, "power_band": "ac"}}
    return build_hud_snapshot(
        sample_web,
        board_lines=sample_board,
        members_obj=sample_members,
        contention_data=sample_cont,
        captured_unix_s=0,
    )


def _load_json_file(path: Path, label: str) -> Any:
    _check_safe_path(path, label)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise HypersphericHudError("READ_OR_JSON_ERR", f"{label}: {exc}") from exc


def _load_text_lines(path: Path, label: str) -> List[str]:
    _check_safe_path(path, label)
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise HypersphericHudError("READ_OR_DECODE_ERR", f"{label}: {exc}") from exc


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Aletheion Pure-Q S^3 Hyperspheric Epistemic HUD")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic offline self-test (default)")
    parser.add_argument("--once", action="store_true", help="Emit single snapshot from --web-index (requires --web-index)")
    parser.add_argument("--json", action="store_true", dest="as_json", help="Emit canonical JSON (default format)")
    parser.add_argument("--screenpad", action="store_true", help="Emit compact 4-line plain-text ScreenPad pane summary")
    parser.add_argument("--web-index", type=Path, default=None, help="Path to epistemic web index JSON (requires --once)")
    parser.add_argument("--board", type=Path, default=None, help="Optional path to board JSONL")
    parser.add_argument("--members", type=Path, default=None, help="Optional path to members JSON")
    parser.add_argument("--poloidal-roles", type=str, default=None, help="Comma-separated poloidal role names")
    parser.add_argument("--contention-json", type=Path, default=None, help="Optional host-contention receipt JSON")
    parser.add_argument("--rag-db", type=Path, default=None, help="Optional hybrid epistemic RAG SQLite path")
    parser.add_argument("--rag-query", type=str, default=None, help="Optional read-only FTS5 query against --rag-db")
    parser.add_argument("--live-clock", action="store_true", help="Stamp wall-clock unix seconds instead of 0")
    args = parser.parse_args(argv)
    try:
        if args.once or args.web_index is not None:
            require_ok(args.once and args.web_index is not None, "BAD_CLI_MODE", "--once and --web-index must be passed together")
            web_data = _load_json_file(args.web_index, "web_index")
            board_lines = _load_text_lines(args.board, "board") if args.board is not None else []
            members_data = _load_json_file(args.members, "members") if args.members is not None else None
            cont_data = _load_json_file(args.contention_json, "contention") if args.contention_json is not None else None
            roles = (
                frozenset(r.strip() for r in args.poloidal_roles.split(",") if r.strip())
                if args.poloidal_roles is not None
                else DEFAULT_POLOIDAL_ROLES
            )
            snap = build_hud_snapshot(
                web_data,
                board_lines=board_lines,
                members_obj=members_data,
                poloidal_roles=roles,
                contention_data=cont_data,
                rag_db_path=args.rag_db,
                rag_query=args.rag_query,
                captured_unix_s=int(time.time()) if args.live_clock else 0,
            )
        else:
            snap = run_selftest()
        if args.screenpad:
            sys.stdout.write(render_screenpad_panel(snap))
        else:
            sys.stdout.write(json.dumps(snap, indent=2, sort_keys=True, ensure_ascii=True, allow_nan=False) + "\n")
        return 0
    except (HypersphericHudError, OSError) as exc:
        sys.stderr.write(f"ERROR: {exc}\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
