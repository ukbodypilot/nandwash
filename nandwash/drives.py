"""Find the drives attached to this machine (SATA, NVMe, USB, ...) and say what nandwash may do with each.

Pure sysfs/procfs; `blkid` is used only to name the filesystem on UNMOUNTED partitions, if present.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field, asdict
from typing import List, Optional

from .fsmap import SUPPORTED_FS

_SKIP = re.compile(r"^(loop|ram|zram|dm-|md|sr|fd|nbd|rbd|pmem)\d*")
_SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var", "/home")


def _read(path: str) -> str:
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return ""


@dataclass
class Partition:
    name: str
    path: str
    devnum: str                 # "8:1"
    start: int                  # bytes from the start of the disk
    size: int                   # bytes
    fstype: str = ""            # "" = unknown / none
    mounts: List[tuple] = field(default_factory=list)   # (mountpoint, writable)
    swap: bool = False

    @property
    def mounted(self) -> bool:
        return bool(self.mounts)

    @property
    def writable(self) -> bool:
        return any(w for _m, w in self.mounts)


@dataclass
class Drive:
    path: str
    name: str
    vendor: str
    model: str
    serial: str
    size: int
    transport: str              # sata / nvme / usb / virtio / mmc / scsi / ?
    rotational: bool
    removable: bool
    partitions: List[Partition] = field(default_factory=list)
    holders: List[str] = field(default_factory=list)
    is_system: bool = False
    notes: List[str] = field(default_factory=list)
    eligible: bool = True
    modes: str = ""

    def to_dict(self) -> dict:
        return asdict(self)

    @property
    def title(self) -> str:
        v = "" if self.vendor.upper() == "ATA" else self.vendor
        return " ".join(x for x in (v, self.model) if x) or self.name


def _mounts_by_devnum() -> dict:
    """{"maj:min": [(mountpoint, fstype, writable)]} from mountinfo (superblock rw flag, not the bind flag)."""
    out: dict = {}
    try:
        with open("/proc/self/mountinfo") as f:
            for line in f:
                left, right = line.rstrip("\n").split(" - ", 1)
                lf, rf = left.split(), right.split()
                mp = lf[4].replace("\\040", " ")
                out.setdefault(lf[2], []).append((mp, rf[0], "rw" in rf[2].split(",")))
    except OSError:
        pass
    return out


def _swap_devices() -> set:
    s = set()
    try:
        with open("/proc/swaps") as f:
            next(f, None)
            for line in f:
                s.add(os.path.realpath(line.split()[0]))
    except OSError:
        pass
    return s


def _blkid_type(path: str) -> str:
    if not shutil.which("blkid"):
        return ""
    try:
        return subprocess.run(["blkid", "-o", "value", "-s", "TYPE", path], capture_output=True, text=True,
                              timeout=5).stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return ""


def _transport(name: str) -> str:
    try:
        real = os.path.realpath(f"/sys/block/{name}")
    except OSError:
        return "?"
    if name.startswith("nvme"):
        return "nvme"
    for key, label in (("/usb", "usb"), ("/ata", "sata"), ("/virtio", "virtio"), ("/mmc", "mmc"),
                       ("/nvme", "nvme"), ("/host", "scsi")):
        if key in real:
            return label
    return "?"


def _serial(name: str, path: str) -> str:
    s = _read(f"/sys/block/{name}/device/serial") or _read(f"/sys/block/{name}/serial")
    if s:
        return s
    from .device import _udev_serial
    return _udev_serial(path) or ""


def describe(name: str, mounts: Optional[dict] = None, swaps: Optional[set] = None) -> Optional[Drive]:
    base = f"/sys/block/{name}"
    if not os.path.isdir(base):
        return None
    sectors = int(_read(f"{base}/size") or 0)
    if sectors <= 0:
        return None
    mounts = _mounts_by_devnum() if mounts is None else mounts
    swaps = _swap_devices() if swaps is None else swaps
    path = f"/dev/{name}"
    d = Drive(path=path, name=name, vendor=_read(f"{base}/device/vendor"), model=_read(f"{base}/device/model"),
              serial=_serial(name, path), size=sectors * 512, transport=_transport(name),
              rotational=_read(f"{base}/queue/rotational") != "0", removable=_read(f"{base}/removable") == "1")
    for pn in sorted(x for x in os.listdir(base) if x.startswith(name) and os.path.isdir(f"{base}/{x}")):
        ppath = f"/dev/{pn}"
        devnum = _read(f"{base}/{pn}/dev")
        ms = mounts.get(devnum, [])
        fst = ms[0][1] if ms else _blkid_type(ppath)
        part = Partition(name=pn, path=ppath, devnum=devnum, start=int(_read(f"{base}/{pn}/start") or 0) * 512,
                         size=int(_read(f"{base}/{pn}/size") or 0) * 512, fstype=fst,
                         mounts=[(m, w) for m, _t, w in ms], swap=os.path.realpath(ppath) in swaps)
        if part.swap:
            part.fstype = "swap"
        try:
            d.holders += os.listdir(f"{base}/{pn}/holders")
        except OSError:
            pass
        d.partitions.append(part)
    try:
        d.holders += os.listdir(f"{base}/holders")
    except OSError:
        pass
    d.is_system = any(m in _SYSTEM_MOUNTS for p in d.partitions for m, _w in p.mounts) or \
        any(p.swap for p in d.partitions)
    _judge(d)
    return d


def _judge(d: Drive) -> None:
    """Fill eligible / notes / modes."""
    if d.rotational:
        if d.transport == "usb":
            d.notes.append("reports ROTATIONAL; many USB enclosures lie. If this is an SSD, pass --assume-ssd")
        else:
            d.eligible = False
            d.notes.append("rotational disk: nothing for nandwash to refresh")
    if d.holders:
        d.eligible = False
        d.notes.append("used by device-mapper/md/LVM layers: " + ", ".join(sorted(set(d.holders))))
    if d.is_system:
        d.notes.append("SYSTEM disk (holds / or boot/swap)")
    if d.transport == "usb":
        d.notes.append("USB: serial may belong to the bridge, not the drive; check it before --expect-serial")
    mounted = [p for p in d.partitions if p.mounted]
    rw = [p for p in mounted if p.writable]
    if not d.eligible:
        d.modes = "none"
    elif not mounted:
        d.modes = "scan, refresh (whole disk), refresh-chunks"
    elif not rw:
        d.modes = "scan, refresh-chunks (--allow-readonly-mounts)"
    elif all(p.fstype in SUPPORTED_FS for p in rw):
        d.modes = "scan, refresh-free (live; free space only)"
    else:
        d.modes = "scan only while mounted read-write (unmount for rewrites)"
        bad = sorted({p.fstype or "unknown" for p in rw if p.fstype not in SUPPORTED_FS})
        if bad:
            d.notes.append("live free-space mode needs ext4/xfs; found " + ", ".join(bad))


def enumerate_drives(include_ineligible: bool = True) -> List[Drive]:
    mounts, swaps = _mounts_by_devnum(), _swap_devices()
    out = []
    for name in sorted(os.listdir("/sys/block")):
        if _SKIP.match(name):
            continue
        d = describe(name, mounts, swaps)
        if d and (include_ineligible or d.eligible):
            out.append(d)
    return out


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return str(n)
