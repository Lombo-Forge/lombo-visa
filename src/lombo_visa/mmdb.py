"""A minimal reader for MaxMind DB (.mmdb) files, written for the DB-IP "IP to Country Lite" database.

Standard library only. It reads the whole file into memory (the country file is a few megabytes) and
answers one question: what record belongs to this address? The file is treated as untrusted input
(it is downloaded), so every read is bounds-checked, pointers may not chain, nesting is limited and
one lookup may decode only a fixed number of values and text bytes (bytes values are skipped, never
copied). Any problem raises MmdbError; callers turn that into "unknown", which fails open.

Format reference: https://maxmind.github.io/MaxMind-DB/
"""

import ipaddress
import struct
from typing import Any

MARKER = b"\xab\xcd\xefMaxMind.com"
MAX_FILE_BYTES = 64 * 1024 * 1024  # the real country file is a few megabytes
MAX_DEPTH = 16
MAX_VALUES = 10_000  # values decoded per lookup or per metadata read
MAX_TEXT_BYTES = 128 * 1024  # text returned per lookup or per metadata read, pointers followed again included
RECORD_SIZES = (24, 28, 32)

_POINTER, _STRING, _DOUBLE, _BYTES, _UINT16, _UINT32, _MAP = 1, 2, 3, 4, 5, 6, 7
_INT32, _UINT64, _UINT128, _ARRAY, _CONTAINER, _END, _BOOL, _FLOAT = 8, 9, 10, 11, 12, 13, 14, 15


class MmdbError(Exception):
    """The file is not a usable MMDB, or a read went outside it."""


class _Budget:
    def __init__(self) -> None:
        self.left = MAX_VALUES
        self.text_left = MAX_TEXT_BYTES

    def spend(self) -> None:
        self.left -= 1
        if self.left < 0:
            raise MmdbError("record is too large")

    def spend_text(self, n: int) -> None:
        self.text_left -= n
        if self.text_left < 0:
            raise MmdbError("record is too large")


def _take(buf: bytes, offset: int, n: int) -> bytes:
    if n < 0 or offset < 0 or offset + n > len(buf):
        raise MmdbError("read outside the file")
    return buf[offset : offset + n]


def decode_value(buf: bytes, base: int, offset: int) -> tuple[Any, int]:
    """Decode one value at `offset`. Pointers are relative to `base`. Returns (value, next offset)."""
    return _decode(buf, base, offset, 0, _Budget(), True)


def _decode(buf: bytes, base: int, offset: int, depth: int, budget: _Budget, allow_pointer: bool) -> tuple[Any, int]:
    if depth > MAX_DEPTH:
        raise MmdbError("nesting is too deep")
    budget.spend()
    ctrl = _take(buf, offset, 1)[0]
    offset += 1
    kind = ctrl >> 5
    if kind == 0:  # extended type: the real type is in the next byte
        kind = 7 + _take(buf, offset, 1)[0]
        offset += 1
    if kind == _POINTER:
        if not allow_pointer:
            raise MmdbError("pointer to a pointer")
        size = (ctrl >> 3) & 3
        raw = int.from_bytes(_take(buf, offset, size + 1 if size < 3 else 4), "big")
        offset += size + 1 if size < 3 else 4
        if size == 0:
            raw |= (ctrl & 7) << 8
        elif size == 1:
            raw = ((ctrl & 7) << 16 | raw) + 2048
        elif size == 2:
            raw = ((ctrl & 7) << 24 | raw) + 526336
        value, _ = _decode(buf, base, base + raw, depth + 1, budget, False)
        return value, offset
    size = ctrl & 31
    if size == 29:
        size = 29 + _take(buf, offset, 1)[0]
        offset += 1
    elif size == 30:
        size = 285 + int.from_bytes(_take(buf, offset, 2), "big")
        offset += 2
    elif size == 31:
        size = 65821 + int.from_bytes(_take(buf, offset, 3), "big")
        offset += 3
    return _decode_body(buf, base, offset, kind, size, depth, budget)


def _decode_body(
    buf: bytes, base: int, offset: int, kind: int, size: int, depth: int, budget: _Budget
) -> tuple[Any, int]:
    if kind == _MAP:
        out: dict[str, Any] = {}
        for _ in range(size):
            key, offset = _decode(buf, base, offset, depth + 1, budget, True)
            if not isinstance(key, str):
                raise MmdbError("map key is not text")
            out[key], offset = _decode(buf, base, offset, depth + 1, budget, True)
        return out, offset
    if kind == _ARRAY:
        items: list[Any] = []
        for _ in range(size):
            item, offset = _decode(buf, base, offset, depth + 1, budget, True)
            items.append(item)
        return items, offset
    if kind == _BOOL:
        return bool(size), offset  # the value lives in the size field; no payload
    if kind == _END:
        raise MmdbError("unexpected end marker")
    if kind == _BYTES:  # never returned: the reader needs maps, text and numbers only
        if offset + size > len(buf):
            raise MmdbError("read outside the file")
        return None, offset + size
    if kind == _STRING:
        budget.spend_text(size)
        raw = _take(buf, offset, size)
        try:
            return raw.decode("utf-8"), offset + size
        except UnicodeDecodeError as e:
            raise MmdbError("text is not UTF-8") from e
    if kind == _DOUBLE and size != 8:
        raise MmdbError("bad double")
    if kind == _FLOAT and size != 4:
        raise MmdbError("bad float")
    longest = {_UINT16: 2, _UINT32: 4, _UINT64: 8, _UINT128: 16, _INT32: 4}
    if kind in longest and size > longest[kind]:
        raise MmdbError("number is too long")
    if kind not in (_DOUBLE, _FLOAT, *longest):
        raise MmdbError(f"unknown type {kind}")
    raw = _take(buf, offset, size)
    offset += size
    if kind == _DOUBLE:
        return struct.unpack(">d", raw)[0], offset
    if kind == _FLOAT:
        return struct.unpack(">f", raw)[0], offset
    return int.from_bytes(raw, "big", signed=kind == _INT32 and size == 4), offset


class Reader:
    def __init__(self, data: bytes) -> None:
        if len(data) > MAX_FILE_BYTES:
            raise MmdbError("file is too large")
        marker_at = data.rfind(MARKER)
        if marker_at < 0:
            raise MmdbError("not an MMDB file (no metadata marker)")
        meta_start = marker_at + len(MARKER)
        meta, _ = _decode(data, meta_start, meta_start, 0, _Budget(), True)
        if not isinstance(meta, dict):
            raise MmdbError("metadata is not a map")
        try:
            self.node_count = int(meta["node_count"])
            self.record_size = int(meta["record_size"])
            self.ip_version = int(meta["ip_version"])
        except (KeyError, TypeError, ValueError, OverflowError) as e:
            raise MmdbError("metadata is incomplete") from e
        self.database_type = str(meta.get("database_type", ""))
        self.build_epoch = int(meta["build_epoch"]) if isinstance(meta.get("build_epoch"), int) else None
        if self.record_size not in RECORD_SIZES or self.ip_version not in (4, 6) or self.node_count < 1:
            raise MmdbError("metadata has unsupported values")
        self._node_bytes = self.record_size // 4
        self._data_start = self.node_count * self._node_bytes + 16
        if self._data_start > marker_at:
            raise MmdbError("search tree is larger than the file")
        self._buf = data
        self._data_end = marker_at

    def _record(self, node: int, bit: int) -> int:
        b = _take(self._buf, node * self._node_bytes, self._node_bytes)
        if self.record_size == 24:
            return int.from_bytes(b[3 * bit : 3 * bit + 3], "big")
        if self.record_size == 28:
            if bit == 0:
                return (b[3] & 0xF0) << 20 | int.from_bytes(b[0:3], "big")
            return (b[3] & 0x0F) << 24 | int.from_bytes(b[4:7], "big")
        return int.from_bytes(b[4 * bit : 4 * bit + 4], "big")

    def get(self, address: str) -> Any:
        """The record for this address, or None when the file has none. Raises MmdbError on a bad file."""
        try:
            ip = ipaddress.ip_address(address)
        except ValueError:
            return None
        if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
            ip = ip.ipv4_mapped
        if ip.version == 6 and self.ip_version == 4:
            return None
        bits = ip.packed
        steps = [(byte >> shift) & 1 for byte in bits for shift in range(7, -1, -1)]
        if ip.version == 4 and self.ip_version == 6:
            steps = [0] * 96 + steps  # IPv4 lives under ::/96 in an IPv6 tree
        node = 0
        for bit in steps:
            if node >= self.node_count:
                break
            node = self._record(node, bit)
        if node == self.node_count:
            return None
        if node < self.node_count:
            raise MmdbError("search tree ended inside a node")
        return self.record_at(node - self.node_count - 16)

    def record_at(self, offset: int) -> Any:
        if offset < 0 or self._data_start + offset >= self._data_end:
            raise MmdbError("data pointer outside the data section")
        value, _ = decode_value(self._buf, self._data_start, self._data_start + offset)
        return value

    def records(self, limit: int = 100_000) -> list[Any]:
        """Every distinct record the search tree points at (a country file has about 250). For `doctor` only."""
        seen: set[int] = set()
        offsets: set[int] = set()
        stack = [0]
        while stack:
            node = stack.pop()
            if node in seen:
                continue
            seen.add(node)
            for bit in (0, 1):
                nxt = self._record(node, bit)
                if nxt < self.node_count:
                    stack.append(nxt)
                elif nxt > self.node_count:
                    offsets.add(nxt - self.node_count - 16)
                    if len(offsets) > limit:
                        raise MmdbError("too many distinct records")
        return [self.record_at(o) for o in sorted(offsets)]
