#!/usr/bin/env bash
set -euo pipefail

# Idempotent post-Omarchy workstation bootstrap for ASUS Zenbook Pro Duo (RTX 3080 + 32 GiB RAM).

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ML_OS_DIR="${HOME}/Projects/ml-os"
HYPR_CONF_DIR="${HOME}/.config/hypr"
SYSTEMD_USER_DIR="${HOME}/.config/systemd/user"
DRY_RUN=0
SKIP_PACKAGES=0
SKIP_SYSTEMD=0

ARCH_PACKAGES=(
  nvidia-open-dkms
  cuda
  cudnn
  asusctl
  btop
  nvtop
  bubblewrap
  rsync
  zstd
  sqlite
  git
  python
)

usage() {
  cat <<'EOF'
Usage: bootstrap-zenbook.sh [options]

Options:
  --ml-os-dir <dir>   Target clone directory for Moissanite-Labs/ml-os (default: $HOME/Projects/ml-os)
  --repo-dir <dir>    Path to aletheia-shell checkout (default: auto-detected)
  --skip-packages     Skip pacman package installation step
  --skip-systemd      Skip user-level systemd timer installation/activation
  --dry-run           Print planned setup actions without modifying system state
  --help              Show this help message
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --ml-os-dir)
      ML_OS_DIR="${2:?--ml-os-dir requires a value}"
      shift 2
      ;;
    --repo-dir)
      REPO_DIR="${2:?--repo-dir requires a value}"
      shift 2
      ;;
    --skip-packages)
      SKIP_PACKAGES=1
      shift
      ;;
    --skip-systemd)
      SKIP_SYSTEMD=1
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
      printf '[bootstrap-zenbook] ERROR: unknown argument: %s\n' "$1" >&2
      exit 2
      ;;
  esac
done

run_cmd() {
  if [[ "${DRY_RUN}" -eq 1 ]]; then
    printf '[bootstrap-zenbook] DRY-RUN:'
    printf ' %q' "$@"
    printf '\n'
  else
    "$@"
  fi
}

printf '[bootstrap-zenbook] repo_dir=%s ml_os_dir=%s dry_run=%d\n' "${REPO_DIR}" "${ML_OS_DIR}" "${DRY_RUN}"

# 1. Arch Linux / Omarchy packages
if [[ "${SKIP_PACKAGES}" -eq 0 ]]; then
  if command -v pacman >/dev/null 2>&1; then
    run_cmd sudo pacman -S --needed --noconfirm "${ARCH_PACKAGES[@]}"
  else
    printf '[bootstrap-zenbook] INFO: pacman not found; verify packages manually: %s\n' "${ARCH_PACKAGES[*]}"
  fi
fi

# 2. Check mise-managed tools (agy, gh, opencode, node, uv), vibe, and elan / Lean 4
for tool in mise agy gh opencode node uv vibe elan lake lean; do
  if command -v "${tool}" >/dev/null 2>&1; then
    printf '[bootstrap-zenbook] OK: found %s (%s)\n' "${tool}" "$(command -v "${tool}")"
  else
    printf '[bootstrap-zenbook] MISSING: %s (install via pacman/mise/elan package manager)\n' "${tool}"
  fi
done

if command -v mise >/dev/null 2>&1; then
  printf '[bootstrap-zenbook] INFO: verify mise tool versions with: mise ls\n'
fi

# 3. Clone or update Moissanite-Labs/ml-os and run read-only hardware probe
if [[ ! -d "${ML_OS_DIR}/.git" ]]; then
  run_cmd mkdir -p "$(dirname "${ML_OS_DIR}")"
  run_cmd git clone https://github.com/Moissanite-Labs/ml-os.git "${ML_OS_DIR}"
else
  run_cmd git -C "${ML_OS_DIR}" fetch --quiet origin
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  printf '[bootstrap-zenbook] DRY-RUN: python3 -I -B %q probe --json\n' "${ML_OS_DIR}/bin/mlos"
elif [[ -f "${ML_OS_DIR}/bin/mlos" ]]; then
  python3 -I -B "${ML_OS_DIR}/bin/mlos" probe --json
fi

# 4. Install Hyprland Zenbook Pro Duo dual-display config (non-destructive)
HYPR_SRC="${REPO_DIR}/configs/hypr-zenbook-duo.conf"
HYPR_DST="${HYPR_CONF_DIR}/aletheia-zenbook-duo.conf"
if [[ -f "${HYPR_SRC}" ]]; then
  run_cmd mkdir -p "${HYPR_CONF_DIR}"
  run_cmd install -m 0644 "${HYPR_SRC}" "${HYPR_DST}"
  printf '[bootstrap-zenbook] INFO: add to %s/hyprland.conf -> source = %s\n' "${HYPR_CONF_DIR}" "${HYPR_DST}"
fi

# 5. Install user-level systemd continual-toroid watchdog service & 30-min timer
if [[ "${SKIP_SYSTEMD}" -eq 0 ]]; then
  SVC_SRC="${REPO_DIR}/systemd/aletheia-toroid-watchdog.service"
  TMR_SRC="${REPO_DIR}/systemd/aletheia-toroid-watchdog.timer"
  if [[ -f "${SVC_SRC}" && -f "${TMR_SRC}" ]]; then
    run_cmd mkdir -p "${SYSTEMD_USER_DIR}"
    run_cmd install -m 0644 "${SVC_SRC}" "${SYSTEMD_USER_DIR}/aletheia-toroid-watchdog.service"
    run_cmd install -m 0644 "${TMR_SRC}" "${SYSTEMD_USER_DIR}/aletheia-toroid-watchdog.timer"
    if command -v systemctl >/dev/null 2>&1; then
      run_cmd systemctl --user daemon-reload
      run_cmd systemctl --user enable --now aletheia-toroid-watchdog.timer
    fi
  fi
fi

printf '[bootstrap-zenbook] COMPLETE\n'
