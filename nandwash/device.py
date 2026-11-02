"""Block-device info and O_DIRECT aligned I/O."""
from __future__ import annotations

import ctypes
import os
from dataclasses import dataclass

O_DIRECT = getattr(os, "O_DIRECT", 0x4000)
ALIGNMENT = 4096


class DeviceError(Exception):
    pass


def aligned_buffer(size: int, alignment: int = ALIGNMENT):
    """Return a ctypes char array of `size` bytes whose address is aligned."""
    raw = ctypes.create_string_buffer(size + alignment)
    offset = (-ctypes.addressof(raw)) % alignment
    buf = (ctypes.c_char * size).from_buffer(raw, offset)
    buf._raw = raw  # keep the backing allocation alive
    return buf


def _sys(name: str, rel: str) -> str:
    try:
        with open(f"/sys/block/{name}/{rel}") as f:
            return f.read().strip()
    except OSError:
        return ""


@dataclass
class DeviceInfo:
    path: str
    name: str
    model: str
    serial: str
    total_bytes: int
    physical_block_size: int
    rotational: bool

    @classmethod
    def probe(cls, path: str) -> "DeviceInfo":
        real = os.path.realpath(path)
        name = os.path.basename(real)
        if not os.path.isdir(f"/sys/block/{name}"):
            raise DeviceError(f"{path} is not a whole block device (partitions are not supported)")
        sectors = int(_sys(name, "size") or 0)  # always 512-byte sectors
        if sectors <= 0:
            raise DeviceError(f"cannot determine size of {path}")
        phys = int(_sys(name, "queue/physical_block_size") or 4096)
        rot = _sys(name, "queue/rotational") != "0"
        model = _sys(name, "device/model") or "unknown"
        serial = _sys(name, "device/serial") or _sys(name, "serial")
        if not serial:
            serial = _udev_serial(real) or name
        return cls(path=real, name=name, model=model, serial=serial,
                   total_bytes=sectors * 512, physical_block_size=max(phys, 512),
                   rotational=rot)


def _udev_serial(path: str) -> str:
    try:
        import subprocess
        out = subprocess.run(["udevadm", "info", "--query=property", f"--name={path}"],
                             capture_output=True, text=True, timeout=5).stdout
        for line in out.splitlines():
            if line.startswith("ID_SERIAL_SHORT="):
                return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


class DirectDevice:
    """O_DIRECT reader/writer. Offsets and lengths must be block aligned."""

    def __init__(self, path: str, writable: bool = False):
        self.path = path
        flags = (os.O_RDWR if writable else os.O_RDONLY) | O_DIRECT
        self.fd = os.open(path, flags)
        self._bufs: dict[int, ctypes.Array] = {}

    def buffer(self, size: int):
        buf = self._bufs.get(size)
        if buf is None:
            buf = self._bufs[size] = aligned_buffer(size)
        return buf

    def read(self, offset: int, size: int):
        """Read `size` bytes at `offset` into a reusable aligned buffer.
        Raises OSError on I/O error or short read. Buffer is valid until the next read of same size."""
        buf = self.buffer(size)
        n = os.preadv(self.fd, [buf], offset)
        if n != size:
            raise OSError(5, f"short read ({n}/{size}) at offset {offset}")
        return buf

    def write(self, offset: int, buf) -> None:
        size = len(buf)
        n = os.pwritev(self.fd, [buf], offset)
        if n != size:
            raise OSError(5, f"short write ({n}/{size}) at offset {offset}")

    def sync(self) -> None:
        try:
            os.fsync(self.fd)
        except OSError:
            pass

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
