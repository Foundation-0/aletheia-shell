#!/usr/bin/env python3
"""
Aletheion HQ Thermal / Power / Battery Longevity Optimizer
                    & Exact Pure-Z/Q Rosen Bifurcation Thermal Guard.

Applies an exact pure-Z/Q Rosen Bifurcation thermal stability guard to
ASUS ZenBook Pro Duo UX582HS (i9-11900H + RTX 3080 Laptop GPU, AAS Plus intake)
under ml-os Ring-0 safety invariants:
  - Battery longevity: verifies & supports safe AC 80% charge threshold (asusctl battery limit 80).
  - Exact pure-Z/Q Rosen thermal bifurcation guard: evaluates exact integer/Fraction
    headroom discriminants against GPU (78 C) and CPU (85 C) sustained knees,
    NVML clock slowdown/thermal bitmasks, and AAS Plus lid-open intake requirement.
  - Safe reversible live characterization of AC Balanced vs Performance profiles
    (wrapped in mlos gpu-lease --mode train for any GPU load).

Zero external dependencies (Python 3 stdlib only), zero bare asserts, 4-mode -I parity.
"""
from __future__ import annotations

import argparse
from fractions import Fraction
import glob
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time
from typing import Any


# Exact UX582HS sustained non-bifurcation thermal ceilings (integer degrees C)
MAX_SUSTAINED_GPU_C = 78
MAX_SUSTAINED_CPU_C = 85
GPU_TARGET_SPEC_C = 87
GPU_SLOWDOWN_CLIFF_C = 100
CPU_PROCHOT_CLIFF_C = 100
RECOMMENDED_AC_BATTERY_LIMIT_PCT = 80

# NVML Clocks Event Reason bitmask flags (NVIDIA NVML specification)
NVML_REASON_GPU_IDLE = 0x0000000000000001
NVML_REASON_SW_POWER_CAP = 0x0000000000000004
NVML_REASON_HW_SLOWDOWN = 0x0000000000000008
NVML_REASON_SW_THERMAL_SLOWDOWN = 0x0000000000000020
NVML_REASON_HW_THERMAL_SLOWDOWN = 0x0000000000000040
NVML_REASON_HW_POWER_BRAKE = 0x0000000000000080

THROTTLE_CRITICAL_MASK = (
    NVML_REASON_HW_SLOWDOWN
    | NVML_REASON_SW_THERMAL_SLOWDOWN
    | NVML_REASON_HW_THERMAL_SLOWDOWN
    | NVML_REASON_HW_POWER_BRAKE
)

VALID_ASUS_PROFILES = ("Quiet", "Balanced", "Performance")


class ThermalGuardError(ValueError):
    """Structured fail-closed error for thermal guard validation."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


def require_cond(cond: bool, code: str, message: str) -> None:
    if not cond:
        raise ThermalGuardError(code, message)


def _check_exact_int(val: Any, name: str) -> int:
    if type(val) is not int:
        raise ThermalGuardError("TYPE_ERROR", f"{name} must be exact int, got {type(val).__name__}")
    return val


def _check_exact_bool(val: Any, name: str) -> bool:
    if type(val) is not bool:
        raise ThermalGuardError("TYPE_ERROR", f"{name} must be exact bool, got {type(val).__name__}")
    return val


def decode_nvml_bitmask(bitmask: int) -> list[str]:
    bitmask = _check_exact_int(bitmask, "throttle_bitmask")
    require_cond(bitmask >= 0, "NEGATIVE_BITMASK", "throttle_bitmask must be non-negative")
    reasons: list[str] = []
    flag_map = (
        (NVML_REASON_GPU_IDLE, "GpuIdle"),
        (NVML_REASON_SW_POWER_CAP, "SwPowerCap"),
        (NVML_REASON_HW_SLOWDOWN, "HwSlowdown"),
        (NVML_REASON_SW_THERMAL_SLOWDOWN, "SwThermalSlowdown"),
        (NVML_REASON_HW_THERMAL_SLOWDOWN, "HwThermalSlowdown"),
        (NVML_REASON_HW_POWER_BRAKE, "HwPowerBrakeSlowdown"),
    )
    for mask, label in flag_map:
        if (bitmask & mask) != 0:
            reasons.append(label)
    return reasons


def evaluate_thermal_rosen_guard(
    *,
    t_gpu_c: int,
    t_cpu_c: int,
    throttle_bitmask: int,
    lid_open: bool,
    cpu_fan_rpm: int,
    gpu_fan_rpm: int,
    battery_limit_pct: int,
    active_profile: str,
) -> dict[str, Any]:
    """
    Exact pure-Z/Q Rosen Bifurcation Guard for UX582HS thermal stability.

    Computes exact rational stability margins (zero floats):
      - delta_gpu = (MAX_SUSTAINED_GPU_C - t_gpu_c) / MAX_SUSTAINED_GPU_C
      - delta_cpu = (MAX_SUSTAINED_CPU_C - t_cpu_c) / MAX_SUSTAINED_CPU_C
      - Bifurcation discriminant D = min(delta_gpu, delta_cpu):
          D > 0 and zero critical throttle bits and lid_open -> WITHIN_STABLE_PHASE
          D == 0 -> AT_THERMAL_KNEE_MANIFOLD (proactive step-down recommended)
          D < 0 or critical throttle bit or not lid_open -> BIFURCATION_CLAMP_REQUIRED
    """
    t_gpu_c = _check_exact_int(t_gpu_c, "t_gpu_c")
    t_cpu_c = _check_exact_int(t_cpu_c, "t_cpu_c")
    throttle_bitmask = _check_exact_int(throttle_bitmask, "throttle_bitmask")
    lid_open = _check_exact_bool(lid_open, "lid_open")
    cpu_fan_rpm = _check_exact_int(cpu_fan_rpm, "cpu_fan_rpm")
    gpu_fan_rpm = _check_exact_int(gpu_fan_rpm, "gpu_fan_rpm")
    battery_limit_pct = _check_exact_int(battery_limit_pct, "battery_limit_pct")

    require_cond(-20 <= t_gpu_c <= 130, "TEMP_OOB", f"t_gpu_c={t_gpu_c} out of physical bounds")
    require_cond(-20 <= t_cpu_c <= 130, "TEMP_OOB", f"t_cpu_c={t_cpu_c} out of physical bounds")
    require_cond(cpu_fan_rpm >= 0 and gpu_fan_rpm >= 0, "FAN_RPM_NEGATIVE", "fan RPM cannot be negative")
    require_cond(20 <= battery_limit_pct <= 100, "BATTERY_LIMIT_OOB", f"battery_limit_pct={battery_limit_pct}")
    require_cond(
        active_profile in VALID_ASUS_PROFILES,
        "INVALID_PROFILE",
        f"active_profile must be in {VALID_ASUS_PROFILES}, got {active_profile!r}",
    )

    reasons: list[str] = []
    if not lid_open:
        reasons.append("AAS_PLUS_LID_CLOSED_INTAKE_OBSTRUCTION")
    if (throttle_bitmask & THROTTLE_CRITICAL_MASK) != 0:
        reasons.append("NVML_CRITICAL_THROTTLE_BIFURCATION")
    if t_gpu_c > MAX_SUSTAINED_GPU_C:
        reasons.append("GPU_EXCEEDS_78C_SUSTAINED_KNEE")
    if t_cpu_c > MAX_SUSTAINED_CPU_C:
        reasons.append("CPU_EXCEEDS_85C_SUSTAINED_KNEE")

    gpu_margin_c = MAX_SUSTAINED_GPU_C - t_gpu_c
    cpu_margin_c = MAX_SUSTAINED_CPU_C - t_cpu_c
    gpu_disc = Fraction(gpu_margin_c, MAX_SUSTAINED_GPU_C)
    cpu_disc = Fraction(cpu_margin_c, MAX_SUSTAINED_CPU_C)
    rosen_disc = gpu_disc if gpu_disc <= cpu_disc else cpu_disc

    advisories: list[str] = []
    if battery_limit_pct > RECOMMENDED_AC_BATTERY_LIMIT_PCT:
        advisories.append("BATTERY_AC_WEAR_RISK_SET_LIMIT_80")
    if (throttle_bitmask & NVML_REASON_SW_POWER_CAP) != 0:
        advisories.append("GPU_SW_POWER_CAP_ACTIVE")
    if t_gpu_c >= 70 and gpu_fan_rpm == 0:
        reasons.append("GPU_FAN_STALL_HAZARD")

    if len(reasons) > 0:
        phase = "BIFURCATION_CLAMP_REQUIRED"
        if not lid_open:
            recommended_profile = "Quiet"
            recommended_dgpu_cap_w = 70
        elif t_gpu_c > MAX_SUSTAINED_GPU_C or (throttle_bitmask & THROTTLE_CRITICAL_MASK) != 0:
            recommended_profile = "Performance"
            recommended_dgpu_cap_w = 85
        else:
            # CPU > 85 C under sustained multi-core load: Balanced reduces PL1/PL2 thermal overshoot
            recommended_profile = "Balanced"
            recommended_dgpu_cap_w = 90
    elif rosen_disc == 0:
        phase = "AT_THERMAL_KNEE_MANIFOLD"
        recommended_profile = "Balanced"
        recommended_dgpu_cap_w = 90
    else:
        phase = "WITHIN_STABLE_PHASE"
        recommended_profile = active_profile
        recommended_dgpu_cap_w = 95 if active_profile == "Performance" else 90

    return {
        "phase": phase,
        "safe_sustained": len(reasons) == 0 and rosen_disc > 0,
        "reason_codes": reasons,
        "advisories": advisories,
        "decoded_nvml_reasons": decode_nvml_bitmask(throttle_bitmask),
        "gpu_margin_c": gpu_margin_c,
        "cpu_margin_c": cpu_margin_c,
        "gpu_discriminant_num": gpu_disc.numerator,
        "gpu_discriminant_den": gpu_disc.denominator,
        "cpu_discriminant_num": cpu_disc.numerator,
        "cpu_discriminant_den": cpu_disc.denominator,
        "rosen_discriminant_num": rosen_disc.numerator,
        "rosen_discriminant_den": rosen_disc.denominator,
        "recommended_profile": recommended_profile,
        "recommended_dgpu_power_cap_w": recommended_dgpu_cap_w,
        "battery_longevity_protected": battery_limit_pct <= RECOMMENDED_AC_BATTERY_LIMIT_PCT,
    }


def validate_fan_curve(label: str, curve: list[dict[str, int]], ceiling: int) -> None:
    """Verify strict 8-point monotonic temperature, non-decreasing duty, and peak ceiling invariants."""
    require_cond(len(curve) == 8, "CURVE_LEN", f"{label} curve must have 8 points")
    for idx in range(1, len(curve)):
        require_cond(
            curve[idx]["temp_c"] > curve[idx - 1]["temp_c"],
            "CURVE_NON_MONOTONIC_TEMP",
            f"{label} temp_c must strictly increase",
        )
        require_cond(
            curve[idx]["duty_pct"] >= curve[idx - 1]["duty_pct"],
            "CURVE_NON_MONOTONIC_DUTY",
            f"{label} duty_pct must be non-decreasing",
        )
    require_cond(curve[-1]["temp_c"] == ceiling, "CURVE_CEILING", f"{label} peak must hit {ceiling}C")
    require_cond(curve[-1]["duty_pct"] == 100, "CURVE_PEAK_DUTY", f"{label} peak must hit 100%")


def build_anti_bifurcation_fan_policy() -> dict[str, Any]:
    """
    Smooth monotonic proactive thermal-policy & fan-curve specification for UX582HS.
    Hardware truth note: UX582HS ACPI/EC exposes xyz.ljones.Platform (ThrottlePolicy +
    ChargeControlEndThreshold) but NOT xyz.ljones.FanCurves; the 8-point curves below
    encode the exact monotonic target profile for firmware/EC-capable ASUS platforms
    paired with UX582HS's sanctioned ThrottlePolicy + RAPL/EPP + dGPU cap controls.
    """
    cpu_curve = [
        {"temp_c": 35, "duty_pct": 20},
        {"temp_c": 45, "duty_pct": 30},
        {"temp_c": 55, "duty_pct": 45},
        {"temp_c": 65, "duty_pct": 62},
        {"temp_c": 72, "duty_pct": 76},
        {"temp_c": 78, "duty_pct": 88},
        {"temp_c": 83, "duty_pct": 96},
        {"temp_c": 85, "duty_pct": 100},
    ]
    gpu_curve = [
        {"temp_c": 35, "duty_pct": 20},
        {"temp_c": 45, "duty_pct": 32},
        {"temp_c": 55, "duty_pct": 50},
        {"temp_c": 62, "duty_pct": 66},
        {"temp_c": 68, "duty_pct": 80},
        {"temp_c": 73, "duty_pct": 92},
        {"temp_c": 76, "duty_pct": 98},
        {"temp_c": 78, "duty_pct": 100},
    ]
    for label, curve, ceiling in (("cpu", cpu_curve, MAX_SUSTAINED_CPU_C), ("gpu", gpu_curve, MAX_SUSTAINED_GPU_C)):
        validate_fan_curve(label, curve, ceiling)

    cpu_str = ",".join(f"{p['temp_c']}c:{p['duty_pct']}%" for p in cpu_curve)
    gpu_str = ",".join(f"{p['temp_c']}c:{p['duty_pct']}%" for p in gpu_curve)
    return {
        "schema": "aletheion-thermal-rosen-policy/v1",
        "target_hardware": "ASUS ZenBook Pro Duo UX582HS (i9-11900H + RTX 3080 Laptop GPU)",
        "sustained_ceilings_c": {"gpu": MAX_SUSTAINED_GPU_C, "cpu": MAX_SUSTAINED_CPU_C},
        "battery_charge_limit_pct": RECOMMENDED_AC_BATTERY_LIMIT_PCT,
        "asusctl_fan_curve_cpu_spec": cpu_str,
        "asusctl_fan_curve_gpu_spec": gpu_str,
        "cpu_curve": cpu_curve,
        "gpu_curve": gpu_curve,
    }


def _parse_ascii_uint(raw: str | None) -> int | None:
    """Strictly parse ASCII base-10 non-negative integer (rejecting Unicode digits like '²')."""
    if raw is None or len(raw) == 0:
        return None
    for ch in raw:
        if ch < "0" or ch > "9":
            return None
    return int(raw)


def _read_text(path: str) -> str:
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _run_cmd(cmd: list[str], timeout: int = 10) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, "", str(exc)


def _find_hwmon_by_name(target_name: str) -> Path | None:
    for p in sorted(glob.glob("/sys/class/hwmon/hwmon*/name")):
        if _read_text(p) == target_name:
            return Path(p).parent
    return None


def read_live_telemetry() -> dict[str, Any]:
    """Read-only live hardware telemetry snapshot (zero disk or sysfs mutations)."""
    # 1. ASUS profile & battery
    rc_prof, out_prof, _ = _run_cmd(["asusctl", "profile", "get"])
    active_profile = "Balanced"
    if rc_prof == 0:
        m = re.search(r"Active profile:\s*(\w+)", out_prof)
        if m and m.group(1) in VALID_ASUS_PROFILES:
            active_profile = m.group(1)

    rc_fc, out_fc, err_fc = _run_cmd(["asusctl", "fan-curve", "--get-enabled"])
    fc_combined = f"{out_fc}\n{err_fc}".strip()
    fan_curve_iface_supported = (rc_fc == 0 and "UnknownInterface" not in fc_combined and "Error:" not in fc_combined)

    bat_limit_v = _parse_ascii_uint(_read_text("/sys/class/power_supply/BAT0/charge_control_end_threshold"))
    bat_limit = bat_limit_v if bat_limit_v is not None else 100
    bat_cap_v = _parse_ascii_uint(_read_text("/sys/class/power_supply/BAT0/capacity"))
    bat_full_v = _parse_ascii_uint(_read_text("/sys/class/power_supply/BAT0/energy_full"))
    bat_design_v = _parse_ascii_uint(_read_text("/sys/class/power_supply/BAT0/energy_full_design"))
    bat_cycles_v = _parse_ascii_uint(_read_text("/sys/class/power_supply/BAT0/cycle_count"))
    bat_status = _read_text("/sys/class/power_supply/BAT0/status") or "unknown"

    # 2. Lid state (AAS Plus intake guard)
    lid_open = True
    lid_state_raw = "unknown"
    for lid_file in sorted(glob.glob("/proc/acpi/button/lid/*/state")):
        txt = _read_text(lid_file)
        if txt:
            lid_state_raw = txt
            if "closed" in txt.lower():
                lid_open = False

    # 3. Fans & CPU coretemp
    asus_hw = _find_hwmon_by_name("asus")
    cpu_fan_rpm = 0
    gpu_fan_rpm = 0
    if asus_hw is not None:
        f1 = _parse_ascii_uint(_read_text(str(asus_hw / "fan1_input")))
        f2 = _parse_ascii_uint(_read_text(str(asus_hw / "fan2_input")))
        cpu_fan_rpm = f1 if f1 is not None else 0
        gpu_fan_rpm = f2 if f2 is not None else 0

    core_hw = _find_hwmon_by_name("coretemp")
    cpu_pkg_c = 0
    cpu_max_core_c = 0
    if core_hw is not None:
        pkg = _parse_ascii_uint(_read_text(str(core_hw / "temp1_input")))
        if pkg is not None:
            cpu_pkg_c = pkg // 1000
        for tf in sorted(core_hw.glob("temp*_input")):
            v = _parse_ascii_uint(_read_text(str(tf)))
            if v is not None:
                c = v // 1000
                if c > cpu_max_core_c:
                    cpu_max_core_c = c

    # 4. CPU frequency, governor, EPP, RAPL limits
    epp = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/energy_performance_preference") or "unknown"
    gov = _read_text("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor") or "unknown"
    freqs_khz: list[int] = []
    for cf in sorted(glob.glob("/sys/devices/system/cpu/cpu*/cpufreq/scaling_cur_freq")):
        v = _parse_ascii_uint(_read_text(cf))
        if v is not None:
            freqs_khz.append(v)
    avg_cpu_mhz = (sum(freqs_khz) // len(freqs_khz)) // 1000 if freqs_khz else 0
    max_cpu_mhz = max(freqs_khz) // 1000 if freqs_khz else 0

    rapl_pl1_v = _parse_ascii_uint(_read_text("/sys/class/powercap/intel-rapl:0/constraint_0_power_limit_uw"))
    rapl_pl2_v = _parse_ascii_uint(_read_text("/sys/class/powercap/intel-rapl:0/constraint_1_power_limit_uw"))
    rapl_pl1_w = rapl_pl1_v // 1_000_000 if rapl_pl1_v is not None else 0
    rapl_pl2_w = rapl_pl2_v // 1_000_000 if rapl_pl2_v is not None else 0

    # 5. NVIDIA GPU telemetry
    gpu_temp_c = 0
    gpu_sm_mhz = 0
    gpu_mem_mhz = 0
    gpu_pstate = "unknown"
    gpu_power_inst_w = "unknown"
    gpu_power_ceil_w = "unknown"
    throttle_mask = 0
    sw_therm_us = 0
    hw_therm_us = 0
    sw_pcap_us = 0

    rc_nv, out_nv, _ = _run_cmd([
        "nvidia-smi",
        "--query-gpu=temperature.gpu,pstate,clocks.sm,clocks.mem,clocks_event_reasons.active",
        "--format=csv,noheader,nounits",
    ])
    if rc_nv == 0 and out_nv:
        parts = [p.strip() for p in out_nv.splitlines()[0].split(",")]
        if len(parts) >= 5:
            gt_v = _parse_ascii_uint(parts[0])
            sm_v = _parse_ascii_uint(parts[2])
            mem_v = _parse_ascii_uint(parts[3])
            gpu_temp_c = gt_v if gt_v is not None else 0
            gpu_pstate = parts[1]
            gpu_sm_mhz = sm_v if sm_v is not None else 0
            gpu_mem_mhz = mem_v if mem_v is not None else 0
            if parts[4].startswith("0x"):
                throttle_mask = int(parts[4], 16)

    rc_q, out_q, _ = _run_cmd(["nvidia-smi", "-q", "-d", "POWER,PERFORMANCE"])
    if rc_q == 0 and out_q:
        m_ip = re.search(r"Instantaneous Power Draw\s*:\s*([0-9.]+ W)", out_q)
        if m_ip:
            gpu_power_inst_w = m_ip.group(1)
        m_cp = re.search(r"Current Power Limit\s*:\s*([0-9.]+ W)", out_q)
        if m_cp:
            gpu_power_ceil_w = m_cp.group(1)
        m_st = re.search(r"SW Thermal Slowdown\s*:\s*(\d+)\s*us", out_q)
        if m_st:
            sw_therm_us = int(m_st.group(1))
        m_ht = re.search(r"HW Thermal Slowdown\s*:\s*(\d+)\s*us", out_q)
        if m_ht:
            hw_therm_us = int(m_ht.group(1))
        m_sp = re.search(r"SW Power Capping\s*:\s*(\d+)\s*us", out_q)
        if m_sp:
            sw_pcap_us = int(m_sp.group(1))

    guard = evaluate_thermal_rosen_guard(
        t_gpu_c=gpu_temp_c,
        t_cpu_c=cpu_pkg_c,
        throttle_bitmask=throttle_mask,
        lid_open=lid_open,
        cpu_fan_rpm=cpu_fan_rpm,
        gpu_fan_rpm=gpu_fan_rpm,
        battery_limit_pct=bat_limit,
        active_profile=active_profile,
    )

    return {
        "timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "asusctl_profile": active_profile,
        "asusctl_fan_curve_dbus_supported": fan_curve_iface_supported,
        "asusctl_fan_curve_probe_stderr": fc_combined if not fan_curve_iface_supported else "",
        "lid_open": lid_open,
        "lid_state_raw": lid_state_raw,
        "battery": {
            "charge_control_end_threshold_pct": bat_limit,
            "capacity_pct": bat_cap_v if bat_cap_v is not None else 0,
            "status": bat_status,
            "energy_full_uwh": bat_full_v if bat_full_v is not None else 0,
            "energy_full_design_uwh": bat_design_v if bat_design_v is not None else 0,
            "cycle_count": bat_cycles_v if bat_cycles_v is not None else 0,
        },
        "cpu": {
            "package_temp_c": cpu_pkg_c,
            "max_core_temp_c": cpu_max_core_c,
            "fan_rpm": cpu_fan_rpm,
            "governor": gov,
            "epp": epp,
            "avg_freq_mhz": avg_cpu_mhz,
            "max_freq_mhz": max_cpu_mhz,
            "rapl_pl1_w": rapl_pl1_w,
            "rapl_pl2_w": rapl_pl2_w,
        },
        "gpu": {
            "temp_c": gpu_temp_c,
            "fan_rpm": gpu_fan_rpm,
            "pstate": gpu_pstate,
            "sm_clock_mhz": gpu_sm_mhz,
            "mem_clock_mhz": gpu_mem_mhz,
            "instantaneous_power": gpu_power_inst_w,
            "current_power_limit": gpu_power_ceil_w,
            "clocks_event_bitmask_hex": hex(throttle_mask),
            "sw_thermal_slowdown_us": sw_therm_us,
            "hw_thermal_slowdown_us": hw_therm_us,
            "sw_power_capping_us": sw_pcap_us,
        },
        "rosen_guard": guard,
    }


def run_deterministic_self_test() -> dict[str, Any]:
    """Pure-Z/Q deterministic verification of the Rosen Thermal Bifurcation Guard (zero I/O)."""
    policy = build_anti_bifurcation_fan_policy()
    cases = [
        (
            "safe_ac_balanced",
            dict(t_gpu_c=64, t_cpu_c=74, throttle_bitmask=0, lid_open=True,
                 cpu_fan_rpm=3600, gpu_fan_rpm=3200, battery_limit_pct=80, active_profile="Balanced"),
            "WITHIN_STABLE_PHASE", True, True,
        ),
        (
            "safe_ac_performance_wear_advisory",
            dict(t_gpu_c=68, t_cpu_c=79, throttle_bitmask=0, lid_open=True,
                 cpu_fan_rpm=4200, gpu_fan_rpm=3800, battery_limit_pct=100, active_profile="Performance"),
            "WITHIN_STABLE_PHASE", True, False,
        ),
        (
            "at_exact_knee_manifold",
            dict(t_gpu_c=78, t_cpu_c=80, throttle_bitmask=0, lid_open=True,
                 cpu_fan_rpm=4500, gpu_fan_rpm=4300, battery_limit_pct=80, active_profile="Performance"),
            "AT_THERMAL_KNEE_MANIFOLD", False, True,
        ),
        (
            "cpu_94c_overshoot_bifurcation",
            dict(t_gpu_c=58, t_cpu_c=94, throttle_bitmask=0, lid_open=True,
                 cpu_fan_rpm=3400, gpu_fan_rpm=0, battery_limit_pct=80, active_profile="Performance"),
            "BIFURCATION_CLAMP_REQUIRED", False, True,
        ),
        (
            "gpu_sw_thermal_slowdown_bifurcation",
            dict(t_gpu_c=83, t_cpu_c=76, throttle_bitmask=NVML_REASON_SW_THERMAL_SLOWDOWN, lid_open=True,
                 cpu_fan_rpm=4500, gpu_fan_rpm=4600, battery_limit_pct=80, active_profile="Performance"),
            "BIFURCATION_CLAMP_REQUIRED", False, True,
        ),
        (
            "aas_plus_lid_closed_obstruction",
            dict(t_gpu_c=60, t_cpu_c=70, throttle_bitmask=0, lid_open=False,
                 cpu_fan_rpm=3000, gpu_fan_rpm=2800, battery_limit_pct=80, active_profile="Performance"),
            "BIFURCATION_CLAMP_REQUIRED", False, True,
        ),
        (
            "gpu_fan_stall_at_72c",
            dict(t_gpu_c=72, t_cpu_c=70, throttle_bitmask=0, lid_open=True,
                 cpu_fan_rpm=3500, gpu_fan_rpm=0, battery_limit_pct=80, active_profile="Balanced"),
            "BIFURCATION_CLAMP_REQUIRED", False, True,
        ),
    ]

    case_results: list[dict[str, Any]] = []
    all_ok = True
    for name, kwargs, exp_phase, exp_safe, exp_bat_ok in cases:
        res = evaluate_thermal_rosen_guard(**kwargs)
        ok = (
            res["phase"] == exp_phase
            and res["safe_sustained"] == exp_safe
            and res["battery_longevity_protected"] == exp_bat_ok
        )
        if not ok:
            all_ok = False
        case_results.append({"case": name, "ok": ok, "evaluation": res})

    # Verify fail-closed input refusal
    refusals_ok = True
    for bad_kwargs in (
        dict(t_gpu_c=True, t_cpu_c=60, throttle_bitmask=0, lid_open=True,
             cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"),
        dict(t_gpu_c=60, t_cpu_c=60, throttle_bitmask=-1, lid_open=True,
             cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="Balanced"),
        dict(t_gpu_c=60, t_cpu_c=60, throttle_bitmask=0, lid_open=True,
             cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=80, active_profile="TurboBogus"),
        dict(t_gpu_c=60, t_cpu_c=60, throttle_bitmask=0, lid_open=True,
             cpu_fan_rpm=3000, gpu_fan_rpm=3000, battery_limit_pct=15, active_profile="Balanced"),
    ):
        try:
            evaluate_thermal_rosen_guard(**bad_kwargs)  # type: ignore[arg-type]
            refusals_ok = False
        except ThermalGuardError:
            pass

    payload = {
        "schema": "aletheion-thermal-rosen-selftest/v1",
        "all_passed": all_ok and refusals_ok,
        "cases_verified": len(case_results),
        "fail_closed_refusals_verified": 4,
        "policy": policy,
        "cases": case_results,
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    payload["payload_sha256"] = hashlib.sha256(raw).hexdigest()
    return payload


def run_profile_characterization(
    *,
    cuda_bin: str | None = None,
    mlos_bin: str | None = None,
    restore_profile: str | None = None,
) -> dict[str, Any]:
    """
    Safely & reversibly characterize AC Balanced vs Performance profiles on UX582HS.
    Any GPU workload is strictly wrapped inside Ring-0 `mlos gpu-lease --mode train`.
    """
    resolved_mlos = mlos_bin or shutil.which("mlos") or str(Path.home() / "Projects" / "ml-os" / "bin" / "mlos")
    pre = read_live_telemetry()
    orig_profile = pre["asusctl_profile"]
    target_restore = restore_profile or orig_profile
    require_cond(target_restore in VALID_ASUS_PROFILES, "INVALID_RESTORE", f"bad restore profile {target_restore!r}")

    profiles_tested: list[dict[str, Any]] = []
    try:
        for prof in ("Balanced", "Performance"):
            rc_set, out_set, err_set = _run_cmd(["asusctl", "profile", "set", prof])
            time.sleep(2.0)
            idle_snap = read_live_telemetry()

            gpu_run_info: dict[str, Any] = {"ran": False}
            if cuda_bin and Path(cuda_bin).is_file():
                t0 = time.monotonic()
                rc_g, out_g, err_g = _run_cmd(
                    [resolved_mlos, "gpu-lease", "--mode", "train", "--", cuda_bin],
                    timeout=30,
                )
                dt_s = round(time.monotonic() - t0, 3)
                peak_triad = None
                peak_a16 = None
                if rc_g == 0 and out_g:
                    try:
                        parsed = json.loads(out_g)
                        peak_triad = parsed["device"]["stream_triad_gb_s"]
                        peak_a16 = parsed["fused_gemv_4_5_bpw"]["8192x14336"]["arms"]["A16_preenc_dp4a_r2_l2pin"]["gweights_per_s"]
                    except (KeyError, ValueError):
                        pass
                gpu_run_info = {
                    "ran": True,
                    "rc": rc_g,
                    "elapsed_s": dt_s,
                    "stream_triad_gb_s": peak_triad,
                    "a16_8192x14336_gweights_s": peak_a16,
                    "stderr_tail": err_g[-200:] if err_g else "",
                }

            post_load_snap = read_live_telemetry()
            profiles_tested.append({
                "profile": prof,
                "set_rc": rc_set,
                "set_out": out_set,
                "set_err": err_set,
                "idle_snapshot": idle_snap,
                "gpu_load_probe": gpu_run_info,
                "post_load_snapshot": post_load_snap,
            })
    finally:
        _run_cmd(["asusctl", "profile", "set", target_restore])
        time.sleep(1.0)

    final_snap = read_live_telemetry()
    return {
        "schema": "aletheion-thermal-characterization/v1",
        "pre_snapshot": pre,
        "profiles_tested": profiles_tested,
        "restored_profile": target_restore,
        "final_snapshot": final_snap,
        "policy": build_anti_bifurcation_fan_policy(),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Aletheion HQ Thermal Rosen Guard & Profiler")
    parser.add_argument("--self-test", action="store_true", help="Run pure-Z/Q deterministic self-test (default)")
    parser.add_argument("--probe-live", action="store_true", help="Capture read-only live hardware telemetry")
    parser.add_argument("--characterize", action="store_true", help="Safely characterize AC Balanced vs Performance")
    parser.add_argument("--cuda-bin", default=None, help="Optional CUDA benchmark binary for GPU lease load probe")
    parser.add_argument("--restore-profile", default=None, help="Profile to restore after --characterize")
    parser.add_argument("--output", default=None, help="Optional JSON output file path")
    args = parser.parse_args(argv)

    try:
        if args.characterize:
            result = run_profile_characterization(
                cuda_bin=args.cuda_bin,
                restore_profile=args.restore_profile,
            )
        elif args.probe_live:
            result = read_live_telemetry()
        else:
            result = run_deterministic_self_test()
    except ThermalGuardError as exc:
        sys.stderr.write(f"ERROR {exc}\n")
        return 2

    out_text = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        Path(args.output).write_text(out_text, encoding="utf-8")
    else:
        sys.stdout.write(out_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
