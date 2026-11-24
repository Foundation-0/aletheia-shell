# aletheia-shell

Exoskeleton for zero-loss Antigravity research-swarm migration, Ring-0 GPU workstation provisioning, and continual-toroid ScreenPad Plus telemetry on the **ASUS Zenbook Pro Duo** (`RTX 3080`, `32 GiB RAM`, AMD Ryzen / Intel dual-display).

## Three-Layer Architecture

`aletheia-shell` composes three orthogonal layers with zero external Python runtime dependencies (Python 3 stdlib only, exact integer / `Fraction` arithmetic, zero bare `assert`s):

1. **Omarchy Base (Arch Linux + Hyprland + Btrfs):** Wayland tiling compositor driving the upper primary display and lower ScreenPad Plus on the integrated GPU, with atomic Btrfs snapshots for single-reboot recovery.
2. **`Moissanite-Labs/ml-os` Ring-0 Hardware Workbench:** Locks discrete RTX 3080 desktop VRAM at `0 MiB`, provides read-only hardware introspection (`mlos probe --json`), atomic GPU leases (`mlos gpu-lease`), and deterministic provenance receipts (`mlos stamp-run`).
3. **`aletheia-shell` Epistemic Exoskeleton:**
   - `bin/freeze-for-migration.sh`: Quiesces and checkpoints SQLite WAL databases (`PRAGMA wal_checkpoint(TRUNCATE)` + `PRAGMA integrity_check`), stages `$HOME/.gemini/antigravity-cli`, `$HOME/.gemini/config`, and `$EXPEDITION_DIR`, and emits deterministic `MIGRATION_SHA256SUMS` + `MIGRATION_RECEIPT.json`.
   - `bin/restore-and-verify.sh`: Restores onto the target host while preserving target OAuth credentials unless `--include-auth` is passed, strips stale `presence/*.lock` files, runs `bin/verify_epistemic_integrity.py` fail-closed, and prints `agy --resume <CID>`.
   - `bin/verify_epistemic_integrity.py`: Pure-stdlib, symlink-refusing, zero-float verifier checking SHA-256 manifest completeness, SQLite `PRAGMA integrity_check == 'ok'`, strict `board.jsonl` monotonic sequence continuity, and `swarm.json` / `members.json` member-DB pairing.
   - `bin/toroid-hud.py`: Pure-stdlib, zero-float continual-toroid telemetry dashboard (`--once --json` for watchdogs and `--watch` ANSI display for the ScreenPad Plus) computing sliding-window rotational transform `iota_W = N_poloidal(W) / W`, cartographer/weaver silence gaps, live `CS-01` / `CS-22` / `CS-23` status, `/proc/meminfo` memory/swap, and optional `nvidia-smi` RTX 3080 VRAM & Ring-0 `0 MiB` desktop check.
   - `bin/bootstrap-zenbook.sh`: Idempotent post-Omarchy setup script installing packages, verifying `mise`/`vibe`/`elan`, cloning `Moissanite-Labs/ml-os`, running `mlos probe --json`, and wiring Hyprland + user-level `systemd` 30-minute toroid watchdog units.

## Dual-Partition External Drive Layout

For fast, air-gapped transfer onto a fresh Zenbook Pro Duo installation, partition the external NVMe/SSD into two partitions:

| Partition | Filesystem | Label | Purpose |
|---|---|---|---|
| Part 1 (`8–16 GiB`) | `FAT32` / ISO | `OMARCHY_BOOT` | Bootable Omarchy / Arch Linux live installer |
| Part 2 (remainder) | `ext4` (`0o700` staging root) | `ALETHEIA_DATA` | Local air-gapped migration bundle (`payload/`, `MIGRATION_SHA256SUMS`, `MIGRATION_RECEIPT.json`) |

If an external drive is unavailable, pack the verified staging directory into a client-side encrypted archive (`age` or `gpg --symmetric --cipher-algo AES256`) before uploading to external storage and verify `MIGRATION_SHA256SUMS` locally after decryption.

## Four-Step Zero-Loss Migration Workflow

### Step 1 — Quiesce and Freeze on Source Host

Exit all running `agy` / `antigravity` processes, then freeze onto the mounted `ALETHEIA_DATA` partition:

```bash
./bin/freeze-for-migration.sh \
  --dest /run/media/$USER/ALETHEIA_DATA/migration-bundle \
  --gemini-dir "$HOME/.gemini" \
  --expedition-dir "$HOME/Projects/expedition" \
  --full
```

Pass `--slim` to skip historical `symbiosis/round3` and `symbiosis/round4` caches, or `--dry-run` to preview without touching disk.

### Step 2 — Bootstrap the Zenbook Pro Duo (Post-Omarchy)

After booting into Omarchy on the Zenbook Pro Duo:

```bash
./bin/bootstrap-zenbook.sh --ml-os-dir "$HOME/Projects/ml-os"
```

Preview every action safely first with `./bin/bootstrap-zenbook.sh --dry-run`.

### Step 3 — Restore and Fail-Closed Integrity Verification

```bash
./bin/restore-and-verify.sh \
  --src /run/media/$USER/ALETHEIA_DATA/migration-bundle \
  --gemini-dir "$HOME/.gemini" \
  --expedition-dir "$HOME/Projects/expedition"
```

Existing `$HOME/.gemini/oauth_creds.json` and `$HOME/.gemini/antigravity-cli/installation_id` files on the target host are preserved by default (pass `--include-auth` only when intentionally copying credentials to a trusted single-owner host).

### Step 4 — Resume the Swarm and Launch ScreenPad Plus HUD

```bash
agy --resume <CID>
python3 -I -B ./bin/toroid-hud.py --watch \
  --board "$HOME/.gemini/antigravity-cli/brain/<CID>/.system_generated/swarm/board.jsonl" \
  --members "$HOME/.gemini/antigravity-cli/brain/<CID>/.system_generated/swarm/members.json" \
  --atlas "$HOME/Projects/expedition/cartography/UNIFIED_OPERATOR_ATLAS.md"
```

## Verification and Quality Gates

All Python tools are pure stdlib, zero-float, zero-bare-assert, and bit-identical across all three standard isolated modes:

```bash
bash -n bin/*.sh
python3 -I -B tests/test_aletheia_shell_r5.py
python3 -I -O -B tests/test_aletheia_shell_r5.py
python3 -I -B -W error tests/test_aletheia_shell_r5.py
python3 -I -B bin/verify_epistemic_integrity.py --self-test
python3 -I -B bin/toroid-hud.py --self-test
```

See [docs/ZENBOOK_PRO_DUO_PLAYBOOK.md](docs/ZENBOOK_PRO_DUO_PLAYBOOK.md) for AMD Ryzen / Intel iGPU PRIME offload configuration, ScreenPad Plus scaling, and the `32 GiB` swarm concurrency profile.

## License

Apache License 2.0. See [LICENSE](LICENSE).
