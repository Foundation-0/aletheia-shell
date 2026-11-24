#!/usr/bin/env bash
set -euo pipefail

# Restore a sealed migration bundle onto a target workstation and verify epistemic integrity fail-closed.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC=""
GEMINI_DIR="${HOME}/.gemini"
EXPEDITION_DIR="${HOME}/Projects/expedition"
CID=""
INCLUDE_AUTH=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: restore-and-verify.sh --src <bundle-dir> [options]

Options:
  --src <dir>              Source bundle directory containing payload/ and MIGRATION_SHA256SUMS
  --gemini-dir <dir>       Target Gemini home directory (default: $HOME/.gemini)
  --expedition-dir <dir>   Target expedition directory (default: $HOME/Projects/expedition)
  --cid <cid>              Coordinator conversation ID (auto-read from MIGRATION_RECEIPT.json if omitted)
  --include-auth           Overwrite target oauth_creds.json / installation_id from bundle
  --dry-run                Preview planned restore and verification without modifying disk
  --help                   Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --src)
      SRC="${2:?--src requires a value}"
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
    --include-auth)
      INCLUDE_AUTH=1
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
      printf '[restore-and-verify] ERROR: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

if [[ -z "${SRC}" ]]; then
  printf '[restore-and-verify] ERROR: --src is required\n' >&2
  usage >&2
  exit 2
fi

MANIFEST="${SRC}/MIGRATION_SHA256SUMS"
RECEIPT="${SRC}/MIGRATION_RECEIPT.json"
PAYLOAD="${SRC}/payload"

if [[ ! -f "${MANIFEST}" || ! -d "${PAYLOAD}" ]]; then
  printf '[restore-and-verify] ERROR: invalid bundle (missing MIGRATION_SHA256SUMS or payload/): %s\n' "${SRC}" >&2
  exit 2
fi

if [[ -z "${CID}" && -f "${RECEIPT}" ]]; then
  CID="$(python3 -I -B - "${RECEIPT}" <<'PY'
import json, sys
from pathlib import Path
obj = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
cid = obj.get("cid", "")
if isinstance(cid, str) and cid:
    print(cid)
PY
)"
fi

if [[ -z "${CID}" ]]; then
  printf '[restore-and-verify] ERROR: could not determine CID; pass --cid <CID>\n' >&2
  exit 2
fi

TARGET_AG="${GEMINI_DIR}/antigravity-cli"
TARGET_CFG="${GEMINI_DIR}/config"

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[restore-and-verify] DRY-RUN: src=%s cid=%s include_auth=%d\n' "${SRC}" "${CID}" "${INCLUDE_AUTH}"
  printf '[restore-and-verify] DRY-RUN: would restore to %s, %s, and %s\n' "${TARGET_AG}" "${TARGET_CFG}" "${EXPEDITION_DIR}"
  printf '[restore-and-verify] DRY-RUN: would strip stale locks in %s/presence\n' "${TARGET_AG}"
  printf '[restore-and-verify] DRY-RUN: would run verify_epistemic_integrity.py fail-closed\n'
  printf '[restore-and-verify] DRY-RUN: launch command -> agy --resume %s\n' "${CID}"
  exit 0
fi

mkdir -p "${TARGET_AG}" "${TARGET_CFG}" "${EXPEDITION_DIR}"

# Preserve existing target host OAuth / installation identity unless --include-auth is explicitly passed.
PRESERVE_AUTH_FLAGS=()
SKIP_AUTH_ARG=()
if [[ "${INCLUDE_AUTH}" -eq 0 ]]; then
  if [[ -f "${TARGET_AG}/installation_id" ]]; then
    PRESERVE_AUTH_FLAGS+=(--exclude='installation_id')
    SKIP_AUTH_ARG+=(--skip-auth-files)
    printf '[restore-and-verify] Preserving existing target %s/installation_id\n' "${TARGET_AG}"
  fi
  if [[ -f "${GEMINI_DIR}/oauth_creds.json" ]]; then
    PRESERVE_AUTH_FLAGS+=(--exclude='oauth_creds.json')
    printf '[restore-and-verify] Preserving existing target %s/oauth_creds.json\n' "${GEMINI_DIR}"
  fi
fi

rsync -a \
  --exclude='presence/*.lock' \
  "${PRESERVE_AUTH_FLAGS[@]}" \
  "${PAYLOAD}/gemini-antigravity-cli/" "${TARGET_AG}/"

if [[ -d "${PAYLOAD}/gemini-config" ]]; then
  rsync -a "${PAYLOAD}/gemini-config/" "${TARGET_CFG}/"
fi

rsync -a "${PAYLOAD}/expedition/" "${EXPEDITION_DIR}/"

# Strip any stale presence lock files cleanly without recursive force-delete.
PRESENCE_DIR="${TARGET_AG}/presence"
if [[ -d "${PRESENCE_DIR}" ]]; then
  find "${PRESENCE_DIR:?}" -maxdepth 1 -type f -name '*.lock' -delete
fi

# Run fail-closed epistemic integrity verifier against restored files.
python3 -I -B "${SCRIPT_DIR}/verify_epistemic_integrity.py" \
  --manifest "${MANIFEST}" \
  --gemini-dir "${TARGET_AG}" \
  --config-dir "${TARGET_CFG}" \
  --expedition-dir "${EXPEDITION_DIR}" \
  --cid "${CID}" \
  "${SKIP_AUTH_ARG[@]}" \
  --json

printf '\n[restore-and-verify] RESTORE VERIFIED PASS\n'
printf '[restore-and-verify] Next step: agy --resume %s\n' "${CID}"
