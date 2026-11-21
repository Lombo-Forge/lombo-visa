import gzip
import http.server
import struct
import threading
import time

import pytest

import mmdb_builder as mb
from conftest import NETWORKS
from lombo_visa import geoip, mmdb
from lombo_visa.mmdb import MmdbError, Reader


# ---------------------------------------------------------------- the reader
@pytest.mark.parametrize("record_size", [24, 28, 32])
@pytest.mark.parametrize("ip_version", [4, 6])
def test_reader_finds_networks(record_size, ip_version):
    nets = {"203.0.113.0/24": "DE", "198.51.100.0/24": "US"}
    if ip_version == 6:
        nets["2001:db8::/32"] = "FR"
    r = Reader(mb.build(nets, ip_version=ip_version, record_size=record_size))
    assert r.get("203.0.113.77")["country"]["iso_code"] == "DE"
    assert r.get("198.51.100.1")["country"]["iso_code"] == "US"
    assert r.get("8.8.8.8") is None
    assert r.get("::ffff:203.0.113.5")["country"]["iso_code"] == "DE"  # IPv4 written the IPv6 way
    assert r.get("not an address") is None
    assert r.database_type == "DBIP-Country-Lite"
    assert r.build_epoch == 1_700_000_000
    if ip_version == 6:
        assert r.get("2001:db8:1::1")["country"]["iso_code"] == "FR"
        assert r.get("2001:db9::1") is None
    else:
        assert r.get("2001:db8::1") is None  # IPv6 against an IPv4-only tree


def test_reader_rejects_bad_files():
    good = mb.build(NETWORKS)
    for bad in (b"", b"hello", good[: len(good) // 2], b"\x00" * 100 + mb.MARKER):
        with pytest.raises(MmdbError):
            Reader(bad)
    # metadata with unsupported values
    odd = mb.build(NETWORKS, record_size=24).replace(mb.uint(5, 24), mb.uint(5, 20), 1)
    with pytest.raises(MmdbError, match="unsupported"):
        Reader(odd)
    # metadata missing a key
    meta = mb.mapping([("node_count", mb.uint(6, 1))])
    with pytest.raises(MmdbError, match="incomplete"):
        Reader(b"\x00" * 32 + mb.MARKER + meta)
    with pytest.raises(MmdbError, match="not a map"):
        Reader(b"\x00" * 32 + mb.MARKER + mb.text("x"))


def test_reader_size_limit(monkeypatch):
    monkeypatch.setattr(mmdb, "MAX_FILE_BYTES", 10)
    with pytest.raises(MmdbError, match="too large"):
        Reader(mb.build(NETWORKS))


def test_reader_survives_truncated_data_and_wild_pointers():
    # A node whose data pointer lies beyond the data section.
    data = mb.build({"203.0.113.0/24": "DE"})
    r = Reader(data)
    r._data_end = r._data_start  # shrink the data section so the pointer is outside it
    with pytest.raises(MmdbError, match="outside"):
        r.get("203.0.113.1")


def test_search_tree_ending_inside_a_node_is_an_error():
    r = Reader(mb.build(NETWORKS))
    # Make every record point at node 0 forever: the walk ends on a real node, not a result.
    r._record = lambda node, bit: 0  # type: ignore[method-assign]
    with pytest.raises(MmdbError, match="inside a node"):
        r.get("203.0.113.1")


def test_search_tree_larger_than_file():
    meta = mb.mapping(
        [
            ("node_count", mb.uint(6, 5000)),
            ("record_size", mb.uint(5, 24)),
            ("ip_version", mb.uint(5, 4)),
        ]
    )
    with pytest.raises(MmdbError, match="larger than the file"):
        Reader(b"\x00" * 64 + mb.MARKER + meta)


def decode(raw: bytes, base: int = 0, offset: int = 0):
    return mmdb.decode_value(raw, base, offset)[0]


def test_decoder_types():
    assert decode(mb.text("héllo")) == "héllo"
    assert decode(mb._ctrl(3, 8) + b"\x3f\xf8\x00\x00\x00\x00\x00\x00") == 1.5
    assert decode(mb._ctrl(15, 4) + b"\x3f\xc0\x00\x00") == 1.5
    assert decode(mb._ctrl(4, 2) + b"ab") is None  # bytes are skipped, never returned
    assert decode(mb.uint(5, 500)) == 500
    assert decode(mb.uint(6, 70000)) == 70000
    assert decode(mb.uint(9, 2**40)) == 2**40
    assert decode(mb.uint(10, 2**100)) == 2**100
    assert decode(mb._ctrl(8, 4) + b"\xff\xff\xff\xff") == -1
    assert decode(mb._ctrl(8, 1) + b"\x05") == 5
    assert decode(bytes([0b00000001, 7])) is True  # boolean true: extended type 14 (7 + 7)
    assert decode(bytes([0b00000000, 7])) is False
    assert decode(mb._ctrl(11, 2) + mb.text("a") + mb.text("b")) == ["a", "b"]
    long_text = "x" * 40
    assert decode(bytes([(2 << 5) | 29, 40 - 29]) + long_text.encode()) == long_text
    long_text = "y" * 300
    assert decode(bytes([(2 << 5) | 30]) + (300 - 285).to_bytes(2, "big") + long_text.encode()) == long_text
    big = "z" * 66000
    assert decode(bytes([(2 << 5) | 31]) + (66000 - 65821).to_bytes(3, "big") + big.encode()) == big


def test_decoder_pointers():
    # data: [0] "DE", then pointers of every size class pointing back at it
    target = mb.text("DE")
    p0 = bytes([0b00100000 | 0])  # size 0, vvv 0, one more byte
    assert decode(target + p0 + b"\x00", 0, len(target)) == "DE"
    p1 = bytes([0b00101000]) + b"\x00\x00"  # size 1: value + 2048 -> out of range, must raise
    with pytest.raises(MmdbError):
        decode(target + p1, 0, len(target))
    pad = b"\x00" * 3000
    raw = pad + target
    p1_ok = bytes([0b00101000]) + (3000 - 2048).to_bytes(2, "big")
    assert decode(raw + p1_ok, 0, len(raw)) == "DE"
    raw2 = b"\x00" * 600_000 + target
    p2_ok = bytes([0b00110000]) + (600_000 - 526336).to_bytes(3, "big")
    assert decode(raw2 + p2_ok, 0, len(raw2)) == "DE"
    p3_ok = bytes([0b00111000]) + (600_000).to_bytes(4, "big")
    assert decode(raw2 + p3_ok, 0, len(raw2)) == "DE"


def test_decoder_refuses_hostile_input(monkeypatch):
    with pytest.raises(MmdbError, match="pointer to a pointer"):
        decode(bytes([0b00100000, 2]) + bytes([0b00100000, 0]), 0, 0)  # points at a pointer
    with pytest.raises(MmdbError):
        decode(bytes([0b00100000, 0]), 0, 0)  # points at itself: a pointer to a pointer
    with pytest.raises(MmdbError, match="outside"):
        decode(mb._ctrl(2, 5) + b"ab")
    with pytest.raises(MmdbError, match="UTF-8"):
        decode(mb._ctrl(2, 1) + b"\xff")
    with pytest.raises(MmdbError, match="bad double"):
        decode(mb._ctrl(3, 2) + b"ab")
    with pytest.raises(MmdbError, match="bad float"):
        decode(mb._ctrl(15, 2) + b"ab")
    with pytest.raises(MmdbError, match="too long"):
        decode(mb._ctrl(5, 3) + b"abc")
    with pytest.raises(MmdbError, match="too long"):
        decode(mb._ctrl(8, 5) + b"abcde")
    with pytest.raises(MmdbError, match="end marker"):
        decode(bytes([0, 6]))
    with pytest.raises(MmdbError, match="unknown type"):
        decode(bytes([0, 9]))  # extended type 16 does not exist
    with pytest.raises(MmdbError, match="not text"):
        decode(mb._ctrl(7, 1) + mb.uint(5, 1) + mb.uint(5, 1))
    deep = b"".join(mb._ctrl(11, 1) for _ in range(40)) + mb.text("x")
    with pytest.raises(MmdbError, match="deep"):
        decode(deep)
    # a value that expands past the per-lookup budget
    items = 20
    block = mb._ctrl(11, items) + mb.text("x") * items
    payload = mb._ctrl(11, 28) + block * 28
    assert len(decode(payload)) == 28
    monkeypatch.setattr(mmdb, "MAX_VALUES", 100)
    with pytest.raises(MmdbError, match="too large"):
        decode(payload)


# ---------------------------------------------------------------- the lookup
def test_lookup_returns_codes_or_none(tmp_path):
    p = tmp_path / "c.mmdb"
    p.write_bytes(mb.build(NETWORKS))
    lk = geoip.open_lookup(str(p))
    assert lk is not None
    assert lk.country("11.0.1.1") == "DE"
    assert lk.country("8.8.8.8") is None
    assert lk.country("junk") is None


def test_lookup_ignores_odd_records(tmp_path):
    cases = {
        "a": mb.mapping([("continent", mb.mapping([]))]),  # no country
        "b": mb.mapping([("country", mb.mapping([("iso_code", mb.text("DEU"))]))]),  # three letters
        "c": mb.mapping([("country", mb.text("DE"))]),  # country is not a map
        "d": mb.mapping([("country", mb.mapping([("iso_code", mb.text("d1"))]))]),
    }
    nets = {"10.1.0.0/16": "a", "10.2.0.0/16": "b", "10.3.0.0/16": "c", "10.4.0.0/16": "d"}
    p = tmp_path / "c.mmdb"
    p.write_bytes(mb.build(nets, records=cases))
    lk = geoip.open_lookup(str(p))
    assert lk is not None
    assert [lk.country(f"10.{n}.0.1") for n in (1, 2, 3, 4)] == [None] * 4


def test_lookup_turns_a_broken_file_into_none(tmp_path):
    data = bytearray(mb.build({"203.0.113.0/24": "DE"}))
    lk = geoip.MmdbLookup(Reader(bytes(data)))
    lk._reader._data_end = 0  # pretend the data section vanished
    assert lk.country("203.0.113.1") is None


def test_open_lookup_missing_garbage_or_huge(tmp_path, monkeypatch):
    assert geoip.open_lookup(str(tmp_path / "nope.mmdb")) is None
    bad = tmp_path / "bad.mmdb"
    bad.write_bytes(b"not a database")
    assert geoip.open_lookup(str(bad)) is None
    good = tmp_path / "good.mmdb"
    good.write_bytes(mb.build(NETWORKS))
    monkeypatch.setattr(geoip, "MAX_FILE_BYTES", 10)
    assert geoip.open_lookup(str(good)) is None


def test_age_days(tmp_path):
    f = tmp_path / "x"
    f.write_bytes(b"1")
    mtime = f.stat().st_mtime
    assert geoip.age_days(str(f), mtime + 86400 * 3) == pytest.approx(3)
    assert geoip.age_days(str(tmp_path / "missing"), 0) is None


# ---------------------------------------------------------------- checks on a download
def test_check_database():
    good = mb.build(NETWORKS)
    assert geoip.check_database(good, require_size=False).node_count > 0
    with pytest.raises(geoip.GeoIpError, match="too small"):
        geoip.check_database(good, require_size=True)
    with pytest.raises(geoip.GeoIpError, match="not a usable"):
        geoip.check_database(b"<html>nope</html>", require_size=False)
    with pytest.raises(geoip.GeoIpError, match="expected a country"):
        geoip.check_database(mb.build(NETWORKS, database_type="DBIP-City-Lite"), require_size=False)


# ---------------------------------------------------------------- the download, against a local server
class Server:
    def __init__(self):
        self.routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                code, body, headers = outer.routes.get(self.path, (404, b"", {}))
                self.send_response(code)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()


@pytest.fixture
def server():
    s = Server()
    yield s
    s.httpd.shutdown()


def test_fetch_refuses_non_https():
    with pytest.raises(geoip.GeoIpError, match="https"):
        geoip.fetch("http://example.com/x.gz")
    with pytest.raises(geoip.GeoIpError, match="https"):
        geoip.fetch("file:///etc/passwd")


def _fetch_via_plain_http(server, monkeypatch):
    """The real fetch() wants https; for the local server, allow http too. Every other rule stays."""
    monkeypatch.setattr(geoip, "ALLOWED_SCHEMES", ("https", "http"))


def test_fetch_gunzips_and_caps_size(server, monkeypatch):
    _fetch_via_plain_http(server, monkeypatch)
    payload = mb.build(NETWORKS)
    server.routes["/ok.gz"] = (200, gzip.compress(payload), {})
    assert geoip.fetch(server.url + "/ok.gz") == payload
    server.routes["/bad.gz"] = (200, b"this is not gzip", {})
    with pytest.raises(geoip.GeoIpError, match="download failed"):
        geoip.fetch(server.url + "/bad.gz")
    server.routes["/missing"] = (404, b"", {})
    with pytest.raises(geoip.GeoIpError, match="HTTP Error 404"):
        geoip.fetch(server.url + "/missing")
    monkeypatch.setattr(geoip, "MAX_FILE_BYTES", 1000)
    server.routes["/bomb.gz"] = (200, gzip.compress(b"\x00" * 100_000), {})
    with pytest.raises(geoip.GeoIpError, match="larger than"):
        geoip.fetch(server.url + "/bomb.gz")


def test_fetch_refuses_redirects_to_other_hosts(server, monkeypatch):
    _fetch_via_plain_http(server, monkeypatch)
    server.routes["/r"] = (302, b"", {"Location": "http://169.254.169.254/latest/meta-data/"})
    with pytest.raises(geoip.GeoIpError, match="refused a redirect"):
        geoip.fetch(server.url + "/r")
    server.routes["/s"] = (302, b"", {"Location": "https://127.0.0.1/elsewhere"})
    server.routes["/loop"] = (302, b"", {"Location": server.url + "/loop"})
    with pytest.raises(geoip.GeoIpError, match="refused a redirect"):
        geoip.fetch(server.url + "/loop")


def test_candidate_urls():
    jan = 1_736_000_000  # 2025-01-04
    urls = geoip.candidate_urls(None, jan)
    assert urls[0].endswith("dbip-country-lite-2025-01.mmdb.gz")
    assert urls[1].endswith("dbip-country-lite-2024-12.mmdb.gz")
    mid = 1_750_000_000  # 2025-06-15
    assert geoip.candidate_urls(None, mid)[1].endswith("2025-05.mmdb.gz")
    assert geoip.candidate_urls("https://example.com/x.gz", mid) == ["https://example.com/x.gz"]


def test_update_replaces_only_a_good_file(server, tmp_path, monkeypatch):
    _fetch_via_plain_http(server, monkeypatch)
    dest = tmp_path / "sub" / "country.mmdb"
    now = 1_750_000_000
    server.routes["/good.gz"] = (200, gzip.compress(mb.build(NETWORKS)), {})
    assert geoip.update(str(dest), server.url + "/good.gz", now).startswith("updated")
    assert geoip.open_lookup(str(dest)) is not None
    first = dest.read_bytes()
    # fresh file: nothing to do, unless forced
    import os

    os.utime(dest, (now, now))
    assert "nothing to do" in geoip.update(str(dest), server.url + "/good.gz", now + 86400)
    assert geoip.update(str(dest), server.url + "/good.gz", now + 86400, force=True).startswith("updated")
    # a bad download leaves the old file alone
    server.routes["/evil.gz"] = (200, gzip.compress(b"<html>captive portal</html>"), {})
    with pytest.raises(geoip.GeoIpError, match="not a usable"):
        geoip.update(str(dest), server.url + "/evil.gz", now, force=True)
    assert dest.read_bytes() == first
    assert [p.name for p in dest.parent.iterdir()] == ["country.mmdb"]  # no temp files left behind


def test_update_default_urls_fall_back_and_report(server, tmp_path, monkeypatch):
    monkeypatch.setattr(geoip, "BASE_URL", "https://127.0.0.1:1/{when}.gz")
    with pytest.raises(geoip.GeoIpError, match="download failed"):
        geoip.update(str(tmp_path / "x.mmdb"), None, 1_750_000_000)


def test_update_cannot_write(server, tmp_path, monkeypatch):
    _fetch_via_plain_http(server, monkeypatch)
    server.routes["/good.gz"] = (200, gzip.compress(mb.build(NETWORKS)), {})
    blocker = tmp_path / "file"
    blocker.write_text("x")
    with pytest.raises(geoip.GeoIpError, match="cannot write"):
        geoip.update(str(blocker / "country.mmdb"), server.url + "/good.gz", 1_750_000_000)


# ---------------------------------------------------------------- audit fixes (docs/security-review.md)
def _record_file(record: bytes, extra: dict[str, bytes] | None = None) -> bytes:
    """A database whose 1.0.0.0/8 maps to `record` (the first record in the data section, offset 0)."""
    return mb.build({"1.0.0.0/8": "AU", "2.0.0.0/8": "BB"}, records={"AU": record, **(extra or {})})


def _pointers(n: int) -> bytes:
    return bytes([30, 11 - 7]) + (n - 285).to_bytes(2, "big") + b"\x20\x00" * n  # n pointers to offset 0


def test_the_size_cap_is_64_mb():
    assert mmdb.MAX_FILE_BYTES == 64 * 1024 * 1024


def test_a_pointer_to_a_big_bytes_value_copies_nothing():
    n = 300_000
    big = bytes([(4 << 5) | 31]) + (n - 65821).to_bytes(3, "big") + b"\x00" * n
    r = Reader(_record_file(big, {"BB": _pointers(300)}))
    value = r.get("2.2.2.2")
    assert value == [None] * 300  # 300 pointers at 300 KB would have been 90 MB; bytes are never returned


def test_a_pointer_to_a_big_string_hits_the_byte_budget():
    n = 100_000
    big = bytes([(2 << 5) | 31]) + (n - 65821).to_bytes(3, "big") + b"a" * n
    r = Reader(_record_file(big, {"BB": _pointers(300)}))
    with pytest.raises(MmdbError, match="too large"):
        r.get("2.2.2.2")
    assert len(r.get("1.1.1.1")) == n  # one copy is fine


def test_a_number_that_is_not_finite_is_an_mmdb_error(tmp_path):
    base = mb.build({"1.0.0.0/8": "AU"})
    cut = base.rfind(mb.MARKER) + len(mb.MARKER)
    inf = bytes([(3 << 5) | 8]) + struct.pack(">d", float("inf"))
    meta = mb.mapping(
        [
            ("node_count", inf),
            ("record_size", mb.uint(5, 24)),
            ("ip_version", mb.uint(5, 6)),
            ("database_type", mb.text("DBIP-Country-Lite")),
        ]
    )
    data = base[:cut] + meta
    with pytest.raises(MmdbError, match="incomplete"):
        Reader(data)
    p = tmp_path / "inf.mmdb"
    p.write_bytes(data)
    assert geoip.open_lookup(str(p)) is None
    with pytest.raises(geoip.GeoIpError, match="not a usable"):
        geoip.check_database(data, require_size=False)


def test_open_lookup_never_raises(tmp_path, monkeypatch):
    p = tmp_path / "x.mmdb"
    p.write_bytes(mb.build(NETWORKS))

    def explode(data):
        raise RuntimeError("anything at all")

    monkeypatch.setattr(geoip, "Reader", explode)
    assert geoip.open_lookup(str(p)) is None


class _CountingStream:
    def __init__(self):
        self.sent = 0

    def read(self, size=-1):
        self.sent += size
        return b"\x00" * size


def test_the_size_cap_is_checked_while_the_data_streams():
    stream = _CountingStream()
    with pytest.raises(geoip.GeoIpError, match="larger than"):
        geoip._read_limited(stream, 200_000, time.monotonic() + 60)
    assert stream.sent <= 200_000 + 2 * geoip.CHUNK


def test_a_download_has_an_overall_deadline():
    class Drip(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                for _ in range(1000):
                    self.wfile.write(b"x")
                    self.wfile.flush()
                    time.sleep(0.05)
            except OSError:
                pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), Drip)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        # a read finishes every 50 ms, so the per-read timeout never fires; only the deadline can stop it
        saved = geoip.ALLOWED_SCHEMES
        geoip.ALLOWED_SCHEMES = ("https", "http")
        started = time.monotonic()
        with pytest.raises(geoip.GeoIpError, match=r"too long|download failed"):
            geoip.fetch(f"http://127.0.0.1:{httpd.server_port}/x.gz", timeout=5, max_seconds=0.5)
        assert time.monotonic() - started < 5
    finally:
        geoip.ALLOWED_SCHEMES = saved
        httpd.shutdown()


def test_a_file_dated_in_the_future_is_updated(tmp_path, monkeypatch):
    import os

    dest = tmp_path / "country.mmdb"
    dest.write_bytes(b"old")
    now = 1_750_000_000
    os.utime(dest, (now + 400 * 86400, now + 400 * 86400))
    assert geoip.age_days(str(dest), now) is None
    fresh = mb.build(NETWORKS)
    monkeypatch.setattr(geoip, "fetch", lambda url: fresh)
    assert geoip.update(str(dest), "https://example.com/x.gz", now).startswith("updated")
    assert dest.read_bytes() == fresh


def test_a_redirect_to_another_port_is_refused(server, monkeypatch):
    _fetch_via_plain_http(server, monkeypatch)
    other = Server()
    try:
        server.routes["/r"] = (302, b"", {"Location": other.url + "/x"})
        with pytest.raises(geoip.GeoIpError, match="refused a redirect"):
            geoip.fetch(server.url + "/r")
    finally:
        other.httpd.shutdown()


def test_an_unusable_download_is_reported_not_raised():
    for junk in (b"", b"\x00" * 50, mb.MARKER, b"x" * 1000 + mb.MARKER + b"\xff" * 10):
        with pytest.raises(geoip.GeoIpError):
            geoip.check_database(junk, require_size=False)


def test_codes_lists_what_the_file_can_answer(tmp_path):
    p = tmp_path / "c.mmdb"
    p.write_bytes(mb.build(NETWORKS))
    lk = geoip.open_lookup(str(p))
    assert lk is not None
    assert lk.codes() == {"DE", "US", "FR"}
    r = Reader(mb.build(NETWORKS))
    r._record = lambda node, bit: 0  # type: ignore[method-assign]  # a loop: must end, not hang
    assert r.records() == []
    with pytest.raises(MmdbError, match="too many"):
        Reader(mb.build(NETWORKS)).records(limit=1)
