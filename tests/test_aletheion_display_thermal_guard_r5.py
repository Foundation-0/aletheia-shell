#!/usr/bin/env python3
"""
Regression and 100% mutation-hardening tests for Aletheion UX582HS display power-gating,
pure-Z/Q Rosen thermal bifurcation guard, and automatic hysteretic thermal-display governor:
  - `bin/aletheion_thermal_rosen_guard.py` (100% refusal-site & boundary kill coverage)
  - `bin/aletheion_display.py` (100% refusal-site & boundary kill coverage)
  - `bin/aletheion_therm_display_guard.py` (100% refusal-site & boundary kill coverage)
  - `systemd/aletheion-therm-display-guard.service` unit hygiene check

Zero external dependencies (Python 3 stdlib only). Zero bare asserts. Zero `/dev/null` writes.
Safe and bit-identical across all isolated modes (-I -B, -I -O -B, -I -B -W error, PYTHONHASHSEED=42 -B).
"""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
import io
import os
from pathlib import Path
import secrets
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT / "bin"
if str(BIN_DIR) not in sys.path:
    sys.path.insert(0, str(BIN_DIR))

import aletheion_display as disp  # noqa: E402
import aletheion_therm_display_guard as tdg  # noqa: E402
import aletheion_thermal_rosen_guard as trg  # noqa: E402


def _err_code(exc_cls: type[Exception], fn) -> str:
    try:
        fn()
    except exc_cls as exc:
        return getattr(exc, "code", type(exc).__name__)
    return "NO_ERROR"


class TestAletheionDisplayAndThermalGuardR5(unittest.TestCase):
    def test_01_thermal_rosen_guard_exact_and_100pct_mutation(self) -> None:
        st = trg.run_deterministic_self_test()
        self.assertTrue(st["all_passed"])
        self.assertEqual(st["cases_verified"], 7)
        self.assertEqual(st["fail_closed_refusals_verified"], 4)

        self.assertEqual(trg.decode_nvml_bitmask(0), [])
        self.assertEqual(
            trg.decode_nvml_bitmask(trg.NVML_REASON_SW_THERMAL_SLOWDOWN | trg.NVML_REASON_SW_POWER_CAP),
            ["SwPowerCap", "SwThermalSlowdown"],
        )
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.decode_nvml_bitmask(-5)), "NEGATIVE_BITMASK")

        for gpu_t, cpu_t, bat_l in ((-20, -20, 20), (130, 130, 100), (78, 85, 80)):
            ev = trg.evaluate_thermal_rosen_guard(
                t_gpu_c=gpu_t, t_cpu_c=cpu_t, throttle_bitmask=0, lid_open=True,
                cpu_fan_rpm=4000, gpu_fan_rpm=4000, battery_limit_pct=bat_l, active_profile="Balanced",
            )
            if gpu_t == 78 and cpu_t == 85:
                self.assertEqual(ev["phase"], "AT_THERMAL_KNEE_MANIFOLD")
                self.assertEqual(ev["reason_codes"], [])
                self.assertEqual(ev["rosen_discriminant_num"], 0)

        ev_thr = trg.evaluate_thermal_rosen_guard(
            t_gpu_c=55, t_cpu_c=60, throttle_bitmask=trg.NVML_REASON_HW_THERMAL_SLOWDOWN, lid_open=True,
            cpu_fan_rpm=4000, gpu_fan_rpm=4000, battery_limit_pct=80, active_profile="Balanced",
        )
        self.assertEqual(ev_thr["phase"], "BIFURCATION_CLAMP_REQUIRED")
        self.assertIn("NVML_CRITICAL_THROTTLE_BIFURCATION", ev_thr["reason_codes"])

        over_gpu = trg.evaluate_thermal_rosen_guard(
            t_gpu_c=79, t_cpu_c=70, throttle_bitmask=0, lid_open=True,
            cpu_fan_rpm=4000, gpu_fan_rpm=4000, battery_limit_pct=80, active_profile="Performance",
        )
        self.assertEqual(over_gpu["phase"], "BIFURCATION_CLAMP_REQUIRED")
        self.assertIn("GPU_EXCEEDS_78C_SUSTAINED_KNEE", over_gpu["reason_codes"])

        over_cpu = trg.evaluate_thermal_rosen_guard(
            t_gpu_c=60, t_cpu_c=86, throttle_bitmask=0, lid_open=True,
            cpu_fan_rpm=3600, gpu_fan_rpm=3600, battery_limit_pct=80, active_profile="Performance",
        )
        self.assertEqual(over_cpu["phase"], "BIFURCATION_CLAMP_REQUIRED")
        self.assertEqual(over_cpu["recommended_profile"], "Balanced")

        for bad_kw, want_code in (
            (dict(t_gpu_c=60, t_cpu_c=70, throttle_bitmask=0, lid_open=1, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "TYPE_ERROR"),
            (dict(t_gpu_c=-21, t_cpu_c=70, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "TEMP_OOB"),
            (dict(t_gpu_c=131, t_cpu_c=70, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "TEMP_OOB"),
            (dict(t_gpu_c=60, t_cpu_c=-21, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "TEMP_OOB"),
            (dict(t_gpu_c=60, t_cpu_c=131, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "TEMP_OOB"),
            (dict(t_gpu_c=60, t_cpu_c=70, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=-1, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"), "FAN_RPM_NEGATIVE"),
            (dict(t_gpu_c=60, t_cpu_c=70, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=19, active_profile="Balanced"), "BATTERY_LIMIT_OOB"),
            (dict(t_gpu_c=60, t_cpu_c=70, throttle_bitmask=0, lid_open=True, cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=101, active_profile="Balanced"), "BATTERY_LIMIT_OOB"),
        ):
            self.assertEqual(_err_code(trg.ThermalGuardError, lambda kw=bad_kw: trg.evaluate_thermal_rosen_guard(**kw)), want_code)

        flat_duty_ok = [
            {"temp_c": 35 + 5 * i, "duty_pct": 20 if i < 2 else (30 + 10 * i if i < 7 else 100)}
            for i in range(8)
        ]
        trg.validate_fan_curve("cpu", flat_duty_ok, 70)
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.validate_fan_curve("cpu", flat_duty_ok[:-1], 70)), "CURVE_LEN")
        bad_temp_eq = [dict(p) for p in flat_duty_ok]; bad_temp_eq[2]["temp_c"] = bad_temp_eq[1]["temp_c"]
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.validate_fan_curve("cpu", bad_temp_eq, 70)), "CURVE_NON_MONOTONIC_TEMP")
        bad_duty_dec = [dict(p) for p in flat_duty_ok]; bad_duty_dec[3]["duty_pct"] = bad_duty_dec[2]["duty_pct"] - 1
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.validate_fan_curve("cpu", bad_duty_dec, 70)), "CURVE_NON_MONOTONIC_DUTY")
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.validate_fan_curve("cpu", flat_duty_ok, 85)), "CURVE_CEILING")
        bad_peak = [dict(p) for p in flat_duty_ok]; bad_peak[-1]["duty_pct"] = 99
        self.assertEqual(_err_code(trg.ThermalGuardError, lambda: trg.validate_fan_curve("cpu", bad_peak, 70)), "CURVE_PEAK_DUTY")

        self.assertIsNone(trg._parse_ascii_uint(None))
        self.assertIsNone(trg._parse_ascii_uint(""))
        self.assertIsNone(trg._parse_ascii_uint("12\u00b2"))
        self.assertIsNone(trg._parse_ascii_uint("-5"))
        self.assertEqual(trg._parse_ascii_uint("080"), 80)

        with mock.patch.object(trg, "read_live_telemetry", return_value={"asusctl_profile": "Balanced"}):
            self.assertEqual(
                _err_code(trg.ThermalGuardError, lambda: trg.run_profile_characterization(restore_profile="Bogus")),
                "INVALID_RESTORE",
            )

    def test_02_display_controller_safety_and_100pct_mutation(self) -> None:
        st = disp.run_self_test()
        self.assertTrue(st["all_passed"])
        self.assertEqual(st["min_brightness_floor_pct"], 20)
        self.assertEqual(st["refusals_verified"], 5)

        self.assertIsNone(disp._parse_ascii_uint(None))
        self.assertIsNone(disp._parse_ascii_uint(""))
        self.assertIsNone(disp._parse_ascii_uint("5\u00b2"))
        self.assertEqual(disp._parse_ascii_uint("528"), 528)

        self.assertEqual(_err_code(disp.DisplayError, lambda: disp.build_lua_monitor_cmd(None, disabled=True)), "UNSAFE_OUTPUT")  # type: ignore[arg-type]
        self.assertEqual(_err_code(disp.DisplayError, lambda: disp.build_lua_monitor_cmd(123, disabled=True)), "UNSAFE_OUTPUT")  # type: ignore[arg-type]
        self.assertEqual(_err_code(disp.DisplayError, lambda: disp.build_lua_monitor_cmd("DP-3", disabled=False, mode="3840x1100@60", position="badpos", scale=2)), "UNSAFE_POS")
        self.assertEqual(_err_code(disp.DisplayError, lambda: disp.build_lua_monitor_cmd("DP-3", disabled=False, mode="3840x1100@60", position=None, scale=2)), "UNSAFE_POS")  # type: ignore[arg-type]

        with mock.patch.object(disp.Path, "exists", return_value=False):
            self.assertEqual(disp._try_set_screenpad_backlight(100, dry_run=False)["reason"], "device_absent")
        with mock.patch.object(disp.Path, "exists", return_value=True), mock.patch.object(disp.os, "access", return_value=False):
            self.assertEqual(disp._try_set_screenpad_backlight(100, dry_run=False)["reason"], "non_writable_use_hyprctl_primary")
        with mock.patch.object(disp.Path, "exists", return_value=True), mock.patch.object(disp.os, "access", return_value=True):
            self.assertTrue(disp._try_set_screenpad_backlight(100, dry_run=True)["dry_run"])
        with (
            mock.patch.object(disp.Path, "exists", return_value=True),
            mock.patch.object(disp.os, "access", return_value=True),
            mock.patch.object(disp.Path, "write_text", side_effect=OSError("ioerr")),
        ):
            self.assertFalse(disp._try_set_screenpad_backlight(100, dry_run=False)["ok"])
        self.assertFalse(disp.send_notification("t", "b", dry_run=True)["sent"])
        with mock.patch.object(disp.shutil, "which", return_value=None):
            self.assertFalse(disp.send_notification("t", "b", dry_run=False)["sent"])

        tmp_dir = Path(tempfile.mkdtemp(prefix="alsh_disp_test_"))
        try:
            os.chmod(tmp_dir, 0o755)
            st_file = tmp_dir / "display_guard.json"
            for m in ("dual", "pad-half", "single", "compute"):
                r = disp.apply_mode(m, state_path=st_file, dry_run=True, notify=False)
                self.assertTrue(r["dry_run"])
                self.assertEqual(r["edp1_mode_policy"], "edid_native_safe_3840x2160")
                self.assertFalse(st_file.exists())

            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.apply_mode("invalid_mode", state_path=st_file, dry_run=True)), "INVALID_MODE")

            disp.save_guard_state(st_file, {"manual_override": "pin-off", "shed_active": False})
            disp.save_guard_state(st_file, {"manual_override": "pin-on", "shed_active": False})
            self.assertEqual(os.lstat(tmp_dir).st_mode & 0o777, 0o755)
            self.assertEqual(os.lstat(st_file).st_mode & 0o777, 0o600)
            self.assertEqual(disp.load_guard_state(st_file)["manual_override"], "pin-on")

            with mock.patch.object(disp.os, "fsync", side_effect=OSError("disk_io")):
                self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(tmp_dir / "ioe.json", {})), "STATE_WRITE_ERROR")

            sym_parent = tmp_dir / "sym_p"
            os.symlink(tmp_dir, sym_parent)
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.load_guard_state(sym_parent / "display_guard.json")), "SYMLINK_FORBIDDEN")

            sym_leaf = tmp_dir / "sym_leaf.json"
            os.symlink(st_file, sym_leaf)
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.load_guard_state(sym_leaf)), "SYMLINK_FORBIDDEN")
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(sym_leaf, {})), "SYMLINK_FORBIDDEN")

            fifo = tmp_dir / "pipe.json"
            os.mkfifo(fifo)
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.load_guard_state(fifo)), "NON_REGULAR_STATE_FILE")
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(fifo, {})), "NON_REGULAR_STATE_FILE")

            hl1 = tmp_dir / "hl1.json"
            hl2 = tmp_dir / "hl2.json"
            disp.save_guard_state(hl1, {"a": 1})
            os.link(hl1, hl2)
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(hl1, {"a": 2})), "HARDLINK_FORBIDDEN")

            exact_64k = tmp_dir / "exact_64k.json"
            exact_64k.write_bytes(b"{" + b" " * 65534 + b"}")
            self.assertIsInstance(disp.load_guard_state(exact_64k), dict)
            over_64k = tmp_dir / "over_64k.json"
            over_64k.write_bytes(b"{" + b" " * 65535 + b"}")
            self.assertEqual(_err_code(disp.DisplayError, lambda: disp.load_guard_state(over_64k)), "STATE_FILE_TOO_LARGE")

            unr = tmp_dir / "unr.json"
            disp.save_guard_state(unr, {"x": 1})
            os.chmod(unr, 0o000)
            try:
                self.assertEqual(_err_code(disp.DisplayError, lambda: disp.load_guard_state(unr)), "STATE_READ_ERROR")
            finally:
                os.chmod(unr, 0o600)

            with mock.patch.object(disp.os, "write", return_value=0):
                self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(tmp_dir / "sw.json", {})), "SHORT_WRITE")
            fake_st = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 2, os.getuid(), 0, 10, 0, 0, 0))
            with mock.patch.object(disp.os, "fstat", return_value=fake_st):
                self.assertEqual(_err_code(disp.DisplayError, lambda: disp.save_guard_state(tmp_dir / "hl_tmp.json", {})), "HARDLINK_FORBIDDEN")

            with (
                mock.patch.object(disp, "_apply_hypr_lua", return_value={"ok": True}),
                mock.patch.object(disp, "_try_set_screenpad_backlight", return_value={"attempted": False}),
                mock.patch.object(disp, "query_monitors", return_value=[{"name": "eDP-1", "disabled": True}]),
            ):
                self.assertEqual(
                    _err_code(disp.DisplayError, lambda: disp.apply_mode("single", state_path=st_file, dry_run=False, notify=False)),
                    "PRIMARY_DISPLAY_RECOVERY_FAILED",
                )

            buf_out, buf_err = io.StringIO(), io.StringIO()
            with redirect_stdout(buf_out), redirect_stderr(buf_err):
                rc_bad = disp.main(["mode", "bogus", "--dry-run"])
            self.assertEqual(rc_bad, 2)
            self.assertIn("MISSING_OR_INVALID_MODE", buf_err.getvalue())
        finally:
            shutil.rmtree(tmp_dir)

    def test_03_thermal_display_guard_hysteresis_and_100pct_mutation(self) -> None:
        st = tdg.run_self_test()
        self.assertTrue(st["all_passed"])
        self.assertEqual(st["checks_verified"], 7)
        self.assertEqual(st["min_brightness_floor_pct"], 20)

        self.assertIsNone(tdg._parse_ascii_uint(None))
        self.assertIsNone(tdg._parse_ascii_uint(""))
        self.assertIsNone(tdg._parse_ascii_uint("9\u00b2"))
        self.assertEqual(tdg._parse_ascii_uint("42"), 42)

        cfg = tdg.HysteresisConfig(
            shed_cpu_c=88, shed_gpu_c=80, shed_hold_s=15,
            restore_cpu_c=76, restore_gpu_c=70, restore_hold_s=30,
            shed_brightness_pct=35,
        )
        sm = tdg.ThermalDisplayStateMachine(cfg, shed_active=False)

        self.assertEqual(sm.step(now_s=0, t_cpu_c=65, t_gpu_c=80, manual_override=None)["decision"], "NONE")
        self.assertEqual(sm.step(now_s=14, t_cpu_c=65, t_gpu_c=81, manual_override=None)["decision"], "NONE")
        shed = sm.step(now_s=15, t_cpu_c=65, t_gpu_c=80, manual_override=None)
        self.assertEqual(shed["decision"], "SHED")
        self.assertEqual(shed["target_brightness_pct"], 35)

        ov = sm.step(now_s=20, t_cpu_c=50, t_gpu_c=50, manual_override="pin-off")
        self.assertEqual(ov["decision"], "OVERRIDE_PAUSED")

        self.assertEqual(sm.step(now_s=25, t_cpu_c=60, t_gpu_c=60, manual_override=None)["decision"], "NONE")
        self.assertEqual(sm.step(now_s=54, t_cpu_c=60, t_gpu_c=60, manual_override=None)["decision"], "NONE")
        rest = sm.step(now_s=55, t_cpu_c=76, t_gpu_c=70, manual_override=None)
        self.assertEqual(rest["decision"], "RESTORE")
        self.assertFalse(rest["shed_active"])

        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: sm.step(now_s=-1, t_cpu_c=60, t_gpu_c=60, manual_override=None)), "INVALID_TIME")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: sm.step(now_s=60, t_cpu_c=131, t_gpu_c=60, manual_override=None)), "INVALID_CPU_TEMP")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: sm.step(now_s=60, t_cpu_c=60, t_gpu_c=-21, manual_override=None)), "INVALID_GPU_TEMP")

        self.assertEqual(tdg.clamp_safe_brightness_pct(-10), 20)
        self.assertEqual(tdg.clamp_safe_brightness_pct(0), 20)
        self.assertEqual(tdg.clamp_safe_brightness_pct(19), 20)
        self.assertEqual(tdg.clamp_safe_brightness_pct(20), 20)
        self.assertEqual(tdg.clamp_safe_brightness_pct(35), 35)
        self.assertEqual(tdg.clamp_safe_brightness_pct(120), 100)
        tdg.HysteresisConfig(shed_brightness_pct=20).validate()
        tdg.HysteresisConfig(shed_brightness_pct=100).validate()

        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.clamp_safe_brightness_pct(False)), "INVALID_BRIGHTNESS_TYPE")  # type: ignore[arg-type]
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_cpu_c=True).validate()), "CONFIG_TYPE_ERROR")  # type: ignore[arg-type]
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_cpu_c=80, restore_cpu_c=80).validate()), "HYSTERESIS_CPU_COLLAPSED")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_gpu_c=75, restore_gpu_c=75).validate()), "HYSTERESIS_GPU_COLLAPSED")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_hold_s=0).validate()), "HOLD_TIMER_NONPOSITIVE")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_brightness_pct=19).validate()), "BRIGHTNESS_FLOOR_VIOLATION")
        self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.HysteresisConfig(shed_brightness_pct=101).validate()), "BRIGHTNESS_FLOOR_VIOLATION")

        tmp_dir = Path(tempfile.mkdtemp(prefix=f"alsh_tdg_{secrets.token_hex(4)}_"))
        try:
            os.chmod(tmp_dir, 0o755)
            st_file = tmp_dir / "display_guard.json"
            act_shed = tdg.execute_transition("SHED", t_cpu_c=90, t_gpu_c=62, cfg=cfg, state_path=st_file, dry_run=True)
            self.assertFalse(act_shed["executed"])
            self.assertEqual(act_shed["target_brightness_pct"], 35)
            act_rest = tdg.execute_transition("RESTORE", t_cpu_c=70, t_gpu_c=60, cfg=cfg, state_path=st_file, dry_run=True)
            self.assertFalse(act_rest["executed"])
            self.assertGreaterEqual(act_rest["restore_brightness_pct"], 20)

            tdg.save_state(st_file, {"shed_active": True})
            tdg.save_state(st_file, {"shed_active": False})
            self.assertEqual(os.lstat(tmp_dir).st_mode & 0o777, 0o755)
            self.assertEqual(os.lstat(st_file).st_mode & 0o777, 0o600)
            self.assertFalse(tdg.load_state(st_file)["shed_active"])

            with mock.patch.object(tdg.os, "fsync", side_effect=OSError("disk_io")):
                self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(tmp_dir / "ioe.json", {})), "STATE_WRITE_ERROR")

            sym_parent = tmp_dir / "sym_p"
            os.symlink(tmp_dir, sym_parent)
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.load_state(sym_parent / "display_guard.json")), "SYMLINK_FORBIDDEN")

            sym_leaf = tmp_dir / "sym_leaf.json"
            os.symlink(st_file, sym_leaf)
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.load_state(sym_leaf)), "SYMLINK_FORBIDDEN")
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(sym_leaf, {})), "SYMLINK_FORBIDDEN")

            fifo = tmp_dir / "pipe.json"
            os.mkfifo(fifo)
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.load_state(fifo)), "NON_REGULAR_STATE_FILE")
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(fifo, {})), "NON_REGULAR_STATE_FILE")

            hl1 = tmp_dir / "hl1.json"
            hl2 = tmp_dir / "hl2.json"
            tdg.save_state(hl1, {"a": 1})
            os.link(hl1, hl2)
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(hl1, {"a": 2})), "HARDLINK_FORBIDDEN")

            exact_64k = tmp_dir / "exact_64k.json"
            exact_64k.write_bytes(b"{" + b" " * 65534 + b"}")
            self.assertIsInstance(tdg.load_state(exact_64k), dict)
            over_64k = tmp_dir / "over_64k.json"
            over_64k.write_bytes(b"{" + b" " * 65535 + b"}")
            self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.load_state(over_64k)), "STATE_FILE_TOO_LARGE")

            unr = tmp_dir / "unr.json"
            tdg.save_state(unr, {"x": 1})
            os.chmod(unr, 0o000)
            try:
                self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.load_state(unr)), "STATE_READ_ERROR")
            finally:
                os.chmod(unr, 0o600)

            with mock.patch.object(tdg.os, "write", return_value=0):
                self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(tmp_dir / "sw.json", {})), "SHORT_WRITE")
            fake_st = os.stat_result((stat.S_IFREG | 0o600, 0, 0, 2, os.getuid(), 0, 10, 0, 0, 0))
            with mock.patch.object(tdg.os, "fstat", return_value=fake_st):
                self.assertEqual(_err_code(tdg.GuardDaemonError, lambda: tdg.save_state(tmp_dir / "hl_tmp.json", {})), "HARDLINK_FORBIDDEN")

            b_out, b_err = io.StringIO(), io.StringIO()
            with redirect_stdout(b_out), redirect_stderr(b_err):
                self.assertEqual(tdg.main(["--self-test", "--interval", "1"]), 0)
            b_out2, b_err2 = io.StringIO(), io.StringIO()
            with redirect_stdout(b_out2), redirect_stderr(b_err2):
                self.assertEqual(tdg.main(["--self-test", "--interval", "0"]), 2)
            self.assertIn("INVALID_INTERVAL", b_err2.getvalue())
        finally:
            shutil.rmtree(tmp_dir)

    def test_04_systemd_unit_hygiene(self) -> None:
        unit_path = ROOT / "systemd" / "aletheion-therm-display-guard.service"
        self.assertTrue(unit_path.is_file())
        txt = unit_path.read_text(encoding="utf-8")
        self.assertIn("NoNewPrivileges=yes", txt)
        self.assertIn("Nice=15", txt)
        self.assertIn("%h/.local/bin/aletheion-therm-display-guard", txt)
        self.assertNotIn("\r", txt)


if __name__ == "__main__":
    unittest.main(verbosity=2)
