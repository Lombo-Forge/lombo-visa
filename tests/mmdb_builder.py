"""Builds a tiny MMDB file in memory for tests, so no real database is ever needed or committed.

build({"203.0.113.0/24": "DE", "2001:db8::/32": "FR"}) returns the bytes of a country database
with those networks. record_size may be 24, 28 or 32; ip_version 4 or 6.
"""

import ipaddress
import struct

MARKER = b"\xab\xcd\xefMaxMind.com"


def _ctrl(kind: int, size: int) -> bytes:
    assert size < 29
    if kind <= 7:
        return bytes([(kind << 5) | size])
    return bytes([size, kind - 7])


def text(s: str) -> bytes:
    raw = s.encode()
    return _ctrl(2, len(raw)) + raw


def uint(kind: int, n: int) -> bytes:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return _ctrl(kind, len(raw)) + raw


def mapping(pairs: list[tuple[str, bytes]]) -> bytes:
    return _ctrl(7, len(pairs)) + b"".join(text(k) + v for k, v in pairs)


def country_record(code: str) -> bytes:
    return mapping([("country", mapping([("iso_code", text(code))]))])


def _pack(left: int, right: int, record_size: int) -> bytes:
    if record_size == 24:
        return left.to_bytes(3, "big") + right.to_bytes(3, "big")
    if record_size == 28:
        mid = ((left >> 24) << 4) | (right >> 24)
        return (left & 0xFFFFFF).to_bytes(3, "big") + bytes([mid]) + (right & 0xFFFFFF).to_bytes(3, "big")
    return struct.pack(">II", left, right)


def build(
    networks: dict[str, str],
    ip_version: int = 6,
    record_size: int = 24,
    database_type: str = "DBIP-Country-Lite",
    records: dict[str, bytes] | None = None,
) -> bytes:
    nodes: list[list[int | None]] = [[None, None]]  # None = empty; ("data", n) kept in side table
    leaves: dict[tuple[int, int], int] = {}  # (node, bit) -> data offset
    data = b""
    offsets: dict[str, int] = {}
    for cidr, code in networks.items():
        net = ipaddress.ip_network(cidr)
        if code not in offsets:
            offsets[code] = len(data)
            data += (records or {}).get(code) or country_record(code)
        bits = [int(b) for b in format(int(net.network_address), f"0{net.max_prefixlen}b")][: net.prefixlen]
        if net.version == 4 and ip_version == 6:
            bits = [0] * 96 + bits
        node = 0
        for bit in bits[:-1]:
            child = nodes[node][bit]
            if child is None:
                nodes.append([None, None])
                child = len(nodes) - 1
                nodes[node][bit] = child
            node = child
        leaves[(node, bits[-1])] = offsets[code]
        nodes[node][bits[-1]] = -1  # marks a leaf
    count = len(nodes)
    tree = b""
    for n, (left, right) in enumerate(nodes):
        values = []
        for bit, child in enumerate((left, right)):
            if child is None:
                values.append(count)
            elif child == -1:
                values.append(count + 16 + leaves[(n, bit)])
            else:
                values.append(child)
        tree += _pack(values[0], values[1], record_size)
    meta = mapping(
        [
            ("node_count", uint(6, count)),
            ("record_size", uint(5, record_size)),
            ("ip_version", uint(5, ip_version)),
            ("database_type", text(database_type)),
            ("build_epoch", uint(9, 1_700_000_000)),
        ]
    )
    return tree + b"\x00" * 16 + data + MARKER + meta
