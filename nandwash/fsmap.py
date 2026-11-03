"""Ask a MOUNTED filesystem which byte ranges of its device are free (FS_IOC_GETFSMAP), and freeze/thaw it.

Why the kernel and not the on-disk bitmaps: on a live ext4 the committed-but-not-yet-checkpointed journal
means the on-disk bitmaps lag the truth. GETFSMAP answers from the kernel's own view. Even that view is
only a snapshot: to *write* into "free" space safely the filesystem must be frozen (no allocations can
happen) between the query and the write. `Frozen` does that with a hard auto-thaw timer.

Supported by ext4 (Linux >= 5.10) and XFS. Offsets are relative to the device the filesystem lives on
(i.e. the PARTITION, not the whole disk).
"""
from __future__ import annotations

import fcntl
import os
import struct
import threading
import time
from typing import List, Tuple

_REC = struct.Struct("=IIQQqQ24s")   # device, flags, physical, owner, offset, length, reserved[3]
_HEAD = struct.Struct("=IIII48s")    # iflags, oflags, count, entries, reserved[6]


def _iowr(typ: str, nr: int, size: int) -> int:
    return (3 << 30) | (size << 16) | (ord(typ) << 8) | nr


FS_IOC_GETFSMAP = _iowr("X", 59, _HEAD.size + 2 * _REC.size)
FIFREEZE = _iowr("X", 119, 4)
FITHAW = _iowr("X", 120, 4)

FMR_OF_LAST = 0x4
FMR_OWN_FREE = 1          # FMR_OWNER(0, 1)
SUPPORTED_FS = ("ext4", "ext3", "ext2", "xfs")

_U32, _U64 = 0xFFFFFFFF, 0xFFFFFFFFFFFFFFFF


def encode_dev(major: int, minor: int) -> int:
    """The kernel's new_encode_dev(), which is what fmr_device carries."""
    return (minor & 0xFF) | (major << 8) | ((minor & ~0xFF) << 12)


def fsmap(mountpoint: str, batch: int = 4096) -> List[tuple]:
    """Every mapping record of the filesystem mounted at `mountpoint`:
    (device, flags, physical, owner, offset, length)."""
    fd = os.open(mountpoint, os.O_RDONLY | os.O_DIRECTORY)
    try:
        lo = _REC.pack(0, 0, 0, 0, 0, 0, b"\0" * 24)
        hi = _REC.pack(_U32, _U32, _U64, _U64, -1, _U64, b"\0" * 24)
        out: List[tuple] = []
        while True:
            buf = bytearray(_HEAD.size + 2 * _REC.size + batch * _REC.size)
            buf[:_HEAD.size] = _HEAD.pack(0, 0, batch, 0, b"\0" * 48)
            buf[_HEAD.size:_HEAD.size + _REC.size] = lo
            buf[_HEAD.size + _REC.size:_HEAD.size + 2 * _REC.size] = hi
            fcntl.ioctl(fd, FS_IOC_GETFSMAP, buf, True)
            entries = _HEAD.unpack_from(buf, 0)[3]
            recs = [_REC.unpack_from(buf, _HEAD.size + 2 * _REC.size + i * _REC.size)[:6] for i in range(entries)]
            out += recs
            if not recs or recs[-1][1] & FMR_OF_LAST:
                return out
            d, _, phys, own, off, ln = recs[-1]
            lo = _REC.pack(d, 0, phys, own, off, ln, b"\0" * 24)   # continue after the last record returned
    finally:
        os.close(fd)


def free_extents(mountpoint: str, device_dev: int) -> List[Tuple[int, int]]:
    """Merged free byte ranges [(start, length)] on the filesystem's own data device."""
    runs = sorted((p, ln) for d, _f, p, own, _o, ln in fsmap(mountpoint) if d == device_dev and own == FMR_OWN_FREE)
    merged: List[List[int]] = []
    for p, ln in runs:
        if merged and p <= merged[-1][0] + merged[-1][1]:
            end = max(merged[-1][0] + merged[-1][1], p + ln)
            merged[-1][1] = end - merged[-1][0]
        else:
            merged.append([p, ln])
    return [(p, ln) for p, ln in merged]


def is_free(extents: List[Tuple[int, int]], start: int, length: int) -> bool:
    """True if [start, start+length) lies wholly inside ONE free extent (extents sorted, merged)."""
    lo, hi = 0, len(extents)
    while lo < hi:
        mid = (lo + hi) // 2
        if extents[mid][0] + extents[mid][1] <= start:
            lo = mid + 1
        else:
            hi = mid
    return lo < len(extents) and extents[lo][0] <= start and start + length <= extents[lo][0] + extents[lo][1]


class Frozen:
    """Freeze the filesystem at `mountpoint` for the duration of a `with` block.

    Safety: a timer thread thaws it after `max_s` no matter what (a freeze that outlives its owner hangs
    every writer on the machine, and on a root filesystem that is the whole box). The thaw uses a file
    descriptor opened BEFORE the freeze. Keep the body tiny and do no filesystem WRITES inside it
    (this includes print() to a redirected file and logging)."""

    def __init__(self, mountpoint: str, max_s: float = 3.0):
        self.mountpoint, self.max_s = mountpoint, max_s
        self._fd = None
        self._lock = threading.Lock()
        self._frozen = False
        self._timer = None
        self.held_s = 0.0

    def _thaw(self) -> None:
        with self._lock:
            if self._frozen:
                try:
                    fcntl.ioctl(self._fd, FITHAW, 0)
                except OSError:
                    pass   # already thawed (EINVAL) or fd gone
                self._frozen = False

    def __enter__(self):
        self._fd = os.open(self.mountpoint, os.O_RDONLY | os.O_DIRECTORY)
        try:
            self._timer = threading.Timer(self.max_s, self._thaw)
            self._timer.daemon = True
            fcntl.ioctl(self._fd, FIFREEZE, 0)
            self._t0 = time.monotonic()
            with self._lock:
                self._frozen = True
            self._timer.start()
        except BaseException:
            os.close(self._fd)
            raise
        return self

    def __exit__(self, *exc):
        self._timer.cancel()
        self._thaw()
        self.held_s = time.monotonic() - self._t0
        os.close(self._fd)
        return False

    @property
    def still_frozen(self) -> bool:
        return self._frozen

    def remaining(self) -> float:
        """Seconds until the auto-thaw fires (0 once thawed). Check this BEFORE any write."""
        return max(0.0, self.max_s - (time.monotonic() - self._t0)) if self._frozen else 0.0
