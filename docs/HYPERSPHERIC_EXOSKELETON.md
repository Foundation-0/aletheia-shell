# Aletheion $S^3$ Hyperspheric Epistemic Exoskeleton

Pure-Python-stdlib, zero-float in `bin/` (`0` IEEE-754 float literals in `bin/*.py` ASTs, `0` bare `assert`s, `0` external dependencies; `tests/test_hyperspheric_exoskeleton_r5.py` includes float literals solely as must-fail negative controls) telemetry HUD, offline pure-$\mathbb{Z}/\mathbb{Q}$ thermal hysteresis simulator/verifier, and combinatorial verification plugins extending `aletheion`.

> **Telemetry Scope Policy (`TEMPERATURE_AND_CHATTER_ONLY`):** All thermal and HUD modules in this package strictly model and report temperature stability (`deg C`) and state-switch chatter counts. Uncontrolled laptop energy or wattage claims are forbidden and rejected fail-closed across both `bin/hyperspheric-hud.py` and `bin/aletheion_hysteresis_governor.py` via NFKC + NFKD + `Cf`/`Mn` stripped, non-ASCII-letter-rejecting, camelCase/underscore-aware key and value validation covering the W/J/Wh/Whr/TDP family, per-token claims, and relative energy/power-saving prose. Unit-first forms require an explicit `:`/`=`/`_` delimiter so standard identifiers (`J2000`, `2024-W05`, `worker W10`, `W12`) are not false-flagged; non-W/J units (`kcal`, `BTU`, `hp`, `eV`) and spelled-out numbers (`forty-five watts`) are outside automated regex scope by design.

---

## Components

### 1. `bin/hyperspheric-hud.py` — $S^3$ Hyperspheric Epistemic Telemetry HUD
Complements `bin/toroid-hud.py` with exact pure-$\mathbb{Z}/\mathbb{Q}$ epistemic-graph integration, canonical JSON output, and a compact 4-line plain-text summary formatted for a ScreenPad Plus terminal pane (`--screenpad`):
- **Epistemic Web Index Integration (`--once --web-index <path>`):** Computes exact rational (`Fraction`) historical interposition share, cross-org bridge-pair density, and mean cross-org degree without floating-point rounding (`index_sha256` validated as lowercase 64-char hex). Passing `--once` without `--web-index` (or vice versa) fails closed with exit code `2`.
- **Message-Board & Roster Dynamics (`--board`, `--members`, `--poloidal-roles`):** Tracks poloidal integration messages, `[done]` completions, and active roster slots.
- **Host-Contention Environment Hook (`--contention-json`):** Fail-closed validation of optional contention receipts (`contended` strict bool, allowlisted `power_band` in `{"ac", "battery", "unknown"}`, zero uncontrolled energy claims).
- **Hybrid Epistemic RAG SQLite Hook (`--rag-db`, `--rag-query`):** Read-only (`mode=ro`) SQLite inspection of `nodes`, `edges`, `dense_embeddings`, `edge_type` histograms, `meta.schema_version`, and parameterized deterministic `fts_chunks` FTS5 query matches.
- **Cryptographic & Schema Integrity:** Validates nested schemas for `hyperspheric_web`, `board_dynamics`, `contention_hook`, and `rag_hook`, plus canonical `snapshot_sha256` via `hmac.compare_digest` with deterministic default `captured_unix_s = 0` (opt-in `--live-clock` for wall-clock timestamps).

### 2. `bin/aletheion_hysteresis_governor.py` — Offline Pure-$\mathbb{Z}/\mathbb{Q}$ Thermal Hysteresis Simulator & Verifier
Offline pure-$\mathbb{Z}/\mathbb{Q}$ thermal-ladder simulator, single-step gear transition function, and self-verifying certificate generator for UX582HS temperature traces (`70/78/85 deg C` upward knees, `65/73/80 deg C` downward knees, exact `5 deg C` deadband; does not directly mutate hardware or act as a live daemon):
- **Stepped-Impedance 2-Chamber Filter (Petra conduit impedance law):** $y_{t+1} = y_t + (T_t - y_t)/k_1,\; z_{t+1} = z_t + (y_{t+1} - z_t)/k_2$ ($k_1, k_2 \ge 2$). Damps a 1-tick $+6\,^\circ\mathrm{C}$ spike ($66 \to 72 \to 66\,^\circ\mathrm{C}$) to exact peak `135/2 = 67.5 deg C < 70 deg C` (`0` switches vs `2` bang-bang switches).
- **Asymmetric Float-Valve Deadband Ladder (Al-Jazarī hysteresis law):** Eliminates limit-cycle chatter on a simulated 40-tick $78 \pm 2\,^\circ\mathrm{C}$ boundary oscillation (`0` hysteresis switches vs `39` bang-bang switches) while tracking a $55 \to 90 \to 58\,^\circ\mathrm{C}$ trace cleanly to Gear `3` and back to Gear `0`.
- **Bilinear Coulomb Friction-Hysteresis Loop Identity (*Dougong* bracket law):** Certifies exact Shoelace polygon dissipation area == analytical $4 f_y (u_m - u_y)(1 - \alpha)$ (`100/1` Coulomb, `0/1` elastic limit, `0/1` residual).
- **CLI Modes:** `--standalone` (default, print canonical JSON certificate), `--write <path>` (write canonical certificate), and `--check <path>` (fail-closed verify certificate against live pure-$\mathbb{Q}$ recomputation).

### 3. `bin/aletheion_exoskeleton_plugins.py` — Pure-$\mathbb{Z}/\mathbb{Q}$ Verification & Scheduling Plugins
- **`khipu_tree_checksum`:** Iterative bounded-depth & bounded-node (`max_depth = 64`, `max_nodes = 10,000`, cycle-rejecting) Inca Khipu additive conservation ($v_{\mathrm{parent}} = \sum v_{\mathrm{child}}$) + canonical Merkle SHA-256 checksum, exercised offline on four reproduced summation constants from published Open Khipu Repository (OKR) records (`khipu_id` `1000037`, `1000009`, `1000058`, `1000127`; Harvard Khipu Database Project) — verifying internal `parent == sum(children)` conservation and Merkle hashing — or ingesting external JSON files via `--khipu-cert <path>`.
- **`sona_eulerian_scheduler`:** Memory-bounded (`m * n <= 100,000`) Chokwe *Sona* diagonal torus sweep verifying exact `cycle_count == gcd(m, n)` across all `576` grids (`1..24 x 1..24`, `359` coprime monolinear cycles), running a synthetic `141`-slot (`repo_000..repo_140`) `(3 x 47, gcd = 1)` Eulerian cycle (configurable via `--repo-rows`, `--repo-cols`), and rejecting non-coprime grids fail-closed (`SONA_NON_COPRIME_FRAGMENTATION`).
- **`panini_c1p_registry`:** Exact verification of all `43/43` classical Pāṇinian *pratyāhāras* on the `14` *Śivasūtras* (`43` slots, `42` distinct phonemes, duplicate `"h"` at slots `9` and `42`), exact rational bit compression (`21/5` descriptor-only, `21/8` total with 43-slot table), exhaustive `6/6` permutation proof of the single-copy Tucker $C_3$ obstruction (`0/6` valid permutations, resolved by duplicating `"h"`), plus a generic C1P contiguous-interval registry encoder with explicit gap witnesses.

---

## CLI Reference & Verification

```bash
# Deterministic offline verification (3-mode isolated Python parity):
python3 -I -B tests/test_hyperspheric_exoskeleton_r5.py
python3 -I -O -B tests/test_hyperspheric_exoskeleton_r5.py
python3 -I -B -W error tests/test_hyperspheric_exoskeleton_r5.py

# Hyperspheric HUD CLI:
python3 -I -B bin/hyperspheric-hud.py --self-test --screenpad
python3 -I -B bin/hyperspheric-hud.py --self-test --json
python3 -I -B bin/hyperspheric-hud.py --once --json --web-index <web.json> \
  [--board <board.jsonl>] [--members <members.json>] [--poloidal-roles r1,r2] \
  [--contention-json <env.json>] [--rag-db <rag.sqlite>] [--rag-query <terms>] [--live-clock]

# Thermal Governor CLI:
python3 -I -B bin/aletheion_hysteresis_governor.py --standalone
python3 -I -B bin/aletheion_hysteresis_governor.py --write /tmp/gov_cert.json
python3 -I -B bin/aletheion_hysteresis_governor.py --check /tmp/gov_cert.json

# Exoskeleton Plugins CLI:
python3 -I -B bin/aletheion_exoskeleton_plugins.py --json [--khipu-cert <khipu.json>] [--repo-rows 3 --repo-cols 47]
```
