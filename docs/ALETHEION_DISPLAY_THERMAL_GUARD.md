# Aletheion UX582HS Display Power-Gating & Hysteretic Thermal Guard

Safe, reversible, pure-stdlib display power-gating, pure-$\mathbb{Z}/\mathbb{Q}$ Rosen thermal bifurcation monitoring, and automatic Schmitt-trigger thermal-display governance for the **ASUS ZenBook Pro Duo `UX582HS`** (`i9-11900H` + `RTX 3080 Laptop GPU`, dual 4K OLED `eDP-1` + `DP-3` ScreenPad Plus) running Omarchy `4.5.0` Hyprland Lua.

## Components

1. **`bin/aletheion_display.py`** (`aletheion-display`):
   - Controls lower ScreenPad Plus (`DP-3`) modes (`dual`, `pad-half`, `single`, `compute`, `toggle-pad`, `cycle-compute`, `pin-on`, `pin-off`, `auto`, `status`, `self-test`) via validated Omarchy `4.5.0` Hyprland Lua `hyprctl eval` commands.
   - Enforces fail-closed Lua injection refusal (`UNSAFE_OUTPUT`, `UNSAFE_MODE`, `UNSAFE_POS`, `UNSAFE_SCALE`), unconditionally refuses disabling primary `eDP-1` (`REFUSE_DISABLE_PRIMARY`), verifies `eDP-1` stays active after live mode switches with automatic emergency recovery, clamps primary OLED brightness to a hard `>= 20%` safety floor, and persists manual-override state atomically (`0o600`, `dir_fd` `O_DIRECTORY | O_NOFOLLOW`, `S_ISREG`, `st_nlink <= 1`, `os.fsync`).
2. **`bin/aletheion_therm_display_guard.py`** (`aletheion-therm-display-guard`) & **`systemd/aletheion-therm-display-guard.service`**:
   - Low-overhead (`Nice=15`, `5 s` poll, `NoNewPrivileges=yes`) Schmitt-trigger hysteretic governor:
     - **Shed:** `T_cpu >= 88°C` OR `T_gpu >= 80°C` sustained `>= 15 s` -> saves current `intel_backlight` percent, dims primary OLED to `35%` (floor `>= 20%`), pauses `DP-3`, and emits a single `notify-send` alert.
     - **Restore:** `T_cpu <= 76°C` AND `T_gpu <= 70°C` sustained `>= 30 s` -> restores saved brightness and re-enables `DP-3`.
     - **Manual Override:** Respects `pin-on`/`pin-off` set by physical ScreenPad button or `aletheion-display toggle-pad` until `aletheion-display auto` re-enables automation.
3. **`bin/aletheion_thermal_rosen_guard.py`**:
   - Pure-$\mathbb{Z}/\mathbb{Q}$ thermal stability discriminant guard (`MAX_SUSTAINED_GPU_C = 78`, `MAX_SUSTAINED_CPU_C = 85`, `RECOMMENDED_AC_BATTERY_LIMIT_PCT = 80`), NVML throttle bitmask decoder, monotonic 8-point fan-curve validator, and reversible AC `Balanced` vs. `Performance` profiler.

## Installation & Verification

```bash
install -m 0755 bin/aletheion_display.py "$HOME/.local/bin/aletheion-display"
install -m 0755 bin/aletheion_therm_display_guard.py "$HOME/.local/bin/aletheion-therm-display-guard"
install -m 0644 systemd/aletheion-therm-display-guard.service "$XDG_CONFIG_HOME/systemd/user/"
systemctl --user daemon-reload && systemctl --user enable --now aletheion-therm-display-guard.service

# Deterministic offline verification (zero hardware mutation):
python3 -I -B tests/test_aletheion_display_thermal_guard_r5.py
python3 -I -B bin/aletheion_display.py self-test
python3 -I -B bin/aletheion_therm_display_guard.py --self-test
python3 -I -B bin/aletheion_thermal_rosen_guard.py --self-test
```
