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
  --iso PATH         Path to downloaded hybrid-MBR Omarchy/Arch ISO. A companion
                     <iso>.sha256 is required unless --allow-unverified-iso is passed.
  --label NAME       Filesystem label for the appended ext4 data partition (default: ALETHEIA_DATA).
  --mount DIR        Mountpoint for the new data partition (default: /mnt/aletheia-data).
  --owner USER       Owner user[:group] for the mounted data partition root (default: $SUDO_USER or $USER).
  --start-gib N      Start offset in GiB for the appended partition (default: 16; must exceed ISO size).
  --yes              Skip the interactive erase confirmation (NOT recommended).
  --allow-unverified-iso
                     Flash even when no <iso>.sha256 companion exists (NOT recommended for boot media).
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
ASSUME_YES=0
ALLOW_UNVERIFIED=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --device) DEV="${2:?missing value for --device}"; shift 2 ;;
    --iso) ISO="${2:?missing value for --iso}"; shift 2 ;;
    --label) LABEL="${2:?missing value for --label}"; shift 2 ;;
    --mount) MOUNT_DIR="${2:?missing value for --mount}"; shift 2 ;;
    --owner) OWNER="${2:?missing value for --owner}"; shift 2 ;;
    --start-gib) START_GIB="${2:?missing value for --start-gib}"; shift 2 ;;
    --yes) ASSUME_YES=1; shift ;;
    --allow-unverified-iso) ALLOW_UNVERIFIED=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "ERROR: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
done

[[ -n "$DEV" && -n "$ISO" ]] || { echo "ERROR: --device and --iso are required" >&2; usage >&2; exit 2; }
[[ -b "$DEV" ]] || { echo "ERROR: not a block device: $DEV" >&2; exit 2; }
# Canonicalize (e.g. /dev/disk/by-id/... -> /dev/sdX) so partition node math works later.
DEV="$(readlink -f "$DEV")"
[[ -b "$DEV" ]] || { echo "ERROR: not a block device after resolving: $DEV" >&2; exit 2; }
[[ -f "$ISO" ]] || { echo "ERROR: ISO not found: $ISO" >&2; exit 2; }
[[ "$START_GIB" =~ ^[0-9]+$ && "$START_GIB" -ge 8 ]] || { echo "ERROR: invalid --start-gib: $START_GIB" >&2; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9_-]{1,16}$ ]] || { echo "ERROR: invalid ext4 label: $LABEL" >&2; exit 2; }
[[ "$OWNER" =~ ^[A-Za-z_][A-Za-z0-9_-]*(:[A-Za-z_][A-Za-z0-9_-]*)?$ ]] || { echo "ERROR: invalid --owner (expected USER or USER:GROUP): $OWNER" >&2; exit 2; }

if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run with sudo (or pass --dry-run to inspect the plan)." >&2
  exit 1
fi

for tool in lsblk findmnt wipefs dd sfdisk partprobe mkfs.ext4 mount chown sha256sum blockdev od readlink; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing required tool: $tool" >&2; exit 2; }
done

# Remediation hint if anything fails after the destructive steps have started.
on_error() {
  echo "ERROR: prep failed (rc=$1). $DEV may be partially written (ISO flashed; data partition possibly missing). Inspect with: lsblk $DEV ; re-run with --dry-run, or re-run the script to retry." >&2
}
trap 'on_error $?' ERR

# 1. Hard safety checks: MUST be whole-disk USB device with zero critical host mounts.
DEV_TYPE="$(lsblk -dn -o TYPE "$DEV" | tr -d '[:space:]')"
DEV_TRAN="$(lsblk -dn -o TRAN "$DEV" | tr -d '[:space:]')"
DEV_MODEL="$(lsblk -dn -o MODEL "$DEV" | sed 's/[[:space:]]*$//')"
DEV_BYTES="$(lsblk -dbn -o SIZE "$DEV" | tr -d '[:space:]')"
SECTOR_SIZE="$(blockdev --getss "$DEV")"
DEV_SECTORS=$(( DEV_BYTES / SECTOR_SIZE ))
ISO_BYTES="$(stat -c '%s' "$ISO")"
START_SECTORS=$(( START_GIB * 1024 * 1024 * 1024 / SECTOR_SIZE ))

[[ "$DEV_TYPE" == "disk" ]] || { echo "ERROR: $DEV is TYPE=$DEV_TYPE (expected whole disk)" >&2; exit 2; }
[[ "$DEV_TRAN" == "usb" ]] || { echo "ERROR: refusing non-USB device $DEV (TRAN=$DEV_TRAN)" >&2; exit 2; }

# 1a. Critical host mounts on DEV itself (mountpoint-based).
while IFS= read -r mp; do
  case "$mp" in
    "" ) ;;
    /|/boot|/boot/efi|/home|"[SWAP]")
      echo "ERROR: refusing device $DEV with critical host mount: $mp" >&2
      exit 2
      ;;
  esac
done < <(lsblk -nr -o MOUNTPOINTS "$DEV")

# 1b. Live-system mounts backed by DEV (source-based). Catches booting from the target
#     live USB: an Arch/Omarchy live environment mounts its ISO under /run/archiso/*,
#     which the mountpoint list above does not flag.
while IFS= read -r line; do
  src="${line%% *}"
  tgt="${line#* }"
  case "$src" in
    "$DEV"|"$DEV"[0-9]*|"$DEV"p[0-9]*) ;;
    *) continue ;;
  esac
  case "$tgt" in
    /|/boot|/boot/efi|/home|"[SWAP]"|/run/archiso/*|/run/live/*|/sysroot|/sysroot/*)
      echo "ERROR: refusing $DEV: live-system mount $tgt is backed by $src" >&2
      exit 2
      ;;
  esac
done < <(findmnt -rn -o SOURCE,TARGET 2>/dev/null || true)

# 1c. The ISO must not live on the target device: we unmount and wipe DEV below,
#     which would destroy the dd source mid-flight.
ISO_SOURCE="$(findmnt -n -o SOURCE -T "$ISO" 2>/dev/null || true)"
case "$ISO_SOURCE" in
  "$DEV"|"$DEV"[0-9]*|"$DEV"p[0-9]*)
    echo "ERROR: ISO ($ISO) is stored on target device $DEV (mounted via $ISO_SOURCE); move it elsewhere first" >&2
    exit 2
    ;;
esac

# 1d. ISO must be a hybrid-MBR image (boot signature 0x55AA at offset 510).
ISO_SIG="$(od -An -tx1 -j 510 -N 2 "$ISO" | tr -d '[:space:]')"
[[ "$ISO_SIG" == "55aa" ]] || { echo "ERROR: $ISO has no MBR boot signature (0x55AA); not a hybrid ISO" >&2; exit 2; }

if (( ISO_BYTES >= START_GIB * 1024 * 1024 * 1024 )); then
  echo "ERROR: ISO (${ISO_BYTES} B) exceeds --start-gib ${START_GIB} GiB" >&2
  exit 2
fi

# MBR 32-bit sector cap check (2^32 - 1 = 4294967295 sectors ~= 2.00 TiB at 512 B).
if (( DEV_SECTORS > 4294967295 )); then
  echo "ERROR: disk has $DEV_SECTORS sectors (> MBR 2 TiB limit); cap the partition explicitly." >&2
  exit 2
fi

# The appended partition needs room to exist: start + >=1 GiB must fit on the disk.
MIN_PART_SECTORS=$(( 1024 * 1024 * 1024 / SECTOR_SIZE ))
if (( START_SECTORS + MIN_PART_SECTORS > DEV_SECTORS )); then
  echo "ERROR: --start-gib ${START_GIB} leaves no room for the data partition on a $(( DEV_BYTES / 1024 / 1024 / 1024 )) GiB disk" >&2
  exit 2
fi

# Verify companion .sha256 if present; fail closed otherwise (boot media).
if [[ -f "${ISO}.sha256" ]]; then
  echo "[1/6] Verifying ISO SHA-256 against ${ISO}.sha256..."
  ( cd "$(dirname "$ISO")" && sha256sum -c "$(basename "${ISO}.sha256")" ) || {
    echo "ERROR: ISO SHA-256 verification FAILED for $ISO" >&2; exit 1;
  }
elif [[ "$ALLOW_UNVERIFIED" -eq 1 ]]; then
  echo "[1/6] WARNING: no ${ISO}.sha256 found; flashing UNVERIFIED ISO (--allow-unverified-iso). Hash for manual check:" >&2
  sha256sum "$ISO" >&2
else
  echo "ERROR: no ${ISO}.sha256 companion found; refusing to flash an unverified ISO." >&2
  echo "       Download the published checksum and place it next to the ISO, or re-run with --allow-unverified-iso." >&2
  exit 2
fi

echo "=== VERIFIED PLAN ==="
echo "  Device:       $DEV ($DEV_MODEL, TRAN=$DEV_TRAN, $(( DEV_BYTES / 1024 / 1024 / 1024 )) GiB, $DEV_SECTORS x ${SECTOR_SIZE}B sectors)"
echo "  ISO:          $ISO ($(( ISO_BYTES / 1024 / 1024 )) MiB)"
echo "  Partition:    appended ext4 (label=${LABEL}) starting at ${START_GIB} GiB (sector ${START_SECTORS}) -> end of disk"
echo "  Mountpoint:   ${MOUNT_DIR} (owner: ${OWNER})"

if [[ "$DRY_RUN" -eq 1 ]]; then
  echo "DRY_RUN_OK: safety checks passed; zero bytes written."
  exit 0
fi

# Interactive erase confirmation: type the device basename to proceed.
if [[ "$ASSUME_YES" -eq 0 ]]; then
  printf 'About to ERASE %s (%s) and flash %s onto it. Type "%s" to confirm: ' "$DEV" "$DEV_MODEL" "$ISO" "$(basename "$DEV")"
  read -r CONFIRM
  [[ "$CONFIRM" == "$(basename "$DEV")" ]] || { echo "Aborted: confirmation did not match." >&2; exit 1; }
fi

unmount_device_tree() {
  local target_dev="$1"
  local path mp
  # Unmount every active mountpoint on the whole disk (hybrid ISO udisks2 auto-mount) AND all partitions.
  while IFS= read -r path; do
    [[ -n "$path" ]] || continue
    while IFS= read -r mp; do
      [[ -n "$mp" ]] || continue
      umount "$mp" 2>/dev/null || umount -l "$mp" 2>/dev/null || true
    done < <(findmnt -rn -o TARGET --source "$path" 2>/dev/null || true)
    umount "$path" 2>/dev/null || true
  done < <(lsblk -nr -o PATH "$target_dev" 2>/dev/null || printf '%s\n' "$target_dev")
}

# 2. Unmount any currently mounted whole-disk or partition targets on DEV.
echo "[2/6] Unmounting existing mounts on $DEV..."
unmount_device_tree "$DEV"

# 3. Wipe stale signatures and flash verified hybrid ISO to DEV.
echo "[3/6] Wiping old partition signatures on $DEV..."
wipefs -a "$DEV"

echo "[4/6] Flashing verified ISO to $DEV (dd bs=4M conv=fsync)..."
dd if="$ISO" of="$DEV" bs=4M status=progress conv=fsync
sync
partprobe "$DEV" || true
udevadm settle || sleep 2
# Desktop auto-mounters (udisks2) immediately mount the hybrid ISO at sector 0 on $DEV;
# release that whole-disk mount so sfdisk/partprobe/mkfs.ext4 do not hit EBUSY.
unmount_device_tree "$DEV"

# 4. Append a Linux (ext4, type 83) partition at START_SECTORS without disturbing the ISO partitions.
echo "[5/6] Appending data partition (${LABEL}, ext4) starting at ${START_GIB} GiB (sector ${START_SECTORS})..."
printf '%s,,83\n' "$START_SECTORS" | sfdisk --append --no-reread "$DEV"
partprobe "$DEV" || true
udevadm settle || sleep 2
unmount_device_tree "$DEV"

# Locate the appended partition dynamically (do not assume it is partition 3).
PART="$(lsblk -nr -o TYPE,PATH "$DEV" 2>/dev/null | awk '$1 == "part" { print $2 }' | tail -n 1)"
[[ -n "$PART" && -b "$PART" ]] || { echo "ERROR: could not locate the appended partition on $DEV" >&2; exit 1; }

echo "[6/6] Formatting $PART as ext4 (${LABEL}) and mounting at ${MOUNT_DIR}..."
mkfs.ext4 -F -L "$LABEL" "$PART"
mkdir -p "$MOUNT_DIR"
mount "$PART" "$MOUNT_DIR"
chown "$OWNER" "$MOUNT_DIR"
mkdir -p "${MOUNT_DIR}/aletheia-migration"
chown "$OWNER" "${MOUNT_DIR}/aletheia-migration"

echo "=== SUCCESS ==="
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV"
echo ""
echo "Ready! Next step (after /quit from agy):"
echo "  bash \"$(cd "$(dirname "$0")" && pwd)/freeze-for-migration.sh\" --dest \"${MOUNT_DIR}/aletheia-migration\" --slim"
