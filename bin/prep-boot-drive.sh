#!/usr/bin/env bash
set -euo pipefail

# Fail-closed dual-purpose boot + migration drive builder for Foundation-0/aletheia-shell.
# Flashes a verified hybrid-MBR Omarchy/Arch ISO to the front of a USB drive and appends
# a 1 MiB-aligned ext4 data partition (ALETHEIA_DATA) across the remaining space (< 2 TiB).

usage() {
  cat <<'EOF'
Usage: sudo bash bin/prep-boot-drive.sh --device /dev/sdX --iso /path/to/omarchy.iso [options]

Options:
  --device DEV       Target USB block device (e.g. /dev/sda). Must report TRAN=usb.
  --iso PATH         Path to downloaded hybrid-MBR Omarchy/Arch ISO.
  --label NAME       Filesystem label for the appended ext4 data partition (default: ALETHEIA_DATA).
  --mount DIR        Mountpoint for the new data partition (default: /mnt/aletheia-data).
  --owner USER       Owner user[:group] for the mounted data partition root (default: $SUDO_USER or $USER).
  --start-gib N      Start offset in GiB for partition 3 (default: 16; must exceed ISO size).
  --dry-run          Validate device/ISO safety checks and print exact plan without writing.
  -h, --help         Show this help text.
EOF
}

DEV=""
ISO=""
LABEL="ALETHEIA_DATA"
MOUNT_DIR="/mnt/aletheia-data"
OWNER="${SUDO_USER:-${USER:-root}}"
START_GIB=16
DRY_RUN=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) DEV="${2:?missing value for --device}"; shift 2 ;;
    --iso) ISO="${2:?missing value for --iso}"; shift 2 ;;
    --label) LABEL="${2:?missing value for --label}"; shift 2 ;;
    --mount) MOUNT_DIR="${2:?missing value for --mount}"; shift 2 ;;
    --owner) OWNER="${2:?missing value for --owner}"; shift 2 ;;
    --start-gib) START_GIB="${2:?missing value for --start-gib}"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$DEV" && -n "$ISO" ]] || { echo "ERROR: --device and --iso are required" >&2; usage >&2; exit 2; }
[[ -b "$DEV" ]] || { echo "ERROR: not a block device: $DEV" >&2; exit 2; }
[[ -f "$ISO" ]] || { echo "ERROR: ISO not found: $ISO" >&2; exit 2; }
[[ "$START_GIB" =~ ^[0-9]+$ && "$START_GIB" -ge 8 ]] || { echo "ERROR: invalid --start-gib: $START_GIB" >&2; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9_-]{1,16}$ ]] || { echo "ERROR: invalid ext4 label: $LABEL" >&2; exit 2; }

if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run with sudo (or pass --dry-run to inspect the plan)." >&2
  exit 1
fi

for tool in lsblk findmnt wipefs dd sfdisk partprobe mkfs.ext4 mount chown sha256sum; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing required tool: $tool" >&2; exit 2; }
done

# 1. Hard safety checks: MUST be whole-disk USB device with zero critical host mounts.
DEV_TYPE="$(lsblk -dn -o TYPE "$DEV" | tr -d '[:space:]')"
DEV_TRAN="$(lsblk -dn -o TRAN "$DEV" | tr -d '[:space:]')"
DEV_MODEL="$(lsblk -dn -o MODEL "$DEV" | sed 's/[[:space:]]*$//')"
DEV_BYTES="$(lsblk -dbn -o SIZE "$DEV" | tr -d '[:space:]')"
DEV_SECTORS=$(( DEV_BYTES / 512 ))
ISO_BYTES="$(stat -c '%s' "$ISO")"
START_SECTORS=$(( START_GIB * 1024 * 1024 * 2 ))

[[ "$DEV_TYPE" == "disk" ]] || { echo "ERROR: $DEV is TYPE=$DEV_TYPE (expected whole disk)" >&2; exit 2; }
[[ "$DEV_TRAN" == "usb" ]] || { echo "ERROR: refusing non-USB device $DEV (TRAN=$DEV_TRAN)" >&2; exit 2; }

while IFS= read -r mp; do
  case "$mp" in
    "" ) ;;
    /|/boot|/boot/efi|/home|"[SWAP]")
      echo "ERROR: refusing device $DEV with critical host mount: $mp" >&2
      exit 2
      ;;
  esac
done < <(lsblk -nr -o MOUNTPOINTS "$DEV")

if (( ISO_BYTES >= START_GIB * 1024 * 1024 * 1024 )); then
  echo "ERROR: ISO (${ISO_BYTES} B) exceeds --start-gib ${START_GIB} GiB" >&2
  exit 2
fi

# MBR 32-bit sector cap check (2^32 - 1 = 4294967295 sectors ~= 2.00 TiB).
if (( DEV_SECTORS > 4294967295 )); then
  echo "ERROR: disk has $DEV_SECTORS sectors (> MBR 2 TiB limit); cap partition 3 explicitly." >&2
  exit 2
fi

# Verify companion .sha256 if present.
if [[ -f "${ISO}.sha256" ]]; then
  echo "[1/6] Verifying ISO SHA-256 against ${ISO}.sha256..."
  ( cd "$(dirname "$ISO")" && sha256sum -c "$(basename "${ISO}.sha256")" )
else
  echo "[1/6] Computing ISO SHA-256 (no companion .sha256 file found)..."
  sha256sum "$ISO"
fi

echo "=== VERIFIED PLAN ==="
echo "  Device:       $DEV ($DEV_MODEL, TRAN=$DEV_TRAN, $(( DEV_BYTES / 1024 / 1024 / 1024 )) GiB, $DEV_SECTORS sectors)"
echo "  ISO:          $ISO ($(( ISO_BYTES / 1024 / 1024 )) MiB)"
echo "  Partition 3:  start=${START_GIB} GiB (sector ${START_SECTORS}) -> end of disk, ext4 label=${LABEL}"
echo "  Mountpoint:   ${MOUNT_DIR} (owner: ${OWNER})"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY_RUN_OK: safety checks passed; zero bytes written."
  exit 0
fi

# 2. Unmount any currently mounted partitions on DEV.
echo "[2/6] Unmounting existing partitions on $DEV..."
while IFS= read -r part; do
  if [[ -n "$part" && "$part" != "$DEV" ]]; then
    umount "$part" 2>/dev/null || true
  fi
done < <(lsblk -nr -o PATH "$DEV")

# 3. Wipe stale signatures and flash verified hybrid ISO to DEV.
echo "[3/6] Wiping old partition signatures on $DEV..."
wipefs -a "$DEV"

echo "[4/6] Flashing verified ISO to $DEV (dd bs=4M conv=fsync)..."
dd if="$ISO" of="$DEV" bs=4M status=progress conv=fsync
sync
partprobe "$DEV" || true
sleep 2

# 4. Append partition 3 (Linux ext4) at START_SECTORS without disturbing ISO partitions 1 & 2.
echo "[5/6] Appending partition 3 (${LABEL}, ext4) starting at ${START_GIB} GiB (sector ${START_SECTORS})..."
printf '%s,,83\n' "$START_SECTORS" | sfdisk --append --no-reread "$DEV"
partprobe "$DEV"
udevadm settle || sleep 2

PART3="${DEV}3"
if [[ "$DEV" == *"nvme"* || "$DEV" == *"mmcblk"* ]]; then
  PART3="${DEV}p3"
fi
[[ -b "$PART3" ]] || { echo "ERROR: expected partition device $PART3 not found" >&2; exit 1; }

echo "[6/6] Formatting $PART3 as ext4 (${LABEL}) and mounting at ${MOUNT_DIR}..."
mkfs.ext4 -F -L "$LABEL" "$PART3"
mkdir -p "$MOUNT_DIR"
mount "$PART3" "$MOUNT_DIR"
chown "${OWNER}:${OWNER}" "$MOUNT_DIR"
mkdir -p "${MOUNT_DIR}/aletheia-migration"
chown "${OWNER}:${OWNER}" "${MOUNT_DIR}/aletheia-migration"

echo "=== SUCCESS ==="
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV"
echo ""
echo "Ready! Next step (after /quit from agy):"
echo "  bash \"$(cd "$(dirname "$0")" && pwd)/freeze-for-migration.sh\" --dest \"${MOUNT_DIR}/aletheia-migration\" --slim"
