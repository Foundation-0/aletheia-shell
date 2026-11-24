# ASUS Zenbook Pro Duo Playbook (`RTX 3080` + `32 GiB RAM` + ScreenPad Plus)

Operational guide for running **Omarchy (Arch Linux + Hyprland + Btrfs)**, **`Moissanite-Labs/ml-os`**, and **`aletheia-shell`** on the ASUS Zenbook Pro Duo workstation.

## 1. Hybrid GPU PRIME Offload and Ring-0 `0 MiB` Desktop VRAM Lock

The Zenbook Pro Duo pairs an integrated GPU (AMD Ryzen `amdgpu` or Intel `i915`/`xe`, auto-detected by `mlos probe --json`) with a discrete NVIDIA `RTX 3080` Laptop GPU (`8 GiB` VRAM).

1. **Compositor Binding:** Pin Hyprland strictly to the iGPU DRM node (`AQ_DRM_DEVICES`) in `$HOME/.config/hypr/aletheia-zenbook-duo.conf` after confirming the iGPU card path with `ls -l /dev/dri/by-path/` and `python3 "$HOME/Projects/ml-os/bin/mlos" probe --json`.
2. **Verify `0 MiB` Desktop Overhead:** Confirm zero Wayland/desktop processes allocate discrete VRAM:
   ```bash
   nvidia-smi --query-compute-apps=pid,process_name,used_memory --format=csv
   nvidia-smi --query-gpu=memory.used,memory.total,temperature.gpu --format=csv,noheader,nounits
   ```
   `bin/toroid-hud.py --once --json --expect-zero-desktop-vram` flags any non-zero desktop VRAM allocation (`ring0_vram_alert: true`).
3. **Exclusive Compute Leases:** Always wrap CUDA training, quantization, or benchmark jobs in `ml-os` atomic leases and provenance stampers:
   ```bash
   python3 "$HOME/Projects/ml-os/bin/mlos" gpu-lease --mode train -- \
     python3 "$HOME/Projects/ml-os/bin/mlos" stamp-run --zone B --output run-receipt.json -- \
     python3 my_experiment.py
   ```

## 2. Dual-Display ScreenPad Plus Scaling and Telemetry Layout

- **Upper Primary Display (`eDP-1`):** Code editor, main coordinator terminal (`agy --resume <CID>`), Lean 4 compiler panes, and browser.
- **Lower ScreenPad Plus (`eDP-2`, Workspace `9`):** Dedicated ambient instrumentation strip:
  - Left pane: `python3 -I -B bin/toroid-hud.py --watch --auto-discover` showing live board head `seq`, sliding-window rotational transform `iota_W = N_poloidal(W) / W` (`W=50`), cartographer/weaver silence gaps, live `CS-01` / `CS-22` / `CS-23` status, RAM/swap, RTX 3080 VRAM/temp, and swarm roster.
  - Right pane: live `/swarm` board feed and `nvtop` / `btop` during GPU runs.
- **Scaling & Touch:** Set `1.5` scale on both displays in `configs/hypr-zenbook-duo.conf` (adjust to `1.25` or `2.0` depending on panel DPI) and map the lower touchscreen input device exclusively to `eDP-2`.

## 3. `32 GiB` Swarm Concurrency Profile

Upgrading from an `8–16 GiB` host to `32 GiB` RAM + `8 GiB` discrete RTX 3080 unlocks a higher-throughput yet memory-safe swarm profile:

| Parameter | Previous Host Profile | Zenbook Pro Duo (`32 GiB` + `RTX 3080`) |
|---|---|---|
| Concurrent Active Workers | `<= 8` | `12–14` active workers (`OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1` per worker) |
| Poloidal Flow Watchdog | Manual check | `systemd` user timer every `30 min` (`aletheia-toroid-watchdog.timer`), `W=50`, `iota_W > 0` |
| Tri-Continent Independent Audits | Sequential single-host | Parallel `vibe` routing (Mistral Large 4 / DeepSeek v4.1 Flash / GLM 5.3) |
| Formal Proofs & CUDA Probes | External / deferred | Local `elan` / Lean 4 builds + `mlos gpu-lease` CUDA `sm_86` runs |

Keep `OPENBLAS_NUM_THREADS=1` and `OMP_NUM_THREADS=1` exported globally so multi-worker Python verifiers do not spawn redundant BLAS thread pools.

## 4. Air-Gapped Cutover and Verification Checklist

1. Stop all `agy` / `antigravity` processes on the source host (`pgrep -f '[a]ntigravity'` returns empty).
2. Run `bin/freeze-for-migration.sh --dest /run/media/$USER/ALETHEIA_DATA/migration-bundle --full` and confirm `MIGRATION_SHA256SUMS` and `MIGRATION_RECEIPT.json` are written with zero SQLite errors.
3. Unmount `ALETHEIA_DATA` cleanly (`sync && umount /run/media/$USER/ALETHEIA_DATA`).
4. Boot Omarchy on the Zenbook Pro Duo, run `bin/bootstrap-zenbook.sh`, authenticate `agy` / `gh` if desired, and mount `ALETHEIA_DATA`.
5. Run `bin/restore-and-verify.sh --src /run/media/$USER/ALETHEIA_DATA/migration-bundle`. Restoration fails closed unless `verify_epistemic_integrity.py` reports `status: "PASS"` across all manifest hashes, every SQLite `.db`, `board.jsonl`, and `members.json`.
6. Execute the printed `agy --resume <CID>` command and start `toroid-hud.py --watch` on workspace `9`.
