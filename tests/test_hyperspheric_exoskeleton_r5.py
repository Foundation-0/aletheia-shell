#!/usr/bin/env python3
"""Assertion-free R5 verification & adversarial tamper-refusal suite for Aletheion Hyperspheric Exoskeleton:
  - bin/hyperspheric-hud.py
  - bin/aletheion_hysteresis_governor.py
  - bin/aletheion_exoskeleton_plugins.py

Pure Python stdlib, zero bare asserts, zero external filesystem dependencies, 3-mode -I parity,
and adversarial boundary/policy/tamper test coverage designed for external mutation-kill verification.
"""

from __future__ import annotations

import ast
import copy
from fractions import Fraction
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = REPO_ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))


def _load_hyphen_module(mod_name: str, file_path: Path):
    spec = importlib.util.spec_from_file_location(mod_name, file_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load module {file_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod
    spec.loader.exec_module(mod)
    return mod


hud = _load_hyphen_module("hyperspheric_hud", BIN_DIR / "hyperspheric-hud.py")
import aletheion_exoskeleton_plugins as plug
import aletheion_hysteresis_governor as gov


def _resign_hud_snap(snap: dict) -> dict:
    out = copy.deepcopy(snap)
    payload = {k: out[k] for k in out if k not in ("captured_unix_s", "snapshot_sha256")}
    out["snapshot_sha256"] = hashlib.sha256(hud.canonical_json_bytes(payload)).hexdigest()
    return out


class TestAletheionHypersphericExoskeletonR5(unittest.TestCase):
    # ------------------------------------------------------------------
    # 0. AST Purity: Zero Float Literals & Zero Bare Asserts Across All 3 Modules
    # ------------------------------------------------------------------
    def test_01_ast_zero_floats_and_zero_bare_asserts(self) -> None:
        for fname in ("hyperspheric-hud.py", "aletheion_hysteresis_governor.py", "aletheion_exoskeleton_plugins.py"):
            src = (BIN_DIR / fname).read_text(encoding="utf-8")
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Assert):
                    self.fail(f"Bare assert found in {fname}")
                if isinstance(node, ast.Constant) and type(node.value) is float:
                    self.fail(f"Float literal {node.value!r} found in {fname}")

    # ------------------------------------------------------------------
    # 1. bin/hyperspheric-hud.py: Guards, Unified NFKC+Cf+Mn Policy, RAG & CLI
    # ------------------------------------------------------------------
    def test_02_hud_primitives_and_nfkc_cf_mn_energy_policy_guards(self) -> None:
        self.assertEqual(hud.check_nonneg_int(0, "z"), 0)
        self.assertEqual(hud.check_nonneg_int(hud.MAX_INT_ABS, "m"), hud.MAX_INT_ABS)
        for bad in (-1, True, 1.5, hud.MAX_INT_ABS + 1, "1"):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.check_nonneg_int(bad, "x")
            self.assertEqual(cm.exception.code, "BAD_NONNEG_INT")

        self.assertEqual(hud.fmt_pct_tenths(0, 0), "0.0%")
        self.assertEqual(hud.fmt_pct_tenths(1, 2), "50.0%")
        self.assertEqual(hud.fmt_pct_tenths(29227, 64951), "44.9%")

        # Legitimate thermal scalars, identifiers (R2), & allowlisted power_band pass cleanly
        hud.validate_no_floats_or_energy_claims(
            {
                "ok": [
                    1,
                    "a",
                    True,
                    None,
                    "5 ms",
                    "70 C",
                    "78\u00b0C",
                    "70/78/85 C knees",
                    "5 deg C deadband",
                    "135/2",
                    "45.0%",
                    "0.5 mm",
                    "grad_W_q",
                    "cpu_jiffies_total",
                    "129,279.00 tok/s",
                    "469.41 us/tok",
                    "8.113 MiB",
                    "sm_86",
                    "R10-TAG-61",
                    "J2000",
                    "J2000.0",
                    "worker W10",
                    "2024-W05",
                    "sprint-W12",
                    "node-w10",
                    "W-2",
                    "W12",
                    "kW12",
                    "bench w/ perf counters",
                    "taskset -c 3 w/ 5 reps",
                    "12 ms/tok",
                    "sm_clock 2100 MHz",
                    "0.9 V",
                ],
                "temp_c": 65,
                "power_band": "ac",
                "metric_unit": "deg_c",
            },
            max_depth=4,
        )
        hud.validate_no_floats_or_energy_claims([[[[1]]]], max_depth=4)
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_no_floats_or_energy_claims({"ok": 1}, max_depth=None)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_NONNEG_INT")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_no_floats_or_energy_claims([[[[1]]]], max_depth=3)
        self.assertEqual(cm.exception.code, "JSON_DEPTH")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_no_floats_or_energy_claims({"f": 0.5})
        self.assertEqual(cm.exception.code, "FLOAT_FORBIDDEN")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_no_floats_or_energy_claims({1: "x"})
        self.assertEqual(cm.exception.code, "BAD_KEY")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_no_floats_or_energy_claims({"t": (1, 2)})
        self.assertEqual(cm.exception.code, "BAD_JSON_TYPE")

        # Asymmetric forbidden key checks (compact regex, value regex on max_45w/130w_psu, token set on pkg_w/pwr, camelCase, Cf/Mn)
        for bad_ek in (
            "mj_per_tok",
            "j_per_tok",
            "uJ_per_tok",
            "mean_watts",
            "pkg_w",
            "total_j",
            "pwr",
            "max_45w",
            "45w",
            "130w_psu",
            "t_d_p",
            "p.o.w.e.r",
            "totalenergyreduction",
            "avgpowermw",
            "power_mw",
            "kwh",
            "tdp",
            "avg_power",
            "\uff4d\uff4a_per_tok",
            "p\u0301ower_draw",
            "e\u0301nergy_j",
            "p\u200bower",
            "totalEnergy",
        ):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_no_floats_or_energy_claims({bad_ek: 1})
            self.assertEqual(cm.exception.code, "FORBIDDEN_ENERGY_KEY")

        for bad_pb in (123, "quiet_45w", {"note": "45.2 W"}, ["ac"]):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_no_floats_or_energy_claims({"power_band": bad_pb})
            self.assertEqual(cm.exception.code, "BAD_POWER_BAND")

        # Non-ASCII homoglyph / Cyrillic / Greek look-alikes rejected fail-closed
        for bad_homoglyph in ({"note": "5 \u051d"}, {"\u0435nergy": 1}, {"note": "3 \u043a\u0412\u0442"}, {"note": "45.2 \u03c9"}, {"note": "\u051datt"}):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_no_floats_or_energy_claims(bad_homoglyph)
            self.assertEqual(cm.exception.code, "NON_ASCII_POLICY_TEXT")

        # Bare units (top-level, nested under unit dict), glued/SI/separator/e-notation/punctuation & prose claims (R1)
        for bad_val_tree in (
            {"metric_unit": "W"},
            {"unit": {"name": "kW"}},
            {"units_used": "mW"},
            {"note": "W"},
            {"note": "W?"},
            {"note": "TDP"},
            {"note": "watt-hour"},
            {"note": "[kW]"},
            {"note": "\u00b5W"},
            {"note": "mJ/tok"},
            {"note": "joules"},
            {"note": "45.2 W"},
            {"note": "45.2 W."},
            {"note": "45.2W!"},
            {"note": "45W"},
            {"note": "5 GW"},
            {"note": "5 TW"},
            {"note": "5 pW"},
            {"note": "5 PW"},
            {"note": "5 kilo watts"},
            {"note": "5 kilo-watts"},
            {"note": "5 m W"},
            {"note": "15 watthours"},
            {"note": "15 watt hours"},
            {"note": "15 watt-hour"},
            {"note": "45 Whr"},
            {"note": "cap_45_w"},
            {"note": "45_watts"},
            {"note": "limit_45_w"},
            {"note": "draw_45w"},
            {"note": "W_45"},
            {"note": "130w_psu"},
            {"note": "max_45w"},
            {"note": "45w"},
            {"note": "5\u2013W"},
            {"note": "12\u00b7W"},
            {"note": "45.2\u2219W"},
            {"note": "45.2\u00d7W"},
            {"note": "15 TDP"},
            {"note": "TDP 15"},
            {"note": "45.2\u200bW"},
            {"note": "45.2 k\u200bW"},
            {"note": "45.2\ufeffW"},
            {"note": "45.2 kilowatts"},
            {"note": "45 milliwatts"},
            {"note": "1.2 megawatts"},
            {"note": "12.5 kilojoules"},
            {"note": "45.2-W"},
            {"note": "45.2 [W]"},
            {"note": "45.2[W] drain"},
            {"note": "W: 45.2"},
            {"note": "W=45.2"},
            {"note": "W: 45.2 drain"},
            {"note": "J: 5"},
            {"note": "3.9e2 W"},
            {"note": "1.5E-3W"},
            {"note": "0.001 J/tok"},
            {"note": "0.5 kWh"},
            {"note": "measured 45.2 mW"},
            {"note": "12.5 joules"},
            {"note": "180 \u00b5W"},
            {"note": "\uff11\uff12\uff37"},
            {"note": "energy use dropped"},
            {"note": "power savings"},
            {"note": "joules per token"},
            {"note": "MJ per token"},
            {"note": "70% less energy"},
            {"note": "70 percent less power"},
            {"note": "achieved energy reduction"},
        ):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_no_floats_or_energy_claims(bad_val_tree)
            self.assertEqual(cm.exception.code, "FORBIDDEN_ENERGY_VAL")

        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud._check_safe_path(Path("a\x00b"), "p")
        self.assertEqual(cm.exception.code, "UNSAFE_PATH")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud._check_safe_path(Path("../secret"), "p")
        self.assertEqual(cm.exception.code, "UNSAFE_PATH")

    def test_03_hud_web_board_hooks_rag_validation_and_cli(self) -> None:
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_epistemic_web_index([])
        self.assertEqual(cm.exception.code, "BAD_INDEX_ROOT")
        base_web = {
            "index_sha256": "a" * 64,
            "node_count": 0,
            "total_cross_org_edges": 0,
            "total_historical_edges": 0,
            "operator_taxonomy_count": 1,
            "obstruction_taxonomy_count": 1,
            "org_counts": {"org_a": 1},
            "cross_org_bridge_matrix": {},
        }
        s_zero = hud.summarize_epistemic_web_index(base_web)
        self.assertEqual(s_zero["historical_interposition_share_q"], "0/1")
        self.assertEqual(s_zero["bridge_pair_density_q"], "0/1")
        self.assertEqual(s_zero["mean_cross_org_degree_q"], "0/1")

        mix_web = copy.deepcopy(base_web)
        mix_web["org_counts"] = {"org_a": 1, "org_b": 1, "org_c": 1}
        mix_web["cross_org_bridge_matrix"] = {"pos": 5, "zero": 0}
        self.assertEqual(hud.summarize_epistemic_web_index(mix_web)["active_org_bridge_pairs"], 1)
        for bad_bm, code in [
            ({1: 5}, "BAD_BRIDGE_KEY"),
            ({"bool_t": True}, "BAD_NONNEG_INT"),
            ({"bad_str": "x"}, "BAD_NONNEG_INT"),
        ]:
            w_bm = copy.deepcopy(base_web)
            w_bm["cross_org_bridge_matrix"] = bad_bm
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.summarize_epistemic_web_index(w_bm)
            self.assertEqual(cm.exception.code, code)

        # B4: Reject non-hex, uppercase, short, non-str index_sha256
        for k, bad_val, code in [
            ("org_counts", {}, "BAD_ORG_COUNTS"),
            ("org_counts", [], "BAD_ORG_COUNTS"),
            ("cross_org_bridge_matrix", [], "BAD_BRIDGE_MATRIX"),
            ("index_sha256", "short", "BAD_INDEX_SHA"),
            ("index_sha256", "z" * 64, "BAD_INDEX_SHA"),
            ("index_sha256", "A" * 64, "BAD_INDEX_SHA"),
            ("index_sha256", 123, "BAD_INDEX_SHA"),
        ]:
            w = copy.deepcopy(base_web)
            w[k] = bad_val
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.summarize_epistemic_web_index(w)
            self.assertEqual(cm.exception.code, code)

        # B2: Board lines container & element type guards (no raw AttributeError / TypeError)
        for bad_lines in (None, "abc", 123):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.summarize_board_and_members(bad_lines)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_BOARD_LINES")
        for bad_elem in ([123], [None]):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.summarize_board_and_members(bad_elem)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_BOARD_LINE_TYPE")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_board_and_members([], None, None)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_POLOIDAL_ROLES")

        sw = hud.summarize_board_and_members(
            ["", json.dumps({"agent": "worker", "text": "ok"})],
            {"members": [{"name": "a", "state": "working"}, {"name": "b", "state": "waiting"}, "non_dict"]},
        )
        self.assertEqual((sw["board_messages"], sw["members_total"], sw["members_working"]), (1, 3, 1))
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_board_and_members(["{bad json"])
        self.assertEqual(cm.exception.code, "BAD_BOARD_JSONL")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_board_and_members(["[]"])
        self.assertEqual(cm.exception.code, "BAD_BOARD_REC")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_board_and_members([], "not_dict")  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_MEMBERS_ROOT")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.summarize_board_and_members([], {"members": "not_list"})
        self.assertEqual(cm.exception.code, "BAD_MEMBERS_LIST")

        self.assertEqual(hud.inspect_contention_hook(None)["status"], "not_provided")
        self.assertEqual(hud.inspect_contention_hook({"environment": {"contended": False}})["power_band"], "unknown")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.inspect_contention_hook([])  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_CONTENTION_ROOT")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.inspect_contention_hook({"environment": []})
        self.assertEqual(cm.exception.code, "BAD_CONTENTION_ENV")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.inspect_contention_hook({"environment": {"contended": "yes"}})
        self.assertEqual(cm.exception.code, "BAD_CONTENDED_FLAG")

        self.assertEqual(hud.inspect_rag_sqlite_hook(None)["status"], "not_wired_yet")
        for bad_p in ("/tmp/rag.sqlite", 123):
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.inspect_rag_sqlite_hook(bad_p)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_RAG_PATH")
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.inspect_rag_sqlite_hook(Path("x.sqlite"), query=123)  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_RAG_QUERY")
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            self.assertEqual(hud.inspect_rag_sqlite_hook(tdp / "nope.sqlite")["status"], "missing_db")

            min_db = tdp / "min.sqlite"
            c_min = sqlite3.connect(min_db)
            c_min.execute("CREATE TABLE nodes (id TEXT)")
            c_min.execute("INSERT INTO nodes VALUES ('n0')")
            c_min.commit()
            c_min.close()
            r_min = hud.inspect_rag_sqlite_hook(min_db, query="panini")
            self.assertEqual((r_min["status"], r_min["nodes_rows"], r_min["query_matches"]), ("wired", 1, []))

            dbp = tdp / "rag.sqlite"
            conn = sqlite3.connect(dbp)
            conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            conn.execute("INSERT INTO meta VALUES ('schema_version', 'aletheia.epistemic_rag_graph.v1')")
            conn.execute("CREATE TABLE nodes (id TEXT PRIMARY KEY, org TEXT NOT NULL, is_verified_cert INTEGER NOT NULL)")
            conn.execute("INSERT INTO nodes VALUES ('n1', 'org_a', 1), ('n2', 'org_b', 0)")
            conn.execute("CREATE TABLE edges (src TEXT, dst TEXT, edge_type TEXT)")
            conn.execute("INSERT INTO edges VALUES ('n1', 'n2', 'PROVES_OPERATOR'), ('n2', 'n1', 'BRIDGES')")
            conn.execute("CREATE TABLE dense_embeddings (node_id TEXT PRIMARY KEY)")
            conn.execute("INSERT INTO dense_embeddings VALUES ('n1'), ('n2')")
            conn.execute("CREATE VIRTUAL TABLE fts_chunks USING fts5(node_id UNINDEXED, title)")
            conn.execute("INSERT INTO fts_chunks VALUES ('n1', 'panini sivasutra c1p'), ('n2', 'chokwe sona cycle')")
            conn.commit()
            conn.close()
            r_ok = hud.inspect_rag_sqlite_hook(dbp, query="panini")
            self.assertEqual(
                (r_ok["status"], r_ok["nodes_rows"], r_ok["edges_rows"], r_ok["verified_cert_nodes"], r_ok["org_buckets"], r_ok["dense_embeddings_rows"], r_ok["query_matches"]),
                ("wired", 2, 2, 1, 2, 2, ["n1"]),
            )
            self.assertEqual(hud.inspect_rag_sqlite_hook(dbp, query=None)["query_matches"], [])

            bad_db = tdp / "corrupt.sqlite"
            bad_db.write_bytes(b"not a sqlite database")
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.inspect_rag_sqlite_hook(bad_db)
            self.assertEqual(cm.exception.code, "BAD_RAG_SQLITE")

            web_p = tdp / "web.json"
            web_p.write_text(json.dumps({**base_web, "node_count": 777, "total_cross_org_edges": 64951, "total_historical_edges": 29227, "org_counts": {"a": 1, "b": 2}, "cross_org_bridge_matrix": {"ab": 5}}), encoding="utf-8")
            brd_p = tdp / "board.jsonl"
            brd_p.write_text(json.dumps({"agent": "custom-cartographer", "text": "[done] ok"}) + "\n", encoding="utf-8")
            mem_p = tdp / "members.json"
            mem_p.write_text(json.dumps({"members": [{"name": "a", "state": "working"}]}), encoding="utf-8")
            cnt_p = tdp / "cont.json"
            cnt_p.write_text(json.dumps({"environment": {"contended": False, "power_band": "ac"}}), encoding="utf-8")

            buf = io.StringIO()
            with mock.patch("sys.stdout", buf):
                rc = hud.main([
                    "--once", "--json", "--web-index", str(web_p),
                    "--board", str(brd_p), "--members", str(mem_p),
                    "--poloidal-roles", "custom-cartographer",
                    "--contention-json", str(cnt_p),
                    "--rag-db", str(dbp), "--rag-query", "panini",
                ])
            self.assertEqual(rc, 0)
            snap_out = json.loads(buf.getvalue())
            self.assertEqual(snap_out["rag_hook"]["query_matches"], ["n1"])
            self.assertEqual(snap_out["board_dynamics"]["poloidal_messages"], 1)
            self.assertEqual(snap_out["captured_unix_s"], 0)

            # A6: Fail closed when --once or --web-index is passed without the other
            for partial_args in (["--once"], ["--web-index", str(web_p)]):
                err_p = io.StringIO()
                with mock.patch("sys.stderr", err_p):
                    self.assertEqual(hud.main(partial_args), 2)
                self.assertIn("BAD_CLI_MODE", err_p.getvalue())

            buf_def = io.StringIO()
            with mock.patch("sys.stdout", buf_def):
                self.assertEqual(hud.main([]), 0)
            self.assertEqual(json.loads(buf_def.getvalue())["snapshot_sha256"], hud.run_selftest()["snapshot_sha256"])

            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud._load_json_file(tdp / "missing.json", "web_index")
            self.assertEqual(cm.exception.code, "READ_OR_JSON_ERR")
            corrupt_json = tdp / "corrupt.json"
            corrupt_json.write_text("{broken", encoding="utf-8")
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud._load_json_file(corrupt_json, "web_index")
            self.assertEqual(cm.exception.code, "READ_OR_JSON_ERR")

            buf_sp = io.StringIO()
            with mock.patch("sys.stdout", buf_sp):
                rc_sp = hud.main(["--self-test", "--screenpad"])
            self.assertEqual(rc_sp, 0)
            self.assertIn("ALETHEION S^3 HYPERSPHERIC HUD", buf_sp.getvalue())

            err_buf = io.StringIO()
            with mock.patch("sys.stderr", err_buf):
                rc_err = hud.main(["--once", "--web-index", str(tdp / "missing.json")])
            self.assertEqual(rc_err, 2)
            self.assertIn("READ_OR_JSON_ERR", err_buf.getvalue())

            # R3: Non-UTF-8 --board returns exit code 2 (no uncaught UnicodeDecodeError)
            bad_brd = tdp / "bad_board.jsonl"
            bad_brd.write_bytes(b"\xff\xfe\x00invalid utf8")
            err_brd = io.StringIO()
            with mock.patch("sys.stderr", err_brd):
                self.assertEqual(hud.main(["--once", "--web-index", str(web_p), "--board", str(bad_brd)]), 2)
            self.assertIn("READ_OR_DECODE_ERR", err_brd.getvalue())
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud._load_text_lines(bad_brd, "board")
            self.assertEqual(cm.exception.code, "READ_OR_DECODE_ERR")

        # B3: Snapshot root + nested section schema validation WITH recomputed snapshot_sha256
        snap = hud.run_selftest()
        hud.validate_hud_snapshot(snap)
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot([])
        self.assertEqual(cm.exception.code, "BAD_SNAP_ROOT")
        for k in list(snap.keys()):
            c = copy.deepcopy(snap)
            del c[k]
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_hud_snapshot(c)
            self.assertEqual(cm.exception.code, "MISSING_KEY")
        c_extra = copy.deepcopy(snap)
        c_extra["unvalidated_extra"] = "sneaky"
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_extra))
        self.assertEqual(cm.exception.code, "EXTRA_KEY")
        for k, bad_val, code in [
            ("schema", "bad", "BAD_SCHEMA"),
            ("zero_floats", False, "BAD_ZERO_FLOATS"),
            ("telemetry_scope_policy", "CLAIMED", "BAD_SCOPE_POLICY"),
            ("snapshot_sha256", 123, "SNAP_SHA_MISMATCH"),
            ("snapshot_sha256", "0" * 64, "SNAP_SHA_MISMATCH"),
            ("snapshot_sha256", "\u00b0" * 64, "SNAP_SHA_MISMATCH"),
        ]:
            c = copy.deepcopy(snap)
            c[k] = bad_val
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_hud_snapshot(c)
            self.assertEqual(cm.exception.code, code)

        # Re-signed nested section forgeries MUST fail closed
        for sec in ("hyperspheric_web", "board_dynamics", "contention_hook", "rag_hook"):
            c = copy.deepcopy(snap)
            c[sec] = "BAD_STRING_SECTION"
            with self.assertRaises(hud.HypersphericHudError) as cm:
                hud.validate_hud_snapshot(_resign_hud_snap(c))
            self.assertEqual(cm.exception.code, "BAD_SECTION_TYPE")

        c_miss = copy.deepcopy(snap)
        del c_miss["hyperspheric_web"]["node_count"]
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_miss))
        self.assertEqual(cm.exception.code, "MISSING_SECTION_KEY")

        c_str = copy.deepcopy(snap)
        c_str["board_dynamics"]["poloidal_share_pct"] = 50
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_str))
        self.assertEqual(cm.exception.code, "BAD_SECTION_STR")

        c_str2 = copy.deepcopy(snap)
        del c_str2["board_dynamics"]["poloidal_share_pct"]
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_str2))
        self.assertEqual(cm.exception.code, "BAD_SECTION_STR")

        c_hex = copy.deepcopy(snap)
        c_hex["hyperspheric_web"]["index_sha256"] = "z" * 64
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_hex))
        self.assertEqual(cm.exception.code, "BAD_INDEX_SHA")

        c_cnt = copy.deepcopy(snap)
        c_cnt["contention_hook"]["contended"] = "no"
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_cnt))
        self.assertEqual(cm.exception.code, "BAD_CONTENDED_FLAG")

        c_rag1 = copy.deepcopy(snap)
        c_rag1["rag_hook"]["edge_type_counts"] = []
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_rag1))
        self.assertEqual(cm.exception.code, "BAD_RAG_EDGE_TYPES")

        c_rag2 = copy.deepcopy(snap)
        c_rag2["rag_hook"]["query_matches"] = {}
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_rag2))
        self.assertEqual(cm.exception.code, "BAD_RAG_MATCHES")

        c_rag3 = copy.deepcopy(snap)
        c_rag3["rag_hook"]["edge_type_counts"] = {"PROVES": -1}
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_rag3))
        self.assertEqual(cm.exception.code, "BAD_NONNEG_INT")

        c_rag4 = copy.deepcopy(snap)
        c_rag4["rag_hook"]["query_matches"] = [123]
        with self.assertRaises(hud.HypersphericHudError) as cm:
            hud.validate_hud_snapshot(_resign_hud_snap(c_rag4))
        self.assertEqual(cm.exception.code, "BAD_RAG_MATCH_ITEM")

    # ------------------------------------------------------------------
    # 2. bin/aletheion_hysteresis_governor.py: Policy Parity, B2 Guards & Tamper
    # ------------------------------------------------------------------
    def test_04_thermal_governor_primitives_policy_and_operators(self) -> None:
        self.assertEqual(gov.check_frac(gov.MAX_INT_ABS, "m"), Fraction(gov.MAX_INT_ABS, 1))
        self.assertEqual(gov.check_frac(-gov.MAX_INT_ABS, "m"), Fraction(-gov.MAX_INT_ABS, 1))
        self.assertEqual(gov.check_frac(Fraction(gov.MAX_INT_ABS, gov.MAX_INT_ABS - 1), "m"), Fraction(gov.MAX_INT_ABS, gov.MAX_INT_ABS - 1))
        for bad, code in [
            (True, "BAD_FRAC"),
            (1.5, "BAD_FRAC"),
            ("1", "BAD_FRAC"),
            (gov.MAX_INT_ABS + 1, "INT_RANGE"),
            (-(gov.MAX_INT_ABS + 1), "INT_RANGE"),
            (Fraction(gov.MAX_INT_ABS + 1, 1), "FRAC_RANGE"),
            (Fraction(1, gov.MAX_INT_ABS + 1), "FRAC_RANGE"),
        ]:
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.check_frac(bad, "x")
            self.assertEqual(cm.exception.code, code)

        self.assertEqual(gov.check_pos_int(1, "p"), 1)
        self.assertEqual(gov.check_pos_int(gov.MAX_INT_ABS, "p"), gov.MAX_INT_ABS)
        for bad in (0, -1, True, gov.MAX_INT_ABS + 1):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.check_pos_int(bad, "p")
            self.assertEqual(cm.exception.code, "BAD_POS_INT")

        # Unified B1 + R1/R2 policy checks in governor
        gov.validate_json_tree_no_floats(
            {"a": [1, "s", True, None, "J2000", "2024-W05", "worker W10", "W12", "taskset -c 3 w/ 5 reps"], "power_band": "ac", "note": "70/78/85 C knees (70\u00b0C)"},
            max_depth=4,
        )
        gov.validate_json_tree_no_floats([[[[1]]]], max_depth=4)
        for bad_md in (None, -1, True, gov.MAX_INT_ABS + 1):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats({"a": 1}, max_depth=bad_md)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_MAX_DEPTH")
        gov.validate_json_tree_no_floats(1, max_depth=0)
        gov.validate_json_tree_no_floats(1, max_depth=gov.MAX_INT_ABS)
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_json_tree_no_floats([[[[1]]]], max_depth=3)
        self.assertEqual(cm.exception.code, "JSON_DEPTH")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_json_tree_no_floats({"f": 0.1})
        self.assertEqual(cm.exception.code, "FLOAT_FORBIDDEN")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_json_tree_no_floats({1: "x"})
        self.assertEqual(cm.exception.code, "BAD_KEY")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_json_tree_no_floats({"t": (1, 2)})
        self.assertEqual(cm.exception.code, "BAD_JSON_TYPE")
        for bad_pb in (123, "bad_band"):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats({"power_band": bad_pb})
            self.assertEqual(cm.exception.code, "BAD_POWER_BAND")
        for bad_ek in ("t_d_p", "p.o.w.e.r", "pkg_w", "total_j", "pwr", "max_45w", "130w_psu", "j_per_tok", "mean_watts", "totalenergyreduction", "avgpowermw", "p\u0301ower_mw"):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats({bad_ek: 1})
            self.assertEqual(cm.exception.code, "FORBIDDEN_ENERGY_KEY")
        for bad_homoglyph in ({"note": "5 \u051d"}, {"\u0435nergy": 1}):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats(bad_homoglyph)
            self.assertEqual(cm.exception.code, "NON_ASCII_POLICY_TEXT")
        for bad_u in ({"unit": "W"}, {"unit": {"name": "kW"}}, {"note": "W"}, {"note": "TDP"}, {"note": "watt-hour"}):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats(bad_u)
            self.assertEqual(cm.exception.code, "FORBIDDEN_ENERGY_VAL")
        for bad_ev in ("45.2 W", "5 GW", "5 TW", "5 pW", "5 kilo watts", "5 m W", "15 watthours", "45 Whr", "cap_45_w", "45_watts", "limit_45_w", "draw_45w", "W_45", "5\u2013W", "12\u00b7W", "45.2\u00d7W", "15 TDP", "45.2\u200bW", "45.2 kilowatts", "W: 45.2", "3.9e2 W", "energy use dropped", "joules per token", "MJ per token", "70% less energy"):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_json_tree_no_floats({"note": bad_ev})
            self.assertEqual(cm.exception.code, "FORBIDDEN_ENERGY_VAL")

        self.assertEqual(gov.petra_stepped_filter_q([50], 2, 2), [Fraction(50, 1)])
        for bad_tr in ([], "50", None):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.petra_stepped_filter_q(bad_tr, 2, 2)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "EMPTY_TRACE")
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.simulate_hysteresis_vs_bangbang(bad_tr)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "EMPTY_TRACE")
        for k1, k2 in ((1, 2), (2, 1)):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.petra_stepped_filter_q([50, 60], k1, k2)
            self.assertEqual(cm.exception.code, "BAD_CHAMBER_K")

        # B2: validate_knee_ladder container type guards (no raw TypeError on None)
        for bad_u, bad_d in ((None, [65]), ([70], None), (None, None)):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_knee_ladder(bad_u, bad_d)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_KNEES_TYPE")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_knee_ladder([], [])
        self.assertEqual(cm.exception.code, "BAD_KNEES_LEN")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_knee_ladder([Fraction(70, 1)], [Fraction(65, 1), Fraction(70, 1)])
        self.assertEqual(cm.exception.code, "BAD_KNEES_LEN")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_knee_ladder([Fraction(70, 1)], [Fraction(70, 1)])
        self.assertEqual(cm.exception.code, "NONPOS_DEADBAND")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_knee_ladder([Fraction(70, 1), Fraction(70, 1)], [Fraction(60, 1), Fraction(65, 1)])
        self.assertEqual(cm.exception.code, "NON_MONO_KNEES")
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_knee_ladder([Fraction(70, 1), Fraction(80, 1)], [Fraction(65, 1), Fraction(65, 1)])
        self.assertEqual(cm.exception.code, "NON_MONO_KNEES")

        for bad_g in (-1, 4, True):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.step_hysteresis_gear(bad_g, Fraction(70, 1), gov.DEFAULT_UP_KNEES, gov.DEFAULT_DN_KNEES)
            self.assertEqual(cm.exception.code, "BAD_GEAR")

        for k, uy, um, al, code in [
            (0, 1, 2, 0, "NONPOS_PARAM"),
            (1, 0, 2, 0, "NONPOS_PARAM"),
            (1, 2, 2, 0, "NO_SLIP"),
            (1, 1, 2, Fraction(-1, 10), "BAD_ALPHA"),
            (1, 1, 2, Fraction(11, 10), "BAD_ALPHA"),
        ]:
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.dougong_deadband_loop_q(k, uy, um, al)
            self.assertEqual(cm.exception.code, code)

    def test_05_thermal_governor_cert_tamper_monkeypatch_and_cli(self) -> None:
        cert = gov.certify_thermal_governor()
        self.assertTrue(cert["all_passed"])
        self.assertEqual(cert["g1_zero_chatter"]["metrics"]["bangbang_switches"], 39)
        self.assertEqual(cert["g1_zero_chatter"]["metrics"]["hysteresis_switches"], 0)
        self.assertEqual(cert["g2_glitch_and_ramp"]["glitch"]["hysteresis_switches"], 0)
        self.assertEqual(cert["g3_dougong_loop"]["coulomb"]["e_diss_shoelace"], "100/1")

        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_governor_certificate([])
        self.assertEqual(cm.exception.code, "BAD_CERT_ROOT")
        for k in list(cert.keys()):
            c = copy.deepcopy(cert)
            del c[k]
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_governor_certificate(c)
            self.assertEqual(cm.exception.code, "MISSING_KEY")
        c_ex = copy.deepcopy(cert)
        c_ex["extra_field"] = 1
        with self.assertRaises(gov.ThermalGovError) as cm:
            gov.validate_governor_certificate(c_ex)
        self.assertEqual(cm.exception.code, "EXTRA_KEY")
        for k, bad_v, code in [
            ("schema", "bad", "BAD_SCHEMA"),
            ("zero_floats", False, "BAD_ZERO_FLOATS"),
            ("telemetry_scope_policy", "BAD", "BAD_SCOPE_POLICY"),
            ("g1_zero_chatter", [], "BAD_SECTION"),
            ("all_passed", False, "ALL_PASSED_FALSE"),
            ("payload_sha256", 123, "PAYLOAD_SHA_MISMATCH"),
            ("payload_sha256", "0" * 64, "PAYLOAD_SHA_MISMATCH"),
            ("payload_sha256", "\u00b0" * 64, "PAYLOAD_SHA_MISMATCH"),
            ("certificate_sha256", 123, "CERT_SHA_MISMATCH"),
            ("certificate_sha256", "0" * 64, "CERT_SHA_MISMATCH"),
            ("certificate_sha256", "\u00b0" * 64, "CERT_SHA_MISMATCH"),
        ]:
            c = copy.deepcopy(cert)
            c[k] = bad_v
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_governor_certificate(c)
            self.assertEqual(cm.exception.code, code)

        for bad_cp in ("/tmp/cert.json", None, 123):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.check_certificate_file(bad_cp)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_CERT_PATH")

        for sec in ("g1_zero_chatter", "g2_glitch_and_ramp", "g3_dougong_loop"):
            c = copy.deepcopy(cert)
            c[sec]["passed"] = 1
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_governor_certificate(c)
            self.assertEqual(cm.exception.code, "BAD_PASS_FLAG")
            c2 = copy.deepcopy(cert)
            c2[sec]["passed"] = False
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.validate_governor_certificate(c2)
            self.assertEqual(cm.exception.code, "GATE_FAILED")

        with mock.patch.object(gov, "dougong_deadband_loop_q", return_value={"exact_match": False, "area_residual": "1/1", "e_diss_shoelace": "0/1"}):
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.certify_thermal_governor()
            self.assertEqual(cm.exception.code, "GATE_FAILED")

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "gov.json"
            buf_w = io.StringIO()
            with mock.patch("sys.stdout", buf_w):
                self.assertEqual(gov.main(["--write", str(p)]), 0)
            buf_s = io.StringIO()
            with mock.patch("sys.stdout", buf_s):
                self.assertEqual(gov.main([]), 0)
            self.assertEqual(p.read_text(encoding="utf-8"), buf_s.getvalue())
            buf_c = io.StringIO()
            with mock.patch("sys.stdout", buf_c):
                self.assertEqual(gov.main(["--check", str(p)]), 0)

            forged = json.loads(p.read_text(encoding="utf-8"))
            forged["g3_dougong_loop"]["coulomb"]["e_diss_shoelace"] = "999/1"
            payload = {k: forged[k] for k in forged if k not in ("payload_sha256", "certificate_sha256")}
            forged["payload_sha256"] = hashlib.sha256(gov._canonical_json_bytes(payload)).hexdigest()
            body = dict(payload)
            body["payload_sha256"] = forged["payload_sha256"]
            forged["certificate_sha256"] = hashlib.sha256(gov._canonical_json_bytes(body)).hexdigest()
            fp = Path(td) / "forged.json"
            fp.write_text(json.dumps(forged), encoding="utf-8")
            with self.assertRaises(gov.ThermalGovError) as cm:
                gov.check_certificate_file(fp)
            self.assertEqual(cm.exception.code, "LIVE_RECOMPUTE_MISMATCH")

            bad = Path(td) / "bad.json"
            bad.write_text("{corrupt", encoding="utf-8")
            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(gov.main(["--check", str(bad)]), 2)
            self.assertIn("READ_OR_JSON_ERR", err.getvalue())

    # ------------------------------------------------------------------
    # 3. bin/aletheion_exoskeleton_plugins.py: B2/B5/A2 Guards & Selftest
    # ------------------------------------------------------------------
    def test_06_exoskeleton_plugins_all_guards_and_selftest(self) -> None:
        self.assertEqual(plug.check_int(plug.MAX_INT_ABS, "i"), plug.MAX_INT_ABS)
        self.assertEqual(plug.check_int(-plug.MAX_INT_ABS, "i"), -plug.MAX_INT_ABS)
        for bad in (True, 1.5, plug.MAX_INT_ABS + 1, -(plug.MAX_INT_ABS + 1)):
            with self.assertRaises(plug.PluginError) as cm:
                plug.check_int(bad, "x")
            self.assertEqual(cm.exception.code, "BAD_INT")

        self.assertEqual(plug.check_pos_int(1, "p"), 1)
        self.assertEqual(plug.check_pos_int(plug.MAX_INT_ABS, "p"), plug.MAX_INT_ABS)
        for bad in (0, -1, True, plug.MAX_INT_ABS + 1):
            with self.assertRaises(plug.PluginError) as cm:
                plug.check_pos_int(bad, "p")
            self.assertEqual(cm.exception.code, "BAD_POS_INT")

        # B5: Iterative Khipu depth cap (depth 1500 no RecursionError) + cycle detection
        deep = {"id": "leaf", "value": 1, "children": []}
        for i in range(1500):
            deep = {"id": f"n{i}", "value": 1, "children": [deep]}
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum(deep)
        self.assertEqual(cm.exception.code, "KHIPU_DEPTH_CAP")

        exact_depth = {"id": "leaf", "value": 1, "children": []}
        for i in range(3):
            exact_depth = {"id": f"d{i}", "value": 1, "children": [exact_depth]}
        self.assertEqual(plug.build_khipu_tree_checksum(exact_depth, max_depth=3, max_nodes=4)["value"], 1)
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum(exact_depth, max_depth=2)
        self.assertEqual(cm.exception.code, "KHIPU_DEPTH_CAP")
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum(exact_depth, max_depth=3, max_nodes=3)
        self.assertEqual(cm.exception.code, "KHIPU_NODE_CAP")

        cyc: dict = {"id": "loop", "value": 1, "children": []}
        cyc["children"].append(cyc)
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum(cyc)
        self.assertEqual(cm.exception.code, "KHIPU_CYCLE")

        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum([])
        self.assertEqual(cm.exception.code, "BAD_KHIPU_NODE")
        for bad_id in ("", 123):
            with self.assertRaises(plug.PluginError) as cm:
                plug.build_khipu_tree_checksum({"id": bad_id, "value": 1})
            self.assertEqual(cm.exception.code, "BAD_KHIPU_ID")
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum({"id": "r", "value": 1, "children": "bad"})
        self.assertEqual(cm.exception.code, "BAD_KHIPU_CHILDREN")
        with self.assertRaises(plug.PluginError) as cm:
            plug.build_khipu_tree_checksum({"id": "r", "value": 10, "children": [{"id": "c1", "value": 4}, {"id": "c2", "value": 5}]})
        self.assertEqual(cm.exception.code, "KHIPU_SUM_MISMATCH")

        for bad_w, code in [
            ([], "BAD_KHIPU_WITNESS"),
            ({"payload": {"g1_hierarchical_tree_sums": []}}, "BAD_KHIPU_WITNESS"),
            ({"payload": {"g1_hierarchical_tree_sums": {"archaeological_witnesses": []}}}, "BAD_KHIPU_WITNESS"),
            ({"top_cord_sums": "bad", "cluster_head_sums": []}, "BAD_KHIPU_WITNESS"),
            ({"top_cord_sums": [], "cluster_head_sums": "bad"}, "BAD_KHIPU_WITNESS"),
            ({"top_cord_sums": [], "cluster_head_sums": []}, "EMPTY_KHIPU_WITNESS"),
            ({"payload": 123}, "EMPTY_KHIPU_WITNESS"),
            ({"top_cord_sums": ["not_dict"], "cluster_head_sums": []}, "BAD_KHIPU_ENTRY"),
            ({"top_cord_sums": [{"khipu_id": 1, "top_cord_id": 2, "top_value": 0, "cluster_values": []}], "cluster_head_sums": []}, "BAD_KHIPU_ENTRY"),
            ({"top_cord_sums": [{"khipu_id": 1, "top_cord_id": 2, "top_value": 0, "cluster_values": "bad"}], "cluster_head_sums": []}, "BAD_KHIPU_ENTRY"),
            ({"top_cord_sums": [], "cluster_head_sums": ["not_dict"]}, "BAD_KHIPU_ENTRY"),
            ({"top_cord_sums": [], "cluster_head_sums": [{"khipu_id": 1, "head_cord_id": 2, "cluster_values": [1]}]}, "BAD_KHIPU_ENTRY"),
            ({"top_cord_sums": [], "cluster_head_sums": [{"khipu_id": 1, "head_cord_id": 2, "cluster_values": "bad"}]}, "BAD_KHIPU_ENTRY"),
        ]:
            with self.assertRaises(plug.PluginError) as cm:
                plug.verify_khipu_archaeological_witnesses(bad_w)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, code)

        wrapped = {"payload": {"g1_hierarchical_tree_sums": {"archaeological_witnesses": plug.DEFAULT_KHIPU_WITNESSES}}}
        kw_res = plug.verify_khipu_archaeological_witnesses(wrapped)
        self.assertEqual((kw_res["witness_count"], kw_res["forest_total_value"]), (4, 964))

        # B5: Bounded Sona grid allocation (no OverflowError / OOM)
        self.assertEqual(plug.count_sona_torus_cycles(2, 2), 2)
        self.assertEqual(plug.count_sona_torus_cycles(2, 3), 1)
        self.assertEqual(plug.count_sona_torus_cycles(1, plug.MAX_TORUS_CELLS), 1)
        with self.assertRaises(plug.PluginError) as cm:
            plug.count_sona_torus_cycles(1, plug.MAX_TORUS_CELLS + 1)
        self.assertEqual(cm.exception.code, "GRID_TOO_LARGE")
        with self.assertRaises(plug.PluginError) as cm:
            plug.count_sona_torus_cycles(10**10, 10**10)
        self.assertEqual(cm.exception.code, "GRID_TOO_LARGE")
        with self.assertRaises(plug.PluginError) as cm:
            plug.sona_eulerian_schedule(1, plug.MAX_TORUS_CELLS + 1, [])
        self.assertEqual(cm.exception.code, "GRID_TOO_LARGE")
        self.assertEqual(plug.sona_eulerian_schedule(1, plug.MAX_TORUS_CELLS, ["x"] * plug.MAX_TORUS_CELLS)["cycle_length"], plug.MAX_TORUS_CELLS)
        with self.assertRaises(plug.PluginError) as cm:
            plug.run_plugin_selftest(repo_rows=1, repo_cols=plug.MAX_TORUS_CELLS + 1)
        self.assertEqual(cm.exception.code, "GRID_TOO_LARGE")
        self.assertEqual(plug.run_plugin_selftest(repo_rows=1, repo_cols=plug.MAX_TORUS_CELLS)["sona_repo_watchdog_rotation"]["cycle_length"], plug.MAX_TORUS_CELLS)

        self.assertTrue(plug.verify_sona_grid_theorem(max_dim=64)["theorem_verified"])
        with self.assertRaises(plug.PluginError) as cm:
            plug.verify_sona_grid_theorem(max_dim=65)
        self.assertEqual(cm.exception.code, "MAX_DIM_CAP")
        with self.assertRaises(plug.PluginError) as cm:
            plug.sona_eulerian_schedule(2, 3, "abcdef")  # type: ignore[arg-type]
        self.assertEqual(cm.exception.code, "BAD_LABEL_COUNT")
        with self.assertRaises(plug.PluginError) as cm:
            plug.sona_eulerian_schedule(2, 3, ["a", "b"])
        self.assertEqual(cm.exception.code, "BAD_LABEL_COUNT")
        for bad_lbls in ([1, "b", "c", "d", "e", "f"], ["", "b", "c", "d", "e", "f"]):
            with self.assertRaises(plug.PluginError) as cm:
                plug.sona_eulerian_schedule(2, 3, bad_lbls)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_LABEL_ITEM")
        with self.assertRaises(plug.PluginError) as cm:
            plug.sona_eulerian_schedule(4, 6, [f"r{i}" for i in range(24)])
        self.assertEqual(cm.exception.code, "SONA_NON_COPRIME_FRAGMENTATION")

        # B2 + A2: Container/arity & guarded marker/start-phoneme lookup in verify_panini_sivasutra_c1p
        for bad_sv, bad_pr, code in [
            (None, plug.CANONICAL_PRATYAHARAS, "BAD_SIVASUTRAS"),
            ([], plug.CANONICAL_PRATYAHARAS, "BAD_SIVASUTRAS"),
            (plug.SIVASUTRAS, None, "BAD_PRATYAHARAS"),
            (plug.SIVASUTRAS, [], "BAD_PRATYAHARAS"),
            (["bad"], plug.CANONICAL_PRATYAHARAS, "BAD_SIVASUTRA_ENTRY"),
            ([(1, "N1")], plug.CANONICAL_PRATYAHARAS, "BAD_SIVASUTRA_ENTRY"),
            ([(1, "", ("a",))], plug.CANONICAL_PRATYAHARAS, "BAD_MARKER"),
            ([(1, 123, ("a",))], plug.CANONICAL_PRATYAHARAS, "BAD_MARKER"),
            ([(1, "N1", "a")], plug.CANONICAL_PRATYAHARAS, "BAD_PHONES"),
            ([(1, "N1", ())], plug.CANONICAL_PRATYAHARAS, "BAD_PHONES"),
            ([(1, "N1", ("",))], plug.CANONICAL_PRATYAHARAS, "BAD_PHONEME"),
            ([(1, "N1", (123,))], plug.CANONICAL_PRATYAHARAS, "BAD_PHONEME"),
            (plug.SIVASUTRAS, ["bad"], "BAD_PRATYAHARA_ENTRY"),
            (plug.SIVASUTRAS, [("a", "b", "c")], "BAD_PRATYAHARA_ENTRY"),
            (plug.SIVASUTRAS, [("bad", "a", "NO_SUCH_MARKER", 1)], "UNKNOWN_MARKER"),
            (plug.SIVASUTRAS, [("bad", "a", ["b"], 1)], "UNKNOWN_MARKER"),
            (plug.SIVASUTRAS, [("bad", "MISSING_P", "N1", 1)], "MISSING_START_PHONEME"),
            (plug.SIVASUTRAS, [("bad", ["a"], "N1", 1)], "MISSING_START_PHONEME"),
        ]:
            with self.assertRaises(plug.PluginError) as cm:
                plug.verify_panini_sivasutra_c1p(sivasutras=bad_sv, pratyaharas=bad_pr)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, code)

        # B2: Panini unhashable / mixed-type spine & tag_sets guards
        for bad_sp in (["a"], "ab"):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(bad_sp, {"t": ["a"]})  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_SPINE")
        for bad_item in ([{}, "b"], ["", "b"], [1, "b"]):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(bad_item, {"t": ["b"]})  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_SPINE_ITEM")
        with self.assertRaises(plug.PluginError) as cm:
            plug.encode_panini_c1p_registry(["a", "a"], {"t": ["a"]})
        self.assertEqual(cm.exception.code, "DUP_SPINE")
        for bad_ts in ([], {}):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(["a", "b"], bad_ts)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_TAG_SETS")
        for bad_tag_dict in ({"": ["a"]}, {1: ["a"], "b": ["a"]}):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(["a", "b"], bad_tag_dict)  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "BAD_TAG_NAME")
        for bad_m in ([], "a"):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(["a", "b"], {"t": bad_m})  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "EMPTY_TAG_SET")
        for bad_member in (["z"], [{}], [123]):
            with self.assertRaises(plug.PluginError) as cm:
                plug.encode_panini_c1p_registry(["a", "b"], {"t": bad_member})  # type: ignore[arg-type]
            self.assertEqual(cm.exception.code, "UNKNOWN_SPINE_ITEM")

        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            self.assertEqual(plug.main([]), 0)
        res = json.loads(buf.getvalue())
        self.assertTrue(res["all_passed"])
        self.assertEqual(res["sona_576_grid_theorem"]["grids_checked"], 576)
        self.assertEqual(res["sona_576_grid_theorem"]["coprime_monolinear_count"], 359)
        self.assertEqual(res["sona_repo_watchdog_rotation"]["cycle_length"], 141)
        self.assertEqual(res["panini_14_sivasutra_c1p"]["c1p_exact_count"], 43)
        self.assertEqual(res["panini_14_sivasutra_c1p"]["tucker_c3_valid_single_copy_permutations"], 0)
        self.assertEqual(res["panini_14_sivasutra_c1p"]["descriptor_compression_ratio_q"], "21/5")
        self.assertEqual(res["panini_14_sivasutra_c1p"]["total_compression_ratio_q"], "21/8")

        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            kp = tdp / "khipu.json"
            kp.write_text(json.dumps(wrapped), encoding="utf-8")
            buf_k = io.StringIO()
            with mock.patch("sys.stdout", buf_k):
                self.assertEqual(plug.main(["--khipu-cert", str(kp)]), 0)
            self.assertEqual(json.loads(buf_k.getvalue())["khipu_archaeological_checksum"]["witness_count"], 4)

            for bad_path in (Path("a\x00b"), Path("../secret.json")):
                with self.assertRaises(plug.PluginError) as cm:
                    plug._load_json(bad_path)
                self.assertEqual(cm.exception.code, "UNSAFE_PATH")

            err = io.StringIO()
            with mock.patch("sys.stderr", err):
                self.assertEqual(plug.main(["--khipu-cert", str(tdp / "missing.json")]), 2)
            self.assertIn("READ_OR_JSON_ERR", err.getvalue())

            err_big = io.StringIO()
            with mock.patch("sys.stderr", err_big):
                self.assertEqual(plug.main(["--repo-rows", "1000000", "--repo-cols", "1"]), 2)
            self.assertIn("GRID_TOO_LARGE", err_big.getvalue())


if __name__ == "__main__":
    unittest.main()
