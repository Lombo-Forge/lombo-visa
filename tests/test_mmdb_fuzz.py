"""Feed the reader thousands of damaged databases. Whatever the bytes, the only exception allowed out is MmdbError
(or GeoIpError from the download check), and no case may run for more than a few seconds.

Seeded, so a failure repeats. A few seconds in all; marked slow only so it can be left out with `-m "not slow"`."""

import contextlib
import random
import signal
import time

import pytest

import mmdb_builder as mb
from lombo_visa import geoip, mmdb

IPS = ["1.1.1.1", "8.8.8.8", "2001:4860:4860::8888", "10.0.0.1", "::ffff:1.2.3.4", "255.255.255.255", "::", "0.0.0.0"]  # noqa: S104 (an address to look up, not a bind)
SEEDS = [
    mb.build({"1.0.0.0/8": "AU", "8.8.8.0/24": "US", "2001:4860::/32": "US"}, ip_version, record_size)
    for record_size in (24, 28, 32)
    for ip_version in (4, 6)
] + [mb.build({"1.0.0.0/8": "AU"}, 6, 24)]


def mutate(rng: random.Random, data: bytes) -> bytes:
    b = bytearray(data)
    kind = rng.choice(["flip", "byte", "trunc", "ins", "rand", "marker", "dup"])
    if kind == "flip":
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(len(b))] ^= 1 << rng.randrange(8)
    elif kind == "byte":
        for _ in range(rng.randint(1, 8)):
            b[rng.randrange(len(b))] = rng.choice([0, 0xFF, 0x1F, 0xE0, 0x5F, 0x7F, 0x20, rng.randrange(256)])
    elif kind == "trunc":
        b = b[: rng.randrange(len(b))]
    elif kind == "ins":
        i = rng.randrange(len(b))
        b[i:i] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 40)))
    elif kind == "rand":
        b = bytearray(rng.randbytes(rng.randint(0, 300)) + mb.MARKER + rng.randbytes(rng.randint(0, 200)))
    elif kind == "marker":
        tail = bytearray(rng.randbytes(rng.randint(0, 60)))
        b = b[: b.rfind(mb.MARKER) + len(mb.MARKER)] + tail
    else:
        b = b + b
    return bytes(b)


class TooSlow(Exception):
    pass


def _alarm(*_):
    raise TooSlow


@pytest.mark.slow
@pytest.mark.parametrize("seed", [1, 2, 3])
def test_damaged_databases_only_ever_raise_mmdb_error(seed):
    rng = random.Random(seed)
    old = signal.signal(signal.SIGALRM, _alarm)
    started = time.monotonic()
    opened = 0
    try:
        for _ in range(2000):
            data = mutate(rng, rng.choice(SEEDS))
            signal.alarm(5)  # one case may not take more than 5 seconds
            try:
                reader = mmdb.Reader(data)
                for ip in IPS:
                    with contextlib.suppress(mmdb.MmdbError):
                        reader.get(ip)
                with contextlib.suppress(mmdb.MmdbError):
                    reader.records(limit=2000)
                opened += 1
            except mmdb.MmdbError:
                pass
            try:
                geoip.check_database(data, require_size=False)
            except geoip.GeoIpError:
                pass
            finally:
                signal.alarm(0)
    finally:
        signal.signal(signal.SIGALRM, old)
    assert opened > 100  # the mutations are not all fatal, so the lookups really ran
    assert time.monotonic() - started < 60
