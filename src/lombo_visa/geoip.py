"""IP address to country. The only module that knows about the database file.

The rest of the app sees one small interface, `Lookup.country(ip)`, which returns an upper-case
two-letter code or None. None means "do not know" for every reason (no file, bad file, address not in
it), and callers must treat it as allow. Swap the file reader for another source by writing a class
with the same method.
"""

import contextlib
import gzip
import http.client
import io
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Protocol

from .mmdb import MAX_FILE_BYTES, MmdbError, Reader

BASE_URL = "https://download.db-ip.com/free/dbip-country-lite-{when}.mmdb.gz"
MIN_DEFAULT_BYTES = 1_000_000  # the real country file is several megabytes; a tiny one is not it
FRESH_DAYS = 20
STALE_DAYS = 75
MAX_REDIRECTS = 3
ALLOWED_SCHEMES = ("https",)  # tests widen this to talk to a local server; nothing else changes it


class GeoIpError(Exception):
    """The database could not be fetched or did not pass the checks. The old file is untouched."""


class Lookup(Protocol):
    def country(self, ip: str) -> str | None: ...

    def codes(self) -> set[str]: ...


class MmdbLookup:
    def __init__(self, reader: Reader) -> None:
        self._reader = reader

    def country(self, ip: str) -> str | None:
        try:
            record = self._reader.get(ip)
        except MmdbError:
            return None
        return _code(record.get("country") if isinstance(record, dict) else None)

    def codes(self) -> set[str]:
        """Every country code the file can answer with (walks the whole tree: for `doctor`, not for rounds)."""
        found = {_code(r.get("country")) for r in self._reader.records() if isinstance(r, dict)}
        return {c for c in found if c}


def _code(country: object) -> str | None:
    code = country.get("iso_code") if isinstance(country, dict) else None
    return code.upper() if isinstance(code, str) and len(code) == 2 and code.isalpha() else None


def open_lookup(path: str) -> Lookup | None:
    """The lookup for the file at `path`, or None when it is missing or unusable.

    Called once per round, so a monthly update is picked up without a restart.
    """
    try:
        if Path(path).stat().st_size > MAX_FILE_BYTES:
            return None
        return MmdbLookup(Reader(Path(path).read_bytes()))
    except Exception:  # noqa: BLE001 (this function's job is "None on any problem": unknown is allowed)
        return None


def age_days(path: str, now: float) -> float | None:
    """Days since the file changed; None when it is missing or its date is in the future (a wrong clock once)."""
    try:
        age = (now - Path(path).stat().st_mtime) / 86400
    except OSError:
        return None
    return age if age >= 0 else None


def check_database(data: bytes, require_size: bool) -> Reader:
    """Raise GeoIpError unless `data` is a country database the reader can use."""
    if require_size and len(data) < MIN_DEFAULT_BYTES:
        raise GeoIpError("the download is too small to be the country database")
    try:
        reader = Reader(data)
        # Probe a few addresses so a file that opens but fails on lookup is caught here.
        for probe in ("1.1.1.1", "8.8.8.8", "2001:4860:4860::8888"):
            reader.get(probe)
    except Exception as e:
        raise GeoIpError(f"the download is not a usable GeoIP database: {e}") from e
    if "country" not in reader.database_type.lower():
        raise GeoIpError(f"the database type is {reader.database_type!r}, expected a country database")
    return reader


class _SameHostHttps(urllib.request.HTTPRedirectHandler):
    """Follow a redirect only to https on the same host and port, at most MAX_REDIRECTS times."""

    def __init__(self, netloc: str) -> None:
        self.netloc = netloc.lower()
        self.count = 0

    def redirect_request(  # type: ignore[no-untyped-def]  # signature comes from the standard library
        self, req, fp, code, msg, headers, newurl
    ):
        target = urllib.parse.urlparse(newurl)
        self.count += 1
        if target.scheme not in ALLOWED_SCHEMES or target.netloc.lower() != self.netloc or self.count > MAX_REDIRECTS:
            raise GeoIpError(f"refused a redirect to {target.scheme}://{target.netloc}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class _Readable(Protocol):
    def read(self, size: int = ..., /) -> bytes: ...


CHUNK = 64 * 1024
MAX_SECONDS = 300  # the whole download, however slowly the server sends it


def _read_limited(stream: _Readable, limit: int, deadline: float) -> bytes:
    """Read in chunks, refusing as soon as the data passes `limit` or the clock passes `deadline` (time.monotonic)."""
    out = bytearray()
    # read1 returns what has arrived, so a slow trickle cannot hold us inside one call
    read = getattr(stream, "read1", stream.read)
    while chunk := read(CHUNK):
        out += chunk
        if len(out) > limit:
            raise GeoIpError("the download is larger than the allowed size")
        if time.monotonic() > deadline:
            raise GeoIpError("the download took too long")
    return bytes(out)


def fetch(url: str, timeout: float = 60, max_seconds: float = MAX_SECONDS) -> bytes:
    """Download and decompress one .mmdb.gz. https only, same-host redirects only, size and total time capped."""
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme not in ALLOWED_SCHEMES or not parsed.hostname:
        raise GeoIpError("the database URL must start with https://")
    opener = urllib.request.build_opener(_SameHostHttps(parsed.netloc))
    opener.addheaders = [("User-Agent", "lombo-visa")]  # the default source answers 403 to Python's own
    deadline = time.monotonic() + max_seconds
    try:
        with opener.open(url, timeout=timeout) as resp:
            packed = _read_limited(resp, MAX_FILE_BYTES // 4, deadline)
        with gzip.GzipFile(fileobj=io.BytesIO(packed)) as unpacked:
            return _read_limited(unpacked, MAX_FILE_BYTES, deadline)
    except GeoIpError:
        raise
    except (urllib.error.URLError, OSError, EOFError, ValueError, zlib.error, http.client.HTTPException) as e:
        raise GeoIpError(f"download failed: {e}") from e


def candidate_urls(url_override: str | None, now: float) -> list[str]:
    if url_override:
        return [url_override]
    this_month = time.gmtime(now)
    year, month = (this_month.tm_year, this_month.tm_mon - 1) if this_month.tm_mon > 1 else (this_month.tm_year - 1, 12)
    return [
        BASE_URL.format(when=time.strftime("%Y-%m", this_month)),
        BASE_URL.format(when=f"{year:04d}-{month:02d}"),
    ]


def update(path: str, url_override: str | None, now: float, force: bool = False) -> str:
    """Fetch the database into `path` if it is old or missing. Returns a one-line result.

    The new file is checked, written beside the old one and moved in with one rename; any failure
    leaves the old file in place.
    """
    age = age_days(path, now)
    if age is not None and age < FRESH_DAYS and not force:  # age is None for a missing file or a date in the future
        return f"{path} is {age:.0f} days old, nothing to do"
    last: GeoIpError | None = None
    for url in candidate_urls(url_override, now):
        try:
            data = fetch(url)
            check_database(data, require_size=url_override is None)
        except GeoIpError as e:
            last = e
            continue
        _replace(path, data)
        return f"updated {path}"
    raise GeoIpError(str(last) if last else "no URL to try")


def _replace(path: str, data: bytes) -> None:
    folder = Path(path).parent
    try:
        folder.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=folder, prefix=".geoip-")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
            Path(tmp).chmod(0o644)
            os.replace(tmp, path)
        finally:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
    except OSError as e:
        raise GeoIpError(f"cannot write {path}: {e}") from e
