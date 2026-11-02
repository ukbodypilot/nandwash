"""Core scan/refresh loop."""
from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Iterator, Optional

from .device import DeviceInfo, DirectDevice
from .smart import SmartSnapshot, poll_smart

SMART_INTERVAL = 30.0
THROTTLE_SLEEP = 30
BLOCK = 4096
RETRIES = 3


@dataclass
class EngineState:
    mode: str
    device_path: str
    total_bytes: int
    current_offset: int = 0
    good_bytes: int = 0
    error_sectors: list = field(default_factory=list)
    retry_count: int = 0
    written_bytes: int = 0
    smart: Optional[SmartSnapshot] = None
    start_time: float = field(default_factory=time.time)
    log_lines: list = field(default_factory=list)
    # extras used by the UI / CLI
    smart_before: Optional[SmartSnapshot] = None
    smart_after: Optional[SmartSnapshot] = None
    start_offset: int = 0
    throttled: bool = False
    finished: bool = False


def _ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def run(device_path: str, mode: str, start_offset: int = 0, chunk_size: int = 1048576,
        max_temp: int = 70, resume: Optional[dict] = None,
        info: Optional[DeviceInfo] = None) -> Iterator[EngineState]:
    """Scan or refresh a device, yielding an EngineState after every chunk.

    `resume` may carry error_sectors/good_bytes/written_bytes/retry_count/smart_before
    from a previous interrupted run.
    """
    if mode not in ("scan", "refresh"):
        raise ValueError("mode must be 'scan' or 'refresh'")
    info = info or DeviceInfo.probe(device_path)
    total = info.total_bytes
    block = max(BLOCK, info.physical_block_size)
    chunk_size = max(block, chunk_size - chunk_size % block)
    start_offset -= start_offset % block

    log: deque = deque(maxlen=10)
    st = EngineState(mode=mode, device_path=info.path, total_bytes=total,
                     current_offset=start_offset, start_offset=start_offset)
    if resume:
        st.error_sectors = list(resume.get("error_sectors", []))
        st.good_bytes = resume.get("good_bytes", 0)
        st.written_bytes = resume.get("written_bytes", 0)
        st.retry_count = resume.get("retry_count", 0)

    def emit(msg: str) -> None:
        log.append(f"{_ts()} {msg}")
        st.log_lines = list(log)

    st.smart_before = SmartSnapshot.from_dict(resume.get("smart_before")) if resume and resume.get("smart_before") else poll_smart(info.path)
    st.smart = st.smart_before
    last_smart = time.time()
    emit(f"{mode} started at offset {start_offset}" + (" (resumed)" if resume else ""))
    yield st

    with DirectDevice(info.path, writable=(mode == "refresh")) as dev:
        offset = start_offset
        while offset < total:
            # --- SMART poll + temperature throttle -------------------------
            if time.time() - last_smart >= SMART_INTERVAL:
                snap = poll_smart(info.path)
                last_smart = time.time()
                if snap:
                    st.smart = snap
            while st.smart and st.smart.temperature is not None and st.smart.temperature > max_temp:
                st.throttled = True
                emit(f"temp {st.smart.temperature}C > {max_temp}C, pausing {THROTTLE_SLEEP}s")
                yield st
                for _ in range(THROTTLE_SLEEP):
                    time.sleep(1)
                    yield st
                snap = poll_smart(info.path)
                last_smart = time.time()
                if snap:
                    st.smart = snap
                else:
                    break
            st.throttled = False

            size = min(chunk_size, total - offset)
            try:
                buf = dev.read(offset, size)
            except OSError:
                buf = None
            if buf is not None:
                st.good_bytes += size
                if mode == "refresh":
                    # Only ever reached with a cleanly-read buffer.
                    try:
                        dev.write(offset, buf)
                    except OSError as e:
                        st.current_offset = offset
                        emit(f"WRITE error at {offset}: {e.strerror or e}; aborting")
                        yield st
                        raise
                    st.written_bytes += size
            else:
                emit(f"read error in chunk @ {offset}, isolating at {block}-byte granularity")
                pos, end = offset, offset + size
                while pos < end:
                    n = min(block, end - pos)
                    ok = False
                    for _ in range(RETRIES):
                        try:
                            dev.read(pos, n)
                            ok = True
                            break
                        except OSError:
                            st.retry_count += 1
                    if ok:
                        st.good_bytes += n
                    else:
                        st.error_sectors.append(pos)
                        emit(f"BAD block at byte offset {pos}")
                    pos += n
                if mode == "refresh":
                    emit("chunk had read errors: not written back")
            offset += size
            st.current_offset = offset
            yield st

        if mode == "refresh":
            dev.sync()

    st.smart_after = poll_smart(info.path) or st.smart
    st.smart = st.smart_after
    st.finished = True
    emit("complete")
    yield st
