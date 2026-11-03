"""Find the drives attached to this machine (SATA, NVMe, USB, ...) and say what nandwash may do with each.

Pure sysfs/procfs; `blkid` is used only to name the filesystem on UNMOUNTED partitions, if present.
"""
from __future__ import annotations

import hashlib
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
class Capability:
    """One thing nandwash may do with a drive. `ok` is True/False, or None = not checked here (see reasons)."""
    ok: Optional[bool]
    reasons: List[str] = field(default_factory=list)


# check          scan + re-read (read-only)
# live           rewrite FREE-space chunks while mounted (ext4/xfs, GETFSMAP + freeze)
# offline-chunks rewrite listed chunks because nothing has the drive mounted read-write
# full           whole-surface rewrite (nothing mounted at all)
# offline-boot   rewrite in-use chunks in the maintenance boot (decided by `preboot check`)
CAPS = ("check", "live", "offline-chunks", "full", "offline-boot")
WRITE_CAPS = CAPS[1:]


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
    thunderbolt: bool = False   # reached through a Thunderbolt PCIe tunnel (dock/enclosure): hot-unpluggable
    notes: List[str] = field(default_factory=list)
    eligible: bool = True
    modes: str = ""
    wwid: str = ""
    ptuuid: str = ""            # partition-table (GPT disk / MBR) id: tie-breaker for same-serial drives
    by_path: str = ""           # physical port (udev ID_PATH): current location, never identity
    id: str = ""                # short ID (A1): nw-xxxxxx
    id_extra: str = ""          # tie-breaker folded into the short ID when two drives share an identity
    ambiguous: bool = False     # identical identity to another attached drive, told apart only by port
    caps: dict = field(default_factory=dict)    # name -> Capability

    def to_dict(self) -> dict:
        return asdict(self)

    def can(self, cap: str) -> bool:
        c = self.caps.get(cap)
        return bool(c and c.ok)

    @property
    def identity(self) -> tuple:
        return identity(self.wwid, self.serial, self.model, self.size)

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


def _thunderbolt_bridges() -> set:
    """PCI addresses of the bridges that carry Thunderbolt PCIe tunnels (the NHI's ancestors below the root
    port). Any drive whose PCI path runs through one of them sits behind a Thunderbolt dock/enclosure."""
    out: set = set()
    base = "/sys/bus/thunderbolt/devices"
    try:
        doms = [x for x in os.listdir(base) if x.startswith("domain")]
    except OSError:
        return out
    for dom in doms:
        parts = os.path.realpath(f"{base}/{dom}").split("/")
        pci = [x for x in parts if re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.\d", x)]
        out.update(pci[1:-1])       # drop the root port (first) and the NHI itself (last)
    return out


def _behind_thunderbolt(name: str) -> bool:
    bridges = _thunderbolt_bridges()
    if not bridges:
        return False
    return any(x in bridges for x in os.path.realpath(f"/sys/block/{name}").split("/"))


def _serial(name: str, path: str) -> str:
    from .device import read_serial
    return read_serial(name, path)


def identity(wwid: str, serial: str, model: str, size: int) -> tuple:
    """What the drive itself reports. Survives reboots, port moves and reformatting; /dev names and fs labels don't."""
    return (wwid or serial, model, int(size))


def short_id(ident: tuple, length: int = 6) -> str:
    h = hashlib.sha256("|".join(str(x) for x in ident).encode()).hexdigest()
    return "nw-" + h[:length]


def assign_ids(ds: List[Drive]) -> None:
    """Give each drive its short ID, lengthened until the IDs of the drives present are unique. Drives whose identity
    is identical (cheap USB bridges reporting one serial) are told apart by partition-table id; if that fails too,
    they are marked ambiguous: reads stay allowed, writes are refused (swapping two enclosures would swap IDs)."""
    groups: dict = {}
    for d in ds:
        groups.setdefault(d.identity, []).append(d)
    for ident, g in groups.items():
        if len(g) == 1:
            continue
        pts = [d.ptuuid for d in g]
        for d in g:
            if d.ptuuid and pts.count(d.ptuuid) == 1:
                d.id_extra = d.ptuuid
            else:
                d.ambiguous = True
                d.id_extra = d.by_path or d.name
    length = 6
    while True:
        ids = [short_id(d.identity + ((d.id_extra,) if d.id_extra else ()), length) for d in ds]
        if len(set(ids)) == len(ids) or length >= 64:
            break
        length += 2
    for d, i in zip(ds, ids):
        d.id = i
        if d.ambiguous:
            _deny_writes(d, "ambiguous identity: another attached drive reports the same serial, model and size, "
                            "and only the port tells them apart")


def _udev(devnum: str) -> dict:
    out = {}
    try:
        with open(f"/run/udev/data/b{devnum}") as f:
            for line in f:
                if line.startswith("E:") and "=" in line:
                    k, v = line[2:].rstrip("\n").split("=", 1)
                    out[k] = v
    except OSError:
        pass
    return out


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
    d.thunderbolt = _behind_thunderbolt(name)
    d.wwid = _read(f"{base}/device/wwid") or _read(f"{base}/wwid")
    u = _udev(_read(f"{base}/dev"))
    d.ptuuid, d.by_path = u.get("ID_PART_TABLE_UUID", ""), u.get("ID_PATH", "")
    d.is_system = any(m in _SYSTEM_MOUNTS for p in d.partitions for m, _w in p.mounts) or \
        any(p.swap for p in d.partitions)
    _judge(d)
    d.id = short_id(d.identity)
    return d


def _deny_writes(d: Drive, why: str) -> None:
    for k in WRITE_CAPS:
        c = d.caps.setdefault(k, Capability(False))
        c.ok = False
        c.reasons.insert(0, why)
    d.modes = _modes(d)


def _modes(d: Drive) -> str:
    names = {"check": "scan", "live": "refresh-free (live; free space only)", "offline-chunks": "refresh-chunks",
             "full": "refresh (whole disk)", "offline-boot": "offline boot"}
    ok = [names[k] for k in CAPS if d.can(k)]
    return ", ".join(ok) if ok else "none"


def judge_caps(d: Drive) -> dict:
    """Capability per CAPS name, each with reasons for yes AND no. Pure: works on the Drive fields only."""
    caps = {k: Capability(False) for k in CAPS}
    blockers = []
    if d.rotational and d.transport != "usb":
        blockers.append("rotational disk: nothing for nandwash to refresh")
    if d.holders:
        blockers.append("used by device-mapper/md/LVM layers: " + ", ".join(sorted(set(d.holders))))
    if blockers:
        for c in caps.values():
            c.reasons += blockers
        return caps
    caps["check"] = Capability(True, ["read-only; safe while mounted"] +
                               (["reports ROTATIONAL (USB enclosures often lie); scan to see"] if d.rotational else []))
    mounted = [p for p in d.partitions if p.mounted]
    rw = [p for p in mounted if p.writable]
    swap = [p for p in d.partitions if p.swap]
    # live: free space of each rw-mounted ext4/xfs partition; anything else on the disk is refused per chunk
    live_ok = [p for p in rw if p.fstype in SUPPORTED_FS]
    live_no = [p for p in rw if p.fstype not in SUPPORTED_FS]
    if live_ok:
        caps["live"] = Capability(True, [f"{p.name} ({p.fstype}, {_mount_label(p)}): free space" for p in live_ok] +
                                  [f"{p.name} ({p.fstype or 'unknown'}) skipped: no free-space map" for p in live_no])
    elif rw:
        caps["live"].reasons.append("live mode needs ext4/xfs; mounted rw: " +
                                    ", ".join(f"{p.name} ({p.fstype or 'unknown'})" for p in rw))
    else:
        caps["live"].reasons.append("nothing mounted read-write: use offline-chunks instead")
    # offline-chunks: nothing writes the disk behind our back
    if rw or swap:
        caps["offline-chunks"].reasons.append("mounted read-write: " +
                                              ", ".join(_mount_label(p) for p in rw + swap))
    else:
        caps["offline-chunks"] = Capability(True, ["read-only mounts: needs --allow-readonly-mounts"] if mounted
                                            else ["not mounted"])
    # full: nothing mounted at all
    if mounted or swap:
        caps["full"].reasons.append("whole-disk rewrite needs every partition unmounted")
    else:
        caps["full"] = Capability(True, ["not mounted"])
    # offline-boot: only relevant when in-use data sits on a rw-mounted disk; the real verdict is preboot check
    if not rw:
        caps["offline-boot"].reasons.append("not needed: nothing is mounted read-write")
    elif d.transport in ("usb",) or d.thunderbolt:
        caps["offline-boot"].reasons.append("the maintenance boot only targets internal drives")
    else:
        caps["offline-boot"] = Capability(None, ["run `nandwash preboot check` for the verdict"])
    return caps


def _mount_label(p: Partition) -> str:
    if p.swap:
        return f"{p.name} (swap)"
    return min((m for m, _w in p.mounts), key=len) if p.mounts else p.name    # shortest: not a container view


def _judge(d: Drive) -> None:
    """Fill caps / eligible / notes / modes."""
    d.caps = judge_caps(d)
    d.eligible = d.can("check")
    if not d.eligible:
        d.notes += d.caps["check"].reasons
    if d.rotational and d.transport == "usb":
        d.notes.append("reports ROTATIONAL; many USB enclosures lie. If this is an SSD, pass --assume-ssd")
    if d.is_system:
        d.notes.append("SYSTEM disk (holds / or boot/swap)")
    if d.thunderbolt:
        d.notes.append("THUNDERBOLT dock/enclosure: hot-unpluggable. Keep the dock powered and the cable seated "
                       "during rewrites (a drive that vanishes mid-write can leave a chunk half-written)")
    if d.transport == "usb":
        d.notes.append("USB: serial may belong to the bridge, not the drive; check it before --expect-serial")
    d.modes = _modes(d)


def enumerate_drives(include_ineligible: bool = True) -> List[Drive]:
    mounts, swaps = _mounts_by_devnum(), _swap_devices()
    out = []
    for name in sorted(os.listdir("/sys/block")):
        if _SKIP.match(name):
            continue
        d = describe(name, mounts, swaps)
        if d:
            out.append(d)
    assign_ids(out)
    return out if include_ineligible else [d for d in out if d.eligible]


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1000 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1000.0
    return str(n)
