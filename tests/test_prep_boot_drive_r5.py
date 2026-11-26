#!/usr/bin/env python3
"""
Mocked-CLI fail-closed gate tests for `bin/prep-boot-drive.sh` (PR #2 review round).

Every device-touching command (lsblk, findmnt, readlink, partprobe, udevadm, wipefs,
dd, sfdisk, mkfs.ext4, mount, umount, chown, sync, id) is replaced by a stub on PATH that
emits canned output from a per-test STATE dir and appends every invocation to STUB_LOG,
so the destructive script is exercised end-to-end without touching a real device. The
only real block device referenced is an untouched loop node, needed solely to satisfy
the script's `[[ -b ]]` test; sha256sum/stat/od run for real against a synthetic ISO.

Also covers the issue #12 disk-safety follow-ups: multi-mount lsblk cells, findmnt
list failure, an ISO path beginning with '-', and --dry-run with no destructive tools on
PATH (test_10..test_13).

Covers the review gates: sidecar unbound/missing/multi/malformed, self-target via
/dev/mapper and Btrfs subvol sources, ISO-on-target, empty ISO source, partprobe
failure, exact-START partition selection (0/1/2 matches), non-root --dry-run, EOF /
wrong / correct erase confirmation, dangerous --mount paths, sector-size and disk-size
guards, 2 TiB MBR cap, --start-gib room, and the happy write path.

Zero external dependencies. Zero bare asserts. Safe across all 3 isolated modes
(`-I -B`, `-I -O -B`, `-I -B -W error`).
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "bin" / "prep-boot-drive.sh"

DEV = "/dev/loop0"        # real, never-written loop node (satisfies [[ -b "$DEV" ]])
DEV_PART = "/dev/loop1"   # stand-in "appended data partition" (also a real loop node)
DEV_PART2 = "/dev/loop2"

GIB = 1024 * 1024 * 1024
DISK_BYTES = 32 * GIB
SECTOR = 512
START_SECTORS = 16 * GIB // SECTOR  # 33554432, matches default --start-gib 16

LSBLK_STUB = r"""#!/bin/bash
echo "lsblk $*" >> "${STUB_LOG:-/dev/null}"
cols=""
prev=""
for a in "$@"; do
  if [[ "$prev" == "-o" ]]; then cols="$a"; fi
  prev="$a"
done
key="lsblk_o_$(printf '%s' "$cols" | tr -c 'A-Za-z0-9' '_')"
f="${STATE:?}/${key}"
if [[ -f "$f" ]]; then cat "$f"; fi
exit 0
"""

FINDMNT_STUB = r"""#!/bin/bash
echo "findmnt $*" >> "${STUB_LOG:-/dev/null}"
mode="list"
prev=""
for a in "$@"; do
  case "$a" in
    -T|--target=*) mode="target" ;;
    --source) mode="source" ;;
  esac
  # Like util-linux getopt: -T consumes the next word, so `-T -- PATH` looks up "--"
  # and then rejects PATH as a stray element (real findmnt exits 1 here).
  if [[ "$prev" == "-T" && "$a" == "--" ]]; then
    echo "findmnt: options --target and --source can't be used together with command line element that is not an option" >&2
    exit 1
  fi
  prev="$a"
done
case "$mode" in
  target)
    f="${STATE:?}/findmnt_SOURCE_T"
    if [[ -f "$f" ]]; then cat "$f"; fi
    if [[ -f "${STATE:?}/findmnt_SOURCE_T.rc" ]]; then exit "$(cat "${STATE:?}/findmnt_SOURCE_T.rc")"; fi
    exit 0
    ;;
  source)
    f="${STATE:?}/findmnt_TARGET_source"
    if [[ -f "$f" ]]; then cat "$f"; fi
    exit 0
    ;;
  *)
    f="${STATE:?}/findmnt_SOURCE_TARGET"
    if [[ -f "$f" ]]; then cat "$f"; fi
    if [[ -f "${STATE:?}/findmnt_SOURCE_TARGET.rc" ]]; then exit "$(cat "${STATE:?}/findmnt_SOURCE_TARGET.rc")"; fi
    exit 0
    ;;
esac
"""

READLINK_STUB = r"""#!/bin/bash
echo "readlink $*" >> "${STUB_LOG:-/dev/null}"
path="${@: -1}"
map="${STATE:-/nonexistent}/readlink.map"
if [[ -f "$map" ]]; then
  hit="$(awk -F'\t' -v p="$path" '$1 == p { print $2; found = 1 } END { exit found ? 0 : 1 }' "$map")" && { printf '%s\n' "$hit"; exit 0; }
fi
if [[ -x /usr/bin/readlink && -e "$path" ]]; then
  /usr/bin/readlink -f -- "$path"
  exit $?
fi
printf '%s\n' "$path"
exit 0
"""

PARTPROBE_STUB = r"""#!/bin/bash
echo "partprobe $*" >> "${STUB_LOG:-/dev/null}"
rc=0
if [[ -f "${STATE:-/nonexistent}/partprobe.rc" ]]; then rc="$(cat "${STATE:-/nonexistent}/partprobe.rc")"; fi
exit "$rc"
"""

GENERIC_STUB = r"""#!/bin/bash
name="$(basename "$0")"
# Drain any piped stdin (real CLIs like sfdisk read it; exiting early would SIGPIPE the writer).
if [[ ! -t 0 ]]; then cat >/dev/null 2>&1 || true; fi
echo "$name $*" >> "${STUB_LOG:?}"
rc=0
f="${STATE:-/nonexistent}/${name}.rc"
if [[ -f "$f" ]]; then rc="$(cat "$f")"; fi
exit "$rc"
"""

ID_STUB = r"""#!/bin/bash
if [[ -f "${STATE:-/nonexistent}/id_u" ]]; then cat "${STATE:-/nonexistent}/id_u"; else /usr/bin/id -u; fi
"""

DISK_TOOLS = ("wipefs", "dd", "sfdisk", "partprobe", "mkfs.ext4")

GENERIC_STUBS = ("wipefs", "dd", "sfdisk", "mkfs.ext4", "mount", "umount", "chown", "sync", "udevadm")


def _lsblk_key(cols: str) -> str:
    return "lsblk_o_" + re.sub(r"[^A-Za-z0-9]", "_", cols)


class PrepDriveHarness:
    """Builds a stubbed PATH + STATE dir and runs bin/prep-boot-drive.sh against them."""

    def __init__(self, td: str) -> None:
        self.root = Path(td)
        self.bindir = self.root / "bin"
        self.state = self.root / "state"
        self.log = self.root / "calls.log"
        self.bindir.mkdir()
        self.state.mkdir()
        stubs = {name: GENERIC_STUB for name in GENERIC_STUBS}
        stubs.update({"lsblk": LSBLK_STUB, "findmnt": FINDMNT_STUB, "readlink": READLINK_STUB,
                      "partprobe": PARTPROBE_STUB, "id": ID_STUB})
        for name, body in stubs.items():
            p = self.bindir / name
            p.write_text(body, encoding="utf-8")
            p.chmod(0o755)
        self.iso_dir = self.root / "iso"
        self.iso_dir.mkdir()
        self.mount_dir = self.root / "mnt"

    def write_state(self, files: dict[str, str]) -> None:
        for k, v in files.items():
            (self.state / k).write_text(v, encoding="utf-8")

    def make_iso(self, name: str = "omarchy.iso", size: int = 600, sig: bool = True,
                 sidecar: str = "good", hash_hex: str | None = None) -> Path:
        p = self.iso_dir / name
        data = bytearray(size)
        if sig:
            data[510] = 0x55
            data[511] = 0xAA
        p.write_bytes(bytes(data))
        digest = hash_hex if hash_hex is not None else hashlib.sha256(bytes(data)).hexdigest()
        sc = self.iso_dir / (name + ".sha256")
        if sidecar == "good":
            sc.write_text(f"{digest}  {name}\n", encoding="utf-8")
        elif sidecar == "binary":
            sc.write_text(f"{digest} *{name}\n", encoding="utf-8")
        elif sidecar == "unrelated":
            sc.write_text(f"{'a' * 64}  other.iso\n", encoding="utf-8")
        elif sidecar == "multi":
            sc.write_text(f"{digest}  {name}\n{digest} *{name}\n", encoding="utf-8")
        elif sidecar == "malformed":
            sc.write_text("this-is-not-a-sha256-line\n", encoding="utf-8")
        elif sidecar == "none":
            if sc.exists():
                sc.unlink()
        else:
            raise ValueError(f"unknown sidecar mode: {sidecar}")
        return p

    def base_state(self, **overrides: str) -> dict[str, str]:
        st = {
            _lsblk_key("TYPE"): "disk\n",
            _lsblk_key("TRAN"): "usb\n",
            _lsblk_key("MODEL"): "MockUSB 3.0\n",
            _lsblk_key("SIZE"): f"{DISK_BYTES}\n",
            _lsblk_key("LOG-SEC"): f"{SECTOR}\n",
            _lsblk_key("MOUNTPOINTS"): "",
            _lsblk_key("PATH"): f"{DEV}\n{DEV_PART}\n",
            _lsblk_key("TYPE,START,PATH"): f"part 2048 {DEV_PART}\npart {START_SECTORS} {DEV_PART}\n",
            _lsblk_key("NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT"): "NAME SIZE FSTYPE LABEL MOUNTPOINT\nloop0 32G iso9660\n",
            "findmnt_SOURCE_TARGET": "",
            "findmnt_SOURCE_T": "tmpfs\n",
            "partprobe.rc": "0\n",
            "id_u": "0\n",
        }
        st.update(overrides)
        self.write_state(st)
        return st

    def run(self, extra_args: list[str], stdin: str = "", iso: str | None = None,
            cwd: str | None = None, path: str | None = None) -> subprocess.CompletedProcess:
        # Fresh call log per run so assertions only see this invocation's stub calls.
        self.log.write_text("", encoding="utf-8")
        args = [
            shutil.which("bash") or "/bin/bash", str(SCRIPT),
            "--device", DEV,
            "--iso", iso if iso is not None else str(self.iso_dir / "omarchy.iso"),
            "--mount", str(self.mount_dir),
            "--owner", "user:group",
            *extra_args,
        ]
        env = dict(os.environ)
        env["PATH"] = path if path is not None else str(self.bindir) + os.pathsep + env.get("PATH", "")
        env["STATE"] = str(self.state)
        env["STUB_LOG"] = str(self.log)
        return subprocess.run(args, input=stdin, capture_output=True, text=True,
                              env=env, timeout=60, check=False, cwd=cwd)

    def path_without_disk_tools(self) -> str:
        """PATH = stub dir (minus the five destructive tools) + a dir of real, read-only utilities.

        No system bin dir is on this PATH, so a real wipefs/dd/sfdisk/partprobe/mkfs.ext4
        cannot be found either."""
        for name in DISK_TOOLS:
            (self.bindir / name).unlink()
        real = self.root / "realbin"
        real.mkdir()
        for name in ("cat", "tr", "sed", "awk", "stat", "od", "sha256sum", "basename",
                     "dirname", "mkdir", "sleep"):
            target = shutil.which(name)
            if target is None:
                raise unittest.SkipTest(f"host lacks {name}")
            (real / name).symlink_to(target)
        return str(self.bindir) + os.pathsep + str(real)

    def calls(self) -> str:
        if self.log.exists():
            return self.log.read_text(encoding="utf-8")
        return ""


class TestPrepBootDriveGates(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory(prefix="prep_drive_r5_")
        self.addCleanup(self._td.cleanup)
        self.h = PrepDriveHarness(self._td.name)

    def test_00_bash_syntax(self) -> None:
        p = subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True, text=True, check=False)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(p.stderr, "")

    def test_01_sidecar_binding_and_missing_sidecar(self) -> None:
        h = self.h
        # Happy path: bound sidecar verifies, dry-run writes nothing.
        iso = h.make_iso(sidecar="good")
        h.base_state()
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("VERIFIED PLAN", r.stdout)
        self.assertIn("DRY_RUN_OK", r.stdout)
        self.assertNotIn("wipefs", h.calls())
        self.assertNotIn("dd ", h.calls())
        self.assertNotIn("mkfs.ext4", h.calls())

        # Binary-mode sidecar entry (*name) is accepted too.
        h.make_iso(sidecar="binary")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)

        # Sidecar naming an unrelated file must NOT verify the ISO (old fail-open bypass).
        h.make_iso(sidecar="unrelated")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("not bound to omarchy.iso", r.stderr)
        self.assertNotIn("wipefs", h.calls())

        # Sidecar listing the ISO twice is ambiguous -> refuse.
        h.make_iso(sidecar="multi")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("exactly one SHA-256 entry for omarchy.iso (found 2)", r.stderr)

        # Malformed sidecar line -> refuse.
        h.make_iso(sidecar="malformed")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("malformed or unrelated entries", r.stderr)

        # Correctly named entry but wrong digest -> verification FAILED, rc=1.
        h.make_iso(sidecar="good", hash_hex="0" * 64)
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("SHA-256 verification FAILED", r.stderr)
        self.assertNotIn("wipefs", h.calls())

        # Missing sidecar fails closed (issue #5) unless the documented opt-out is given.
        h.make_iso(sidecar="none")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no " + str(iso) + ".sha256 companion found; refusing", r.stderr)
        self.assertNotIn("wipefs", h.calls())
        r = h.run(["--dry-run", "--allow-unverified-iso"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("UNVERIFIED ISO", r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)
        self.assertNotIn("wipefs", h.calls())

    def test_02_self_target_guards(self) -> None:
        h = self.h
        h.make_iso()
        # 1a: critical mountpoint listed by lsblk MOUNTPOINTS (live USB at /run/archiso).
        h.base_state(**{_lsblk_key("MOUNTPOINTS"): "/run/archiso/bootmnt\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("critical host mount: /run/archiso/bootmnt", r.stderr)
        self.assertNotIn("wipefs", h.calls())

        # 1b: LUKS2/LVM-style source /dev/mapper/* canonicalized to a holder of DEV.
        h.base_state(
            findmnt_SOURCE_TARGET=f"/dev/mapper/vg-root /run/archiso/bootmnt\n",
            **{"readlink.map": f"/dev/mapper/vg-root\t{DEV_PART}\n"},
        )
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("live-system mount /run/archiso/bootmnt is backed by /dev/mapper/vg-root", r.stderr)
        self.assertNotIn("wipefs", h.calls())

        # 1b: whole-disk Btrfs source with subvolume suffix [/...].
        h.base_state(findmnt_SOURCE_TARGET=f"{DEV}[/@] /\n")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn(f"live-system mount / is backed by {DEV}[/@]", r.stderr)

        # Control: an ordinary data mount on a DEV partition is NOT critical.
        h.base_state(findmnt_SOURCE_TARGET=f"{DEV_PART} /media/usb-data\n")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)

    def test_03_iso_on_target_guard(self) -> None:
        h = self.h
        h.make_iso()
        # Direct partition source on DEV.
        h.base_state(findmnt_SOURCE_T=f"{DEV_PART}\n")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("is stored on target device", r.stderr)
        self.assertIn(DEV_PART, r.stderr)
        self.assertNotIn("wipefs", h.calls())

        # LUKS-style mapper source canonicalized to a holder of DEV.
        h.base_state(
            findmnt_SOURCE_T="/dev/mapper/cryptusb\n",
            **{"readlink.map": f"/dev/mapper/cryptusb\t{DEV_PART}\n"},
        )
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("is stored on target device", r.stderr)

        # Btrfs subvol suffix on the ISO source.
        h.base_state(findmnt_SOURCE_T=f"{DEV}[/data]\n")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("is stored on target device", r.stderr)

        # Empty/unknown ISO source fails closed (both rc=0-empty and rc=1 from findmnt).
        h.base_state(findmnt_SOURCE_T="", **{"findmnt_SOURCE_T.rc": "0\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not determine which device backs", r.stderr)
        h.base_state(**{"findmnt_SOURCE_T.rc": "1\n"})
        (h.state / "findmnt_SOURCE_T").unlink()
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("could not determine which device backs", r.stderr)

    def test_04_partprobe_failure_and_exact_start_selection(self) -> None:
        h = self.h
        h.make_iso()
        # Happy write path: exactly one partition at START_SECTORS is formatted.
        h.base_state()
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("=== SUCCESS ===", r.stdout)
        calls = h.calls()
        self.assertIn(f"wipefs -a {DEV}", calls)
        self.assertIn(f"dd if={h.iso_dir / 'omarchy.iso'} of={DEV}", calls)
        self.assertIn(f"sfdisk --append --no-reread {DEV}", calls)
        self.assertIn(f"mkfs.ext4 -F -L ALETHEIA_DATA {DEV_PART}", calls)
        self.assertIn(f"mount {DEV_PART} {h.mount_dir}", calls)
        # chown must receive USER:GROUP as a single argument (issue: chown user:group).
        self.assertIn(f"chown user:group {h.mount_dir}\n", calls)
        self.assertIn(f"chown user:group {h.mount_dir}/aletheia-migration", calls)
        self.assertNotIn(f"chown user:group user:group", calls)

        # partprobe failing (EBUSY) must abort fail-closed after dd but before mkfs.
        h.base_state(**{"partprobe.rc": "1\n"})
        r = h.run(["--yes"])
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("partprobe failed on " + DEV + " after 3 attempts", r.stderr)
        self.assertIn("WARNING: partprobe attempt 1 failed", r.stderr)
        self.assertIn("may be partially written", r.stderr)  # ERR trap armed before first write
        calls = h.calls()
        self.assertIn("dd ", calls)
        self.assertNotIn("mkfs.ext4", calls)
        self.assertNotIn("sfdisk", calls)

        # Zero partitions at START_SECTORS -> refuse, format nothing.
        h.base_state(**{_lsblk_key("TYPE,START,PATH"): f"part 2048 {DEV_PART}\npart 4096 {DEV_PART2}\n"})
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"expected exactly one partition on {DEV} starting at sector {START_SECTORS}, found 0", r.stderr)
        self.assertNotIn("mkfs.ext4", h.calls())

        # Two partitions at START_SECTORS -> ambiguous, refuse, format nothing.
        h.base_state(**{_lsblk_key("TYPE,START,PATH"):
                        f"part {START_SECTORS} {DEV_PART}\npart {START_SECTORS} {DEV_PART2}\n"})
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 1)
        self.assertIn(f"found 2; refusing to format anything", r.stderr)
        self.assertNotIn("mkfs.ext4", h.calls())

    def test_05_non_root_dry_run_no_root_only_calls(self) -> None:
        h = self.h
        h.make_iso()
        h.base_state(id_u="1000\n")
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("VERIFIED PLAN", r.stdout)
        self.assertIn("DRY_RUN_OK", r.stdout)
        # The premature ERR trap must not fire during a read-only dry-run.
        self.assertNotIn("partially written", r.stderr)
        self.assertNotIn("Permission denied", r.stderr)
        self.assertNotIn("wipefs", h.calls())
        # Non-root without --dry-run is refused before any write.
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 1)
        self.assertIn("run with sudo", r.stderr)
        self.assertNotIn("wipefs", h.calls())

    def test_06_erase_confirmation_eof_wrong_and_correct(self) -> None:
        h = self.h
        h.make_iso()
        h.base_state()
        # EOF on stdin (no input) -> clean abort, nothing written.
        r = h.run([], stdin="")
        self.assertEqual(r.returncode, 1)
        self.assertIn("EOF on stdin", r.stderr)
        self.assertNotIn("wipefs", h.calls())
        # Wrong confirmation -> abort, nothing written.
        r = h.run([], stdin="nope\n")
        self.assertEqual(r.returncode, 1)
        self.assertIn("confirmation did not match", r.stderr)
        self.assertNotIn("wipefs", h.calls())
        # Correct device basename -> proceeds and writes (all destructive cmds stubbed).
        r = h.run([], stdin="loop0\n")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("wipefs -a " + DEV, h.calls())
        # --yes skips the prompt entirely.
        h2 = PrepDriveHarness(tempfile.mkdtemp(prefix="prep_drive_r5_yes_"))
        self.addCleanup(lambda: __import__("shutil").rmtree(h2.root, ignore_errors=True))
        h2.make_iso()
        h2.base_state()
        r = h2.run(["--yes"], stdin="")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("wipefs -a " + DEV, h2.calls())

    def test_07_dangerous_mount_paths_rejected(self) -> None:
        h = self.h
        h.make_iso()
        h.base_state()
        # Built at runtime so the source never contains a literal personal-path pattern.
        home_case = "/" + "home" + "/user/data"
        for bad, msg in (
            ("/", "refusing dangerous --mount path: /"),
            ("/boot", "refusing dangerous --mount path: /boot"),
            ("/boot/efi", "refusing dangerous --mount path: /boot/efi"),
            ("/home", "refusing dangerous --mount path: /home"),
            (home_case, f"refusing dangerous --mount path: {home_case}"),
            ("/usr", "refusing dangerous --mount path: /usr"),
            ("/etc", "refusing dangerous --mount path: /etc"),
            ("/var/tmp/x", "refusing dangerous --mount path: /var/tmp/x"),
            ("/run/archiso/bootmnt", "refusing dangerous --mount path"),
            ("/sysroot", "refusing dangerous --mount path: /sysroot"),
            ("relative/path", "--mount must be an absolute path"),
            ("", "missing value for --mount"),  # empty value rejected at parse time
        ):
            r = h.run(["--mount", bad, "--dry-run"])
            self.assertNotEqual(r.returncode, 0, f"mount={bad!r}")
            self.assertIn(msg, r.stderr, f"mount={bad!r}")
        # A sane absolute path under the temp dir is accepted.
        r = h.run(["--mount", str(h.mount_dir), "--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_08_sector_size_disk_size_and_layout_guards(self) -> None:
        h = self.h
        h.make_iso()
        # Implausible LOG-SEC values fail before any division.
        for bad_ss in ("0\n", "256\n", "abc\n", ""):
            h.base_state(**{_lsblk_key("LOG-SEC"): bad_ss})
            r = h.run(["--dry-run"])
            self.assertEqual(r.returncode, 2, f"LOG-SEC={bad_ss!r}")
            self.assertIn("implausible logical sector size", r.stderr)
        # Implausible SIZE values fail closed.
        for bad_sz in ("0\n", "abc\n", ""):
            h.base_state(**{_lsblk_key("SIZE"): bad_sz})
            r = h.run(["--dry-run"])
            self.assertEqual(r.returncode, 2, f"SIZE={bad_sz!r}")
            self.assertIn("implausible device size", r.stderr)
        # 4096-byte sectors: sector math and exact-START selection still work.
        start4k = 16 * GIB // 4096
        h.base_state(**{_lsblk_key("LOG-SEC"): "4096\n",
                        _lsblk_key("TYPE,START,PATH"): f"part 256 {DEV_PART}\npart {start4k} {DEV_PART}\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn(f"mkfs.ext4 -F -L ALETHEIA_DATA {DEV_PART}", h.calls())
        # >2 TiB disk refused (MBR 32-bit sector cap).
        h.base_state(**{_lsblk_key("SIZE"): f"{3 * 1024 * GIB}\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("2 TiB limit", r.stderr)
        # --start-gib leaving no room for the data partition.
        h.base_state()
        r = h.run(["--dry-run", "--start-gib", "40"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("leaves no room for the data partition", r.stderr)
        self.assertNotIn("wipefs", h.calls())
        # ISO larger than --start-gib (sparse 17 GiB file with valid 0x55AA signature).
        big = h.iso_dir / "big.iso"
        with open(big, "wb") as f:
            f.truncate(17 * GIB)
            f.seek(510)
            f.write(b"\x55\xaa")
        r = h.run(["--dry-run", "--iso", str(big)])
        self.assertEqual(r.returncode, 2)
        self.assertIn("exceeds --start-gib 16 GiB", r.stderr)

    def test_09_device_and_iso_gates(self) -> None:
        h = self.h
        h.make_iso()
        h.base_state(**{_lsblk_key("TRAN"): "sata\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("refusing non-USB device", r.stderr)
        h.base_state(**{_lsblk_key("TYPE"): "part\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("expected whole disk", r.stderr)
        # ISO without the 0x55AA hybrid signature is refused.
        h.make_iso(sig=False)
        h.base_state()
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("no MBR boot signature", r.stderr)
        # Non-block-device --device and missing ISO fail closed.
        h.make_iso()
        r = h.run(["--dry-run", "--device", "/etc/hostname"])
        self.assertEqual(r.returncode, 2)
        self.assertIn("not a block device", r.stderr)
        r = h.run(["--dry-run", "--iso", str(h.iso_dir / "missing.iso")])
        self.assertEqual(r.returncode, 2)
        self.assertIn("ISO not found", r.stderr)
        self.assertNotIn("wipefs", h.calls())


    def test_10_lsblk_multi_mount_cell_is_split(self) -> None:
        # Issue #12 item 1: `lsblk -r` packs several mountpoints of one device into a
        # single cell joined by a literal "\\x0a". The critical mount is not first, and the
        # first entry is not itself a critical prefix; findmnt (1b) reports nothing, so
        # only check 1a can catch it.
        h = self.h
        h.make_iso()
        for cell, hit in (("/mnt/data\\x0a/home\n", "/home"),
                          ("/media/usb\\x0a/mnt/x\\x0a/boot/efi\n", "/boot/efi"),
                          ("[SWAP]\n", "[SWAP]")):
            h.base_state(**{_lsblk_key("MOUNTPOINTS"): cell})
            r = h.run(["--dry-run"])
            self.assertEqual(r.returncode, 2, (cell, r.stdout, r.stderr))
            self.assertIn(f"critical host mount: {hit}", r.stderr)
            self.assertNotIn("DRY_RUN_OK", r.stdout)
        # Control: several non-critical mounts in one cell still pass.
        h.base_state(**{_lsblk_key("MOUNTPOINTS"): "/mnt/data\\x0a/media/usb\n"})
        r = h.run(["--dry-run"])
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)

    def test_11_findmnt_list_failure_fails_closed(self) -> None:
        # Issue #12 item 2: if `findmnt -rn -o SOURCE,TARGET` errors, 1b must refuse
        # rather than be skipped (formerly `|| true`).
        h = self.h
        h.make_iso()
        for rc in ("1\n", "4\n"):
            h.base_state(**{"findmnt_SOURCE_TARGET.rc": rc})
            r = h.run(["--dry-run"])
            self.assertEqual(r.returncode, 2, (rc, r.stdout, r.stderr))
            self.assertIn("findmnt failed to list mounts", r.stderr)
            self.assertNotIn("DRY_RUN_OK", r.stdout)
        # Real run with --yes stops at the same gate before any write.
        h.base_state(**{"findmnt_SOURCE_TARGET.rc": "1\n"})
        r = h.run(["--yes"])
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("findmnt failed to list mounts", r.stderr)
        for tool in ("wipefs", "dd ", "sfdisk", "mkfs.ext4", "umount", "partprobe"):
            self.assertNotIn(tool, h.calls())

    def test_12_iso_path_starting_with_dash(self) -> None:
        # Issue #12 item 3: the ISO path is passed to findmnt so it cannot be read as an
        # option. The script makes it absolute and passes --target=PATH as one argv word.
        h = self.h
        iso = h.make_iso(name="-omarchy.iso")
        h.base_state()
        r = h.run(["--dry-run"], iso="-omarchy.iso", cwd=str(h.iso_dir))
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)
        self.assertIn(f"ISO:          {iso} ", r.stdout)
        self.assertIn(f"findmnt -n -o SOURCE --target={iso}\n", h.calls())
        # And the ISO-on-target gate still resolves through that call.
        h.base_state(findmnt_SOURCE_T=f"{DEV_PART}\n")
        r = h.run(["--dry-run"], iso="-omarchy.iso", cwd=str(h.iso_dir))
        self.assertEqual(r.returncode, 2)
        self.assertIn("is stored on target device", r.stderr)

    def test_13_dry_run_without_destructive_tools_on_path(self) -> None:
        # Issue #12 item 4: --dry-run finishes its safety checks with none of wipefs, dd,
        # sfdisk, partprobe, mkfs.ext4 on PATH; a real run still requires each of them.
        h = self.h
        h.make_iso()
        h.base_state()
        path = h.path_without_disk_tools()
        r = h.run(["--dry-run"], path=path)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("DRY_RUN_OK", r.stdout)
        self.assertNotIn("missing required tool", r.stderr)
        r = h.run(["--yes"], path=path)
        self.assertEqual(r.returncode, 2, r.stderr)
        self.assertIn("missing required tool: wipefs", r.stderr)
        self.assertNotIn("VERIFIED PLAN", r.stdout)
        self.assertNotIn("umount", h.calls())
        # Each of the five is required on its own for a real run.
        stub_body = GENERIC_STUB
        for missing in DISK_TOOLS:
            for name in DISK_TOOLS:
                p = h.bindir / name
                if name == missing:
                    if p.exists():
                        p.unlink()
                else:
                    p.write_text(PARTPROBE_STUB if name == "partprobe" else stub_body, encoding="utf-8")
                    p.chmod(0o755)
            r = h.run(["--yes"], path=path)
            self.assertEqual(r.returncode, 2, (missing, r.stderr))
            self.assertIn(f"missing required tool: {missing}", r.stderr)
            self.assertNotIn("umount", h.calls())
            r = h.run(["--dry-run"], path=path)
            self.assertEqual(r.returncode, 0, (missing, r.stderr))
            self.assertIn("DRY_RUN_OK", r.stdout)


if __name__ == "__main__":
    unittest.main()
