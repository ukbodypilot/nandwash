"""Can this machine run the offline maintenance boot (nandwash-maint), and what exactly would installing it change?

Everything here is READ-ONLY. `check()` inspects the machine and returns a verdict with a reason per finding;
`plan()` renders the files an install would write. Neither writes anything.

Deliberately an ALLOWLIST: GRUB + an ext2/3/4 or XFS root that is a plain partition. Anything else is reported
unsupported with the reason, never guessed at.
"""
from __future__ import annotations

import glob
import hashlib
import os
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from typing import List, Optional

MAINT_PATH = "/usr/local/sbin/nandwash-maint"
CONF_PATH = "/etc/nandwash/maint.conf"
ENTRY_ID = "nandwash-maint"
MARK_BEGIN, MARK_END = "# >>> nandwash-maint (managed) >>>", "# <<< nandwash-maint (managed) <<<"
ROOT_FS_OK = ("ext4", "ext3", "ext2", "xfs")
_INSMOD = {"ext4": "ext2", "ext3": "ext2", "ext2": "ext2", "xfs": "xfs", "vfat": "fat"}
_PCI = re.compile(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d")


@dataclass
class Check:
    name: str
    status: str            # ok | warn | fail | info
    detail: str


@dataclass
class Report:
    checks: List[Check] = field(default_factory=list)
    facts: dict = field(default_factory=dict)       # what plan() needs; filled as checks succeed

    def add(self, name, status, detail):
        self.checks.append(Check(name, status, detail))

    @property
    def failures(self) -> List[Check]:
        return [c for c in self.checks if c.status == "fail"]

    @property
    def warnings(self) -> List[Check]:
        return [c for c in self.checks if c.status == "warn"]

    @property
    def verdict(self) -> str:
        if self.failures:
            return "unsupported"
        return "supported-with-warnings" if self.warnings else "supported"

    def to_dict(self) -> dict:
        return {"verdict": self.verdict, "checks": [asdict(c) for c in self.checks]}


# ---------- pure helpers (unit-tested) ----------
def parse_mountinfo(text: str) -> list:
    """[(mountpoint, fstype, source, "maj:min")] from /proc/self/mountinfo text."""
    out = []
    for line in text.splitlines():
        if " - " not in line:
            continue
        left, right = line.split(" - ", 1)
        lf, rf = left.split(), right.split()
        if len(lf) < 5 or len(rf) < 2:
            continue
        out.append((lf[4].replace("\\040", " "), rf[0], rf[1], lf[2]))
    return out


def mount_for(path: str, mounts: list) -> Optional[tuple]:
    """The mount entry that holds `path` (longest mountpoint prefix; the LAST one wins on a tie)."""
    best = None
    for m in mounts:
        mp = m[0]
        if path == mp or path.startswith(mp.rstrip("/") + "/") or mp == "/":
            if best is None or len(mp) >= len(best[0]):
                best = m
    return best


def parse_cmdline(text: str) -> list:
    return shlex.split(text.strip())


def maint_cmdline(args: list) -> list:
    """The running kernel's arguments, turned into the maintenance entry's: root read-only, our init, no splash."""
    drop = ("BOOT_IMAGE=", "init=", "resume=", "resume_offset=")   # resume= could restore a hibernation image instead of running us
    out = [a for a in args if not a.startswith(drop) and a not in ("rw", "ro", "quiet", "splash")]
    return out + ["ro", f"init={MAINT_PATH}"]


def find_initrds(kernel_real: str) -> list:
    """Initramfs (plus CPU microcode images) that go with a kernel image, by the common distro naming schemes."""
    d, name = os.path.split(kernel_real)
    suffix = name[len("vmlinuz"):] if name.startswith("vmlinuz") else ""
    ucode = [os.path.join(d, u) for u in ("intel-ucode.img", "amd-ucode.img") if os.path.exists(os.path.join(d, u))]
    for cand in (f"initramfs{suffix}.img", f"initrd.img{suffix}", f"initramfs{suffix}", f"initrd{suffix}",
                 f"initrd{suffix}.img"):
        if os.path.exists(os.path.join(d, cand)):
            return ucode + [os.path.join(d, cand)]
    return []


def render_entry(fs_uuid: str, fstype: str, kernel_grub: str, initrd_grub: list, args: list) -> str:
    lines = [f"menuentry 'nandwash offline maintenance (one-shot)' --id {ENTRY_ID} {{"]
    if fstype in _INSMOD:
        lines.append(f"    insmod {_INSMOD[fstype]}")
    lines += [f"    search --no-floppy --fs-uuid --set=root {fs_uuid}",
              f"    linux {kernel_grub} {' '.join(shlex.quote(a) for a in args)}"]
    if initrd_grub:
        lines.append(f"    initrd {' '.join(initrd_grub)}")
    lines.append("}")
    return "\n".join(lines) + "\n"


def managed_block(entry: str) -> str:
    return f"{MARK_BEGIN}\n{entry}{MARK_END}\n"


def render_conf(device: str, serial: str, nw: str, wdt_module: str, wdt_ident: str) -> str:
    return (f"DEVICE={device}\nSERIAL={serial}\nNW={nw}\nMAXRUN=3000\nSCAN_LIMIT=900\nFIX_LIMIT=900\n"
            f"WDT_MODULE={wdt_module}\nWDT_IDENT={wdt_ident}\n")


# ---------- machine inspection ----------
def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return ""


def _uuid_of(source: str) -> str:
    try:
        real = os.path.realpath(source)
        for ln in glob.glob("/dev/disk/by-uuid/*"):
            if os.path.realpath(ln) == real:
                return os.path.basename(ln)
    except OSError:
        pass
    if shutil.which("blkid"):
        try:
            return subprocess.run(["blkid", "-o", "value", "-s", "UUID", source], capture_output=True, text=True,
                                  timeout=5).stdout.strip()
        except (subprocess.SubprocessError, OSError):
            pass
    return ""


def _disk_of(source: str) -> str:
    """Whole-disk name behind a /dev partition node ('' if it is not a plain disk/partition)."""
    name = os.path.basename(os.path.realpath(source))
    sysp = f"/sys/class/block/{name}"
    if not os.path.exists(sysp):
        return ""
    real = os.path.realpath(sysp)
    return os.path.basename(os.path.dirname(real)) if os.path.exists(os.path.join(real, "partition")) else name


def _pci_driver(disk: str) -> str:
    """Driver of the nearest PCI device above the disk (the storage controller)."""
    pci = [x for x in os.path.realpath(f"/sys/block/{disk}").split("/") if _PCI.fullmatch(x)]
    if not pci:
        return ""
    try:
        return os.path.basename(os.path.realpath(f"/sys/bus/pci/devices/{pci[-1]}/driver"))
    except OSError:
        return ""


def _grub_tools():
    for suffix in ("grub", "grub2"):
        reb, env = shutil.which(f"{suffix}-reboot"), shutil.which(f"{suffix}-editenv")
        if reb and env:
            return suffix, reb, env
    return None, None, None


def _watchdogs() -> list:
    out = []
    for w in sorted(glob.glob("/sys/class/watchdog/watchdog*")):
        ident = _read(os.path.join(w, "identity")).strip()
        try:
            t = int(_read(os.path.join(w, "timeout")).strip() or 0)
        except ValueError:
            t = 0
        if ident:
            out.append((ident, t))
    return out


def check(device: Optional[str] = None, mounts: Optional[list] = None) -> Report:
    from . import drives as drv
    r = Report()
    f = r.facts
    mounts = parse_mountinfo(_read("/proc/self/mountinfo")) if mounts is None else mounts

    # --- platform
    if sys.platform != "linux":
        r.add("platform", "fail", f"{sys.platform}: the maintenance boot is Linux-only")
        return r
    osr = {k: v for k, _q, v in re.findall(r'^(\w+)=("?)(.*?)\2$', _read("/etc/os-release"), re.M)}
    r.add("platform", "info", f"Linux, {osr.get('PRETTY_NAME') or osr.get('ID') or 'unknown distro'}, kernel {os.uname().release}")
    if os.geteuid() != 0:
        r.add("privileges", "warn", "not root: /boot/grub and the kernel log may be unreadable, so some checks are weaker. Re-run with sudo")
    if os.environ.get("SSH_CONNECTION"):
        r.add("session", "warn", "this is a REMOTE session. The maintenance boot takes the machine off the network "
                                 "for up to an hour; make sure you can reach the console or power-cycle it")
    r.add("firmware", "info", "UEFI" if os.path.isdir("/sys/firmware/efi") else "legacy BIOS")

    # --- bootloader (allowlist: GRUB)
    suffix, reboot_tool, editenv = _grub_tools()
    other = []
    if shutil.which("bootctl") and os.path.exists("/sys/firmware/efi/efivars"):
        for v in glob.glob("/sys/firmware/efi/efivars/LoaderInfo-*"):       # GRUB >= 2.12 writes this too: read it
            try:
                with open(v, "rb") as fh:
                    if "systemd-boot" in fh.read()[4:].decode("utf-16le", "replace").lower():
                        other.append("systemd-boot")
            except OSError:
                pass
    if suffix is None:
        r.add("bootloader", "fail", (f"{', '.join(other)} detected: not supported yet (only GRUB)" if other
                                     else "no GRUB tools (grub-reboot / grub-editenv) found: not supported"))
    else:
        gdir = next((d for d in (f"/boot/{suffix}", "/boot/grub", "/boot/grub2") if os.path.exists(f"{d}/grub.cfg")), None)
        if gdir is None:
            r.add("bootloader", "fail", "GRUB tools present but no grub.cfg under /boot/grub or /boot/grub2")
        else:
            f.update(grub_dir=gdir, reboot_tool=reboot_tool)
            cfg = _read(f"{gdir}/grub.cfg")
            if not cfg:
                r.add("bootloader", "warn", f"GRUB found at {gdir} but grub.cfg is unreadable without root; "
                                            "cannot confirm it honours custom.cfg / one-shot boot")
            else:
                miss = [n for n, k in (("custom.cfg", "custom.cfg"), ("one-shot boot (next_entry)", "next_entry")) if k not in cfg]
                if miss:
                    r.add("bootloader", "fail", f"GRUB at {gdir} does not support: {', '.join(miss)}")
                else:
                    r.add("bootloader", "ok", f"GRUB at {gdir}: custom.cfg and one-shot boot present ({os.path.basename(reboot_tool)})")
            gm = mount_for(gdir, mounts)
            if gm and gm[1] in ("btrfs", "zfs", "f2fs"):
                r.add("grubenv", "fail" if gm[1] == "zfs" else "warn",
                      f"/boot/grub is on {gm[1]}: GRUB cannot reliably write its environment block there, "
                      "so the one-shot entry may not be cleared")
            else:
                r.add("grubenv", "ok", "GRUB's environment block lives on a plain filesystem")
            if other:
                r.add("bootloader-other", "warn", f"{', '.join(other)} also present; make sure GRUB is the loader that boots")

    # --- root filesystem
    root = mount_for("/", mounts)
    root_disk = ""
    if not root:
        r.add("root fs", "fail", "cannot find the root mount")
    else:
        _mp, fst, src, _mm = root
        f["root_source"] = src
        if not src.startswith("/dev/") or src.startswith(("/dev/mapper/", "/dev/dm-", "/dev/md")):
            r.add("root fs", "fail", f"root is '{src}' (LUKS/LVM/RAID/overlay): the maintenance boot cannot mount it read-only; not supported")
        elif fst not in ROOT_FS_OK:
            r.add("root fs", "fail", f"root filesystem is {fst}; only {', '.join(ROOT_FS_OK)} are supported")
        else:
            root_disk = _disk_of(src)
            r.add("root fs", "ok", f"{fst} on {src}")
            hold = os.listdir(f"/sys/class/block/{os.path.basename(os.path.realpath(src))}/holders") \
                if os.path.exists(f"/sys/class/block/{os.path.basename(os.path.realpath(src))}/holders") else []
            if hold:
                r.add("root layers", "fail", f"root partition is held by {', '.join(hold)}")
        if root_disk:
            rd = drv.describe(root_disk)
            if rd and (rd.transport == "usb" or rd.thunderbolt):
                r.add("root bus", "fail", f"root disk is on {'Thunderbolt' if rd.thunderbolt else 'USB'}: unsupported")

    # --- running kernel -> the entry we would add
    args = parse_cmdline(_read("/proc/cmdline"))
    bi = next((a[len("BOOT_IMAGE="):] for a in args if a.startswith("BOOT_IMAGE=")), "")
    bi = re.sub(r"^\([^)]*\)", "", bi)
    if any(a.startswith("init=") for a in args):
        r.add("kernel args", "warn", "the running kernel already has init=...; the maintenance entry will replace it")
    kreal = next((c for c in (bi, "/boot" + bi) if bi and os.path.exists(c)), "")
    if not bi:
        r.add("kernel image", "fail", "no BOOT_IMAGE= on the kernel command line (not booted via GRUB?); cannot locate the kernel")
    elif not kreal:
        r.add("kernel image", "fail", f"running kernel {bi} not found under / or /boot")
    else:
        inits = find_initrds(kreal)
        km = mount_for(os.path.realpath(kreal), mounts)
        kuuid = _uuid_of(km[2]) if km else ""
        if not [i for i in inits if "ucode" not in i] or not km:
            r.add("kernel image", "fail", f"found {kreal} but no matching initramfs next to it")
        elif not kuuid:
            r.add("kernel image", "fail", f"cannot determine the UUID of the filesystem holding {kreal}")
        else:
            base = km[0] if km[0] != "/" else ""
            rel = lambda p: p[len(base):] if base and p.startswith(base) else p
            f.update(kernel_grub=rel(os.path.realpath(kreal)), initrd_grub=[rel(os.path.realpath(i)) for i in inits],
                     boot_uuid=kuuid, boot_fstype=km[1], args=maint_cmdline(args))
            r.add("kernel image", "ok", f"{kreal} + {', '.join(os.path.basename(i) for i in inits)} (fs {km[1]}, uuid {kuuid})")

    # --- what the maintenance script needs on the ROOT filesystem
    missing = [t for t in ("bash", "timeout", "awk", "modprobe", "mount", "sync", "dmesg", "reboot") if not shutil.which(t)]
    r.add("tools", "fail" if missing else "ok", f"missing: {', '.join(missing)}" if missing else "bash, timeout, awk, modprobe, mount, sync, dmesg, reboot present")
    if not shutil.which("smartctl"):
        r.add("smartctl", "warn", "smartctl not found (smartmontools): SMART data will be absent from the maintenance report")
    # the venv's own bin dir first (sudo drops it from PATH, and realpath(sys.executable) would leave the venv)
    nw = next((c for c in (os.path.join(os.path.dirname(os.path.abspath(sys.executable)), "nandwash"),
                           shutil.which("nandwash") or "") if c and os.path.exists(c)), "")
    if not nw:
        r.add("nandwash install", "fail", "cannot locate the installed `nandwash` executable")
    else:
        f["nw"] = nw
        bad = []
        for p in (os.path.realpath(nw), os.path.realpath(sys.executable), os.path.dirname(os.path.dirname(os.path.abspath(nw)))):
            m = mount_for(p, mounts)
            if m and m[0] != "/":
                bad.append(f"{p} is on {m[0]}")
        if bad:
            r.add("nandwash install", "fail", "nandwash must live on the ROOT filesystem (only / is mounted in the maintenance boot): " + "; ".join(bad))
        else:
            r.add("nandwash install", "ok", f"{nw} (python {os.path.realpath(sys.executable)}) is on the root filesystem")

    # --- hardware watchdog (safety net for a hang)
    wds = _watchdogs()                       # [(identity, timeout_s)]
    mods = [m.split()[0] for m in _read("/proc/modules").splitlines()]
    names = [w[0] for w in wds]
    pick = "iTCO_wdt" if "iTCO_wdt" in names else (names[0] if names else "")
    f["wdt_ident"], f["wdt_module"] = pick, (pick if pick in mods else "")
    if not wds:
        r.add("watchdog", "warn", "NO hardware watchdog (the BIOS may disable it). A hang inside the maintenance boot "
                                  "would need a power cycle; only the software time limits protect you")
    else:
        t = dict(wds)[pick]
        if t < 20:
            r.add("watchdog", "warn", f"watchdog {pick} has a {t}s timeout (<20s): the runner will not arm it")
        else:
            r.add("watchdog", "ok", f"hardware watchdog {pick} ({t}s timeout) will be armed; also present: "
                                    f"{', '.join(n for n in names if n != pick) or 'none'}")

    # --- the target drive
    if device:
        name = os.path.basename(os.path.realpath(device))
        d = drv.describe(name)
        if d is None:
            r.add("target", "fail", f"{device}: not a usable block device")
        else:
            f.update(device=d.path, serial=d.serial)
            if not d.eligible:
                r.add("target", "fail", f"{d.path}: {'; '.join(d.notes) or 'not eligible'}")
            elif not d.serial:
                r.add("target", "fail", f"{d.path} reports no serial, so the wrong-drive guard cannot be set")
            else:
                r.add("target", "ok", f"{d.path} {d.title} serial {d.serial}" + (" (the system disk)" if d.is_system else ""))
            if d.transport == "usb" or d.thunderbolt:
                r.add("target bus", "fail", f"{d.path} is on {'Thunderbolt' if d.thunderbolt else 'USB'}: the maintenance boot loads no "
                                            "hot-plug/bus drivers or authorisation, so it cannot reach it. Use the live commands")
            elif root_disk and d.name != root_disk:
                a, b = _pci_driver(d.name), _pci_driver(root_disk)
                if a and b and a != b:
                    r.add("target bus", "warn", f"{d.path} sits on a different controller driver ({a}) than the root disk ({b}); "
                                                "it may not be loaded in the maintenance boot")
                else:
                    r.add("target bus", "ok", "same controller driver as the root disk")
    else:
        r.add("target", "info", "no DEVICE given: drive-specific checks skipped")
    return r


# ---------- install plan (dry run) ----------
def plan(rep: Report) -> dict:
    """What an install would write. Requires a report with no failures and a target device."""
    f = rep.facts
    if rep.failures:
        raise ValueError("machine is not supported: " + "; ".join(c.detail for c in rep.failures))
    for k in ("device", "serial", "kernel_grub", "boot_uuid", "grub_dir", "nw"):
        if k not in f:
            raise ValueError(f"cannot plan: missing {k} (give a DEVICE and run on a supported machine)")
    script = os.path.join(os.path.dirname(__file__), "data", "nandwash-maint")
    with open(script, "rb") as fh:
        sha = hashlib.sha256(fh.read()).hexdigest()
    entry = render_entry(f["boot_uuid"], f["boot_fstype"], f["kernel_grub"], f["initrd_grub"], f["args"])
    cfgpath = f"{f['grub_dir']}/custom.cfg"
    existing = _read(cfgpath)
    return {
        "files": [
            {"path": MAINT_PATH, "mode": "0755", "action": _state(MAINT_PATH, sha_of=sha), "sha256": sha[:16],
             "content": "(the maintenance runner shipped inside nandwash)"},
            {"path": CONF_PATH, "mode": "0644", "action": "overwrite" if os.path.exists(CONF_PATH) else "create",
             "content": render_conf(f["device"], f["serial"], f["nw"], f["wdt_module"], f["wdt_ident"])},
            {"path": cfgpath, "mode": "0644",
             "action": ("replace managed block" if MARK_BEGIN in existing else "append block") if existing else "create",
             "content": managed_block(entry)},
        ],
        "run": f"sudo {os.path.basename(f['reboot_tool'])} {ENTRY_ID} && sudo reboot",
        "undo": [f"remove the block between '{MARK_BEGIN}' and '{MARK_END}' from {cfgpath}",
                 f"rm {MAINT_PATH} {CONF_PATH}", "(the one-shot entry is consumed by one boot; nothing else to undo)"],
        "untouched": ["the normal boot entries", "grub.cfg itself (custom.cfg is read at boot)", "the kernel and initramfs",
                      "the target drive (nothing is read or written until you run the maintenance boot)"],
    }


def _state(path: str, sha_of: str) -> str:
    try:
        with open(path, "rb") as fh:
            return "unchanged" if hashlib.sha256(fh.read()).hexdigest() == sha_of else "overwrite"
    except OSError:
        return "create"
