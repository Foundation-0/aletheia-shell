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
                     Must be an absolute path outside system directories (/, /boot, /home,
                     /usr, /etc, /var, /run, /sysroot, /dev, /proc, /sys).
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
DEV="$(readlink -f -- "$DEV")"
[[ -b "$DEV" ]] || { echo "ERROR: not a block device after resolving: $DEV" >&2; exit 2; }
[[ -f "$ISO" ]] || { echo "ERROR: ISO not found: $ISO" >&2; exit 2; }
# Make the ISO path absolute (without resolving symlinks, so the .sha256 sidecar is still
# looked up next to the path the user gave): no later argument can then begin with '-'.
[[ "$ISO" == /* ]] || ISO="$PWD/$ISO"
[[ "$START_GIB" =~ ^[0-9]+$ && "$START_GIB" -ge 8 ]] || { echo "ERROR: invalid --start-gib: $START_GIB" >&2; exit 2; }
[[ "$LABEL" =~ ^[A-Za-z0-9_-]{1,16}$ ]] || { echo "ERROR: invalid ext4 label: $LABEL" >&2; exit 2; }
[[ "$OWNER" =~ ^[A-Za-z_][A-Za-z0-9_-]*(:[A-Za-z_][A-Za-z0-9_-]*)?$ ]] || { echo "ERROR: invalid --owner (expected USER or USER:GROUP): $OWNER" >&2; exit 2; }
# The data partition gets mounted at MOUNT_DIR: refuse system directories up front.
[[ "$MOUNT_DIR" == /* ]] || { echo "ERROR: --mount must be an absolute path: $MOUNT_DIR" >&2; exit 2; }
case "$MOUNT_DIR" in
  /|/boot|/boot/*|/efi|/efi/*|/home|/home/*|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/run|/run/*|/sysroot|/sysroot/*|/dev|/dev/*|/proc|/proc/*|/sys|/sys/*)
    echo "ERROR: refusing dangerous --mount path: $MOUNT_DIR" >&2
    exit 2
    ;;
esac

if [[ "$DRY_RUN" -eq 0 && "$(id -u)" -ne 0 ]]; then
  echo "ERROR: run with sudo (or pass --dry-run to inspect the plan)." >&2
  exit 1
fi

# --dry-run only reads (lsblk/findmnt/stat/od/readlink/sha256sum); the five destructive tools are
# required only for a real run, so a dry-run can finish its safety checks on a host without them.
for tool in lsblk findmnt mount umount chown sha256sum stat od readlink; do
  command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing required tool: $tool" >&2; exit 2; }
done
if [[ "$DRY_RUN" -eq 0 ]]; then
  for tool in wipefs dd sfdisk partprobe mkfs.ext4; do
    command -v "$tool" >/dev/null 2>&1 || { echo "ERROR: missing required tool: $tool" >&2; exit 2; }
  done
fi

# Remediation hint if anything fails after the destructive steps have started.
# The ERR trap is armed only right before the first write (see step [2/6] below), so that
# read-only --dry-run failures never print a misleading "partially written" message.
on_error() {
  echo "ERROR: prep failed (rc=$1). $DEV may be partially written (ISO flashed; data partition possibly missing). Inspect with: lsblk $DEV ; re-run with --dry-run, or re-run the script to retry." >&2
}

# 1. Hard safety checks: MUST be whole-disk USB device with zero critical host mounts.
# All queries below are world-readable (lsblk/stat/od), so non-root --dry-run works.
DEV_TYPE="$(lsblk -dn -o TYPE "$DEV" | tr -d '[:space:]')"
DEV_TRAN="$(lsblk -dn -o TRAN "$DEV" | tr -d '[:space:]')"
DEV_MODEL="$(lsblk -dn -o MODEL "$DEV" | sed 's/[[:space:]]*$//')"
DEV_BYTES="$(lsblk -dbn -o SIZE "$DEV" | tr -d '[:space:]')"
SECTOR_SIZE="$(lsblk -dn -o LOG-SEC "$DEV" | tr -d '[:space:]')"
[[ "$SECTOR_SIZE" =~ ^[0-9]+$ && "$SECTOR_SIZE" -ge 512 ]] || { echo "ERROR: implausible logical sector size for $DEV (LOG-SEC='$SECTOR_SIZE')" >&2; exit 2; }
[[ "$DEV_BYTES" =~ ^[0-9]+$ && "$DEV_BYTES" -gt 0 ]] || { echo "ERROR: implausible device size for $DEV (SIZE='$DEV_BYTES')" >&2; exit 2; }
DEV_SECTORS=$(( DEV_BYTES / SECTOR_SIZE ))
ISO_BYTES="$(stat -c '%s' -- "$ISO")"
START_SECTORS=$(( START_GIB * 1024 * 1024 * 1024 / SECTOR_SIZE ))

[[ "$DEV_TYPE" == "disk" ]] || { echo "ERROR: $DEV is TYPE=$DEV_TYPE (expected whole disk)" >&2; exit 2; }
[[ "$DEV_TRAN" == "usb" ]] || { echo "ERROR: refusing non-USB device $DEV (TRAN=$DEV_TRAN)" >&2; exit 2; }

# Canonical holder set of DEV: the disk itself plus every child node (partitions, crypt,
# lvm/dm), so the guards below cannot be bypassed by /dev/mapper/* or /dev/dm-* sources.
mapfile -t DEV_HOLDERS_RAW < <(lsblk -nr -o PATH "$DEV" 2>/dev/null || true)
[[ ${#DEV_HOLDERS_RAW[@]} -gt 0 ]] || { echo "ERROR: could not enumerate holders of $DEV via lsblk" >&2; exit 2; }
DEV_HOLDERS=()
for h in "${DEV_HOLDERS_RAW[@]}"; do
  [[ -n "$h" ]] || continue
  DEV_HOLDERS+=("$(readlink -f -- "$h" 2>/dev/null || printf '%s' "$h")")
done

# True if $1 (a findmnt SOURCE, possibly /dev/mapper/*, /dev/dm-* or Btrfs /dev/sdX[/subvol])
# canonicalizes to DEV or one of its holders.
is_dev_holder() {
  local src="$1" c h
  src="${src%%\[*}"   # strip Btrfs subvolume suffix, e.g. /dev/sda[/@]
  c="$(readlink -f -- "$src" 2>/dev/null || printf '%s' "$src")"
  for h in "${DEV_HOLDERS[@]}"; do
    [[ "$c" == "$h" ]] && return 0
  done
  return 1
}

# 1a. Critical host mounts anywhere on DEV or its children (mountpoint-based; lsblk
#     traverses crypt/lvm children and MOUNTPOINTS lists every mount of each device).
while IFS= read -r line; do
  # `lsblk -r` encodes a device's several mountpoints as a literal "\x0a" inside one cell;
  # split on it (and on commas) so every mountpoint is checked, not just the first.
  line="${line//\\x0a/ }"
  line="${line//,/ }"
  read -r -a mps <<< "$line"
  for mp in "${mps[@]}"; do
    case "$mp" in
      "" ) ;;
      /|/boot|/boot/*|/efi|/home|/home/*|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/run/archiso|/run/archiso/*|/run/live|/run/live/*|/sysroot|/sysroot/*|"[SWAP]")
        echo "ERROR: refusing device $DEV with critical host mount: $mp" >&2
        exit 2
        ;;
    esac
  done
done < <(lsblk -nr -o MOUNTPOINTS "$DEV")

# 1b. Live-system mounts backed by DEV or any of its holders (source-based). Catches
#     booting from the target live USB: an Arch/Omarchy live environment mounts its
#     medium under /run/archiso/*, which the mountpoint list above may not flag.
# A findmnt failure means we cannot prove DEV is not a live disk: refuse instead of skipping 1b.
if ! FINDMNT_ALL="$(findmnt -rn -o SOURCE,TARGET 2>/dev/null)"; then
  echo "ERROR: findmnt failed to list mounts; cannot prove $DEV is not backing a live system; refusing fail-closed" >&2
  exit 2
fi
while IFS= read -r line; do
  src="${line%% *}"
  tgt="${line#* }"
  is_dev_holder "$src" || continue
  case "$tgt" in
    /|/boot|/boot/*|/home|/home/*|/usr|/usr/*|/etc|/etc/*|/var|/var/*|/run/archiso|/run/archiso/*|/run/live|/run/live/*|/sysroot|/sysroot/*|"[SWAP]")
      echo "ERROR: refusing $DEV: live-system mount $tgt is backed by $src" >&2
      exit 2
      ;;
  esac
done <<< "$FINDMNT_ALL"

# 1c. The ISO must not live on the target device: we unmount and wipe DEV below,
#     which would destroy the dd source mid-flight. Fail closed if the backing
#     device of the ISO cannot be determined.
# --target=PATH keeps the path inside the option's own argv word. (`findmnt -T -- PATH` would
# make "--" the -T argument and then reject PATH as a stray element.)
ISO_SOURCE="$(findmnt -n -o SOURCE --target="$ISO" 2>/dev/null || true)"
if [[ -z "$ISO_SOURCE" ]]; then
  echo "ERROR: could not determine which device backs $ISO (findmnt --target failed); refusing fail-closed" >&2
  exit 2
fi
if is_dev_holder "$ISO_SOURCE"; then
  echo "ERROR: ISO ($ISO) is stored on target device $DEV (mounted via $ISO_SOURCE); move it elsewhere first" >&2
  exit 2
fi

# 1d. ISO must be a hybrid-MBR image (boot signature 0x55AA at offset 510).
ISO_SIG="$(od -An -tx1 -j 510 -N 2 -- "$ISO" | tr -d '[:space:]')"
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

# Verify the companion .sha256, with the digest bound to the exact ISO basename.
# A sidecar that is missing, names other files, is malformed, or lists the ISO more
# than once is rejected; a missing sidecar fails closed unless explicitly opted out.
if [[ -f "${ISO}.sha256" ]]; then
  echo "[1/6] Verifying ISO SHA-256 against ${ISO}.sha256..."
  ISO_BASE="$(basename -- "$ISO")"
  expected=""
  matches=0
  bad_entries=0
  while IFS= read -r line || [[ -n "$line" ]]; do
    [[ -z "$line" ]] && continue
    h="${line:0:64}"
    rest="${line:64}"
    sep1="${rest:0:1}"
    sep2="${rest:1:1}"
    fname="${rest:2}"
    # sha256sum line format: <64-hex> + ' ' + (' ' text mode | '*' binary mode) + filename
    if [[ "$h" =~ ^[0-9a-fA-F]{64}$ && "$sep1" == " " && ( "$sep2" == " " || "$sep2" == "*" ) ]]; then
      if [[ "$fname" == "$ISO_BASE" ]]; then
        matches=$(( matches + 1 ))
        expected="$h"
      else
        bad_entries=$(( bad_entries + 1 ))
      fi
    else
      bad_entries=$(( bad_entries + 1 ))
    fi
  done < "${ISO}.sha256"
  if (( bad_entries > 0 )); then
    echo "ERROR: ${ISO}.sha256 has malformed or unrelated entries (not bound to ${ISO_BASE}); refusing." >&2
    exit 2
  fi
  if (( matches != 1 )); then
    echo "ERROR: ${ISO}.sha256 must contain exactly one SHA-256 entry for ${ISO_BASE} (found ${matches}); refusing." >&2
    exit 2
  fi
  actual="$(sha256sum -- "$ISO")"
  actual="${actual%% *}"
  if [[ "${expected,,}" != "${actual,,}" ]]; then
    echo "ERROR: ISO SHA-256 verification FAILED for $ISO (sidecar=${expected,,}, actual=${actual,,})" >&2
    exit 1
  fi
elif [[ "$ALLOW_UNVERIFIED" -eq 1 ]]; then
  echo "[1/6] WARNING: no ${ISO}.sha256 found; flashing UNVERIFIED ISO (--allow-unverified-iso). Hash for manual check:" >&2
  sha256sum -- "$ISO" >&2
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
# The prompt is only shown on a terminal, and the read is EOF-safe (empty stdin aborts).
if [[ "$ASSUME_YES" -eq 0 ]]; then
  if [[ -t 0 ]]; then
    printf 'About to ERASE %s (%s) and flash %s onto it. Type "%s" to confirm: ' "$DEV" "$DEV_MODEL" "$ISO" "$(basename -- "$DEV")"
  fi
  if ! read -r CONFIRM; then
    echo "Aborted: no confirmation received (EOF on stdin)." >&2
    exit 1
  fi
  [[ "$CONFIRM" == "$(basename -- "$DEV")" ]] || { echo "Aborted: confirmation did not match." >&2; exit 1; }
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

# Re-read the partition table, failing closed if the kernel refuses (e.g. EBUSY):
# continuing with a stale table could select and format the wrong partition.
partprobe_retry() {
  local dev="$1" attempt
  for attempt in 1 2 3; do
    if partprobe "$dev"; then
      udevadm settle 2>/dev/null || sleep 2
      return 0
    fi
    echo "WARNING: partprobe attempt ${attempt} failed on $dev; retrying after settle..." >&2
    udevadm settle 2>/dev/null || sleep 2
  done
  echo "ERROR: partprobe failed on $dev after 3 attempts (kernel partition table may be stale); refusing to continue." >&2
  return 1
}

# Arm the remediation trap only now: everything above is read-only (safe for --dry-run),
# everything below mutates the target device.
trap 'on_error $?' ERR

# 2. Unmount any currently mounted whole-disk or partition targets on DEV.
echo "[2/6] Unmounting existing mounts on $DEV..."
unmount_device_tree "$DEV"

# 3. Wipe stale signatures and flash verified hybrid ISO to DEV.
echo "[3/6] Wiping old partition signatures on $DEV..."
wipefs -a "$DEV"

echo "[4/6] Flashing verified ISO to $DEV (dd bs=4M conv=fsync)..."
dd if="$ISO" of="$DEV" bs=4M status=progress conv=fsync
sync
partprobe_retry "$DEV"
# Desktop auto-mounters (udisks2) immediately mount the hybrid ISO at sector 0 on $DEV;
# release that whole-disk mount so sfdisk/partprobe/mkfs.ext4 do not hit EBUSY.
unmount_device_tree "$DEV"

# 4. Append a Linux (ext4, type 83) partition at START_SECTORS without disturbing the ISO partitions.
echo "[5/6] Appending data partition (${LABEL}, ext4) starting at ${START_GIB} GiB (sector ${START_SECTORS})..."
printf '%s,,83\n' "$START_SECTORS" | sfdisk --append --no-reread "$DEV"
partprobe_retry "$DEV"
unmount_device_tree "$DEV"

# Locate the appended partition strictly by its start sector (exactly one match required);
# never guess by listing order, which can resolve to an existing ISO/EFI partition.
mapfile -t PART_MATCHES < <(lsblk -nr -o TYPE,START,PATH "$DEV" 2>/dev/null | awk -v s="$START_SECTORS" '$1 == "part" && ($2 + 0) == (s + 0) { print $3 }')
if [[ ${#PART_MATCHES[@]} -ne 1 || -z "${PART_MATCHES[0]}" ]]; then
  echo "ERROR: expected exactly one partition on $DEV starting at sector ${START_SECTORS}, found ${#PART_MATCHES[@]}; refusing to format anything" >&2
  exit 1
fi
PART="${PART_MATCHES[0]}"
[[ -b "$PART" ]] || { echo "ERROR: selected partition is not a block device: $PART" >&2; exit 1; }

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
echo "  bash \"$(cd "$(dirname -- "$0")" && pwd)/freeze-for-migration.sh\" --dest \"${MOUNT_DIR}/aletheia-migration\" --slim"
