#!/usr/bin/env bash
set -euo pipefail

# Quiesce, WAL-checkpoint, stage, and SHA-256 seal an Antigravity + expedition migration bundle.

DEST=""
GEMINI_DIR="${HOME}/.gemini"
EXPEDITION_DIR="${HOME}/Projects/expedition"
CID=""
MODE="full"
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: freeze-for-migration.sh --dest <dir> [options]

Options:
  --dest <dir>             Destination staging directory (e.g. /run/media/$USER/ALETHEIA_DATA/bundle)
  --gemini-dir <dir>       Gemini home directory (default: $HOME/.gemini)
  --expedition-dir <dir>   Expedition workspace directory (default: $HOME/Projects/expedition)
  --cid <cid>              Coordinator conversation ID (auto-detected if omitted)
  --slim                   Skip historical symbiosis/round3 and symbiosis/round4 caches
  --full                   Copy full expedition directory (default)
  --dry-run                Preview checks and planned copies without modifying disk
  --help                   Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dest)
      DEST="${2:?--dest requires a value}"
      shift 2
      ;;
    --gemini-dir)
      GEMINI_DIR="${2:?--gemini-dir requires a value}"
      shift 2
      ;;
    --expedition-dir)
      EXPEDITION_DIR="${2:?--expedition-dir requires a value}"
      shift 2
      ;;
    --cid)
      CID="${2:?--cid requires a value}"
      shift 2
      ;;
    --slim)
      MODE="slim"
      shift
      ;;
    --full)
      MODE="full"
      shift
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf '[freeze-for-migration] ERROR: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${DEST}" ]]; then
  printf '[freeze-for-migration] ERROR: --dest is required\n' >&2
  usage >&2
  exit 2
fi

AG_DIR="${GEMINI_DIR}/antigravity-cli"
CFG_DIR="${GEMINI_DIR}/config"

if [[ ! -d "${AG_DIR}" ]]; then
  printf '[freeze-for-migration] ERROR: antigravity-cli directory not found: %s\n' "${AG_DIR}" >&2
  exit 2
fi
if [[ ! -d "${EXPEDITION_DIR}" ]]; then
  printf '[freeze-for-migration] ERROR: expedition directory not found: %s\n' "${EXPEDITION_DIR}" >&2
  exit 2
fi

# Fail closed if agy / antigravity is still running (unless --dry-run).
if [[ "${DRY_RUN}" -eq 0 ]]; then
  if pgrep -f '[a]ntigravity' >/dev/null 2>&1 || pgrep -x agy >/dev/null 2>&1; then
    printf '[freeze-for-migration] ERROR: antigravity/agy process is still running; stop it before freezing.\n' >&2
    exit 2
  fi
else
  printf '[freeze-for-migration] DRY-RUN: skipping live process block check\n'
fi

# Auto-detect CID from brain/*/.system_generated/swarm if omitted.
if [[ -z "${CID}" ]]; then
  CID="$(python3 -I -B - "${AG_DIR}" <<'PY'
import sys
from pathlib import Path
ag = Path(sys.argv[1])
found = sorted((ag / "brain").glob("*/.system_generated/swarm/board.jsonl"))
if len(found) == 1:
    print(found[0].parents[2].name)
elif len(found) > 1:
    print(found[-1].parents[2].name)
PY
)"
fi

if [[ -z "${CID}" ]]; then
  printf '[freeze-for-migration] ERROR: could not detect swarm CID; pass --cid <CID>\n' >&2
  exit 2
fi

# Checkpoint SQLite WAL and verify PRAGMA integrity_check on every DB (Python stdlib sqlite3).
if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[freeze-for-migration] DRY-RUN: would checkpoint & integrity-check SQLite DBs in %s\n' "${AG_DIR}"
  printf '[freeze-for-migration] DRY-RUN: mode=%s cid=%s\n' "${MODE}" "${CID}"
  printf '[freeze-for-migration] DRY-RUN: would rsync %s -> %s/payload/gemini-antigravity-cli\n' "${AG_DIR}" "${DEST}"
  printf '[freeze-for-migration] DRY-RUN: would rsync %s -> %s/payload/gemini-config\n' "${CFG_DIR}" "${DEST}"
  printf '[freeze-for-migration] DRY-RUN: would rsync %s -> %s/payload/expedition\n' "${EXPEDITION_DIR}" "${DEST}"
  printf '[freeze-for-migration] DRY-RUN: would emit %s/MIGRATION_SHA256SUMS and %s/MIGRATION_RECEIPT.json\n' "${DEST}" "${DEST}"
  exit 0
fi

python3 -I -B - "${AG_DIR}" <<'PY'
import sqlite3, sys
from pathlib import Path

ag = Path(sys.argv[1])
dbs = sorted((ag / "conversations").glob("*.db"))
summ = ag / "conversation_summaries.db"
if summ.is_file():
    dbs.append(summ)
if not dbs:
    raise SystemExit("ERROR: no SQLite databases found to checkpoint")
for dbp in dbs:
    conn = sqlite3.connect(str(dbp))
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE);").fetchall()
        rows = conn.execute("PRAGMA integrity_check;").fetchall()
        if rows != [("ok",)]:
            raise SystemExit(f"ERROR: SQLite integrity_check failed on {dbp.name}: {rows!r}")
    finally:
        conn.close()
print(f"[freeze-for-migration] SQLite WAL checkpoint + integrity_check OK ({len(dbs)} DBs)")
PY

mkdir -p "${DEST}/payload/gemini-antigravity-cli" "${DEST}/payload/gemini-config" "${DEST}/payload/expedition"
chmod 700 "${DEST}"

rsync -a \
  --exclude='presence/*.lock' \
  --exclude='crashes/' \
  --exclude='log/' \
  --exclude='__pycache__/' \
  --exclude='*.pyc' \
  "${AG_DIR}/" "${DEST}/payload/gemini-antigravity-cli/"

if [[ -d "${CFG_DIR}" ]]; then
  rsync -a \
    --exclude='__pycache__/' \
    --exclude='*.pyc' \
    "${CFG_DIR}/" "${DEST}/payload/gemini-config/"
fi

EXP_EXCLUDES=(
  --exclude='__pycache__/'
  --exclude='*.pyc'
  --exclude='.cache/aletheia-snaps/'
)
if [[ "${MODE}" == "slim" ]]; then
  EXP_EXCLUDES+=(
    --exclude='symbiosis/round3/'
    --exclude='symbiosis/round4/'
  )
fi

rsync -a "${EXP_EXCLUDES[@]}" "${EXPEDITION_DIR}/" "${DEST}/payload/expedition/"

# Emit deterministic MIGRATION_SHA256SUMS and MIGRATION_RECEIPT.json.
python3 -I -B - "${DEST}" "${CID}" "${MODE}" <<'PY'
import hashlib, json, os, sys
from datetime import datetime, timezone
from pathlib import Path

dest = Path(sys.argv[1])
cid = sys.argv[2]
mode = sys.argv[3]
payload = dest / "payload"

lines = []
total_bytes = 0
file_count = 0
for dirpath, dirnames, filenames in os.walk(payload, followlinks=False):
    dirnames.sort()
    filenames.sort()
    for fname in filenames:
        fp = Path(dirpath) / fname
        if fp.is_symlink() or not fp.is_file():
            continue
        h = hashlib.sha256()
        with fp.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                h.update(chunk)
                total_bytes += len(chunk)
        rel = fp.relative_to(payload).as_posix()
        lines.append(f"{h.hexdigest()}  {rel}")
        file_count += 1

manifest_text = "\n".join(lines) + ("\n" if lines else "")
manifest_path = dest / "MIGRATION_SHA256SUMS"
manifest_path.write_text(manifest_text, encoding="utf-8")
manifest_sha = hashlib.sha256(manifest_text.encode("utf-8")).hexdigest()

receipt = {
    "schema": "aletheia.migration_receipt.v1",
    "created_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    "cid": cid,
    "mode": mode,
    "file_count": file_count,
    "total_bytes": total_bytes,
    "manifest_sha256": manifest_sha,
}
(dest / "MIGRATION_RECEIPT.json").write_text(json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8")
print(f"[freeze-for-migration] SEALED files={file_count} bytes={total_bytes} manifest_sha256={manifest_sha}")
PY
