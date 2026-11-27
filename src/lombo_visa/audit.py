"""The audit log: one JSON line per decision, so "why was I blocked?" has an answer.

Each line: {"ts", "mode": "session"|"gateway", "decision", "country", "user", "ip_hash"}.
The address is never written. `ip_hash` is a truncated HMAC-SHA256 of it, keyed with a random secret kept
beside the state file (`audit.key`, mode 0600, made on first use): the same address gives the same hash
(so repeats can be spotted) but nobody can turn a hash back into an address without the key.

The file is capped (`MAX_BYTES`); when it is full it moves to `<file>.1` (one old file is kept). Writing the log must
never break a decision: every failure here is swallowed after one warning.
"""

import contextlib
import fcntl
import hashlib
import hmac
import json
import os
import secrets
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

from .core import clean_text

MAX_BYTES = 5 * 1024 * 1024
KEY_FILE = "audit.key"
KEY_BYTES = 32
HASH_CHARS = 16

_warned: set[str] = set()
_thread_lock = threading.Lock()


def _warn_once(path: str, why: str) -> None:
    if path not in _warned:
        _warned.add(path)
        print(f"[WARN] audit log {path}: {why}. Decisions are not being recorded.", file=sys.stderr)


def key_path(audit_file: str) -> Path:
    """The secret lives next to the log, so one mounted data folder holds both."""
    return Path(audit_file).parent / KEY_FILE


def load_key(audit_file: str) -> bytes:
    """The hashing key; made (mode 0600) on first use. Raises OSError or ValueError when it cannot be had."""
    path = key_path(audit_file)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as f:
            f.write(secrets.token_bytes(KEY_BYTES))
    key = path.read_bytes()
    if len(key) != KEY_BYTES:
        raise ValueError(f"{path} is not a key file (expected {KEY_BYTES} bytes); delete it to make a new one")
    return key


def ip_hash(key: bytes, ip: str | None) -> str:
    if not ip:
        return ""
    return hmac.new(key, ip.encode(), hashlib.sha256).hexdigest()[:HASH_CHARS]


@contextlib.contextmanager
def _locked(audit_file: str) -> Iterator[None]:
    with _thread_lock:
        fd = os.open(audit_file + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)


def record(audit_file: str, mode: str, decision: str, country: str, user: str, ip: str | None, now: float) -> None:
    """Append one line. Does nothing when `audit_file` is empty. Never raises."""
    if not audit_file:
        return
    try:
        line = (
            json.dumps(
                {
                    "ts": int(now),
                    "mode": mode,
                    "decision": decision,
                    "country": country,
                    "user": clean_text(user),
                    "ip_hash": ip_hash(load_key(audit_file), ip),
                }
            )
            + "\n"
        )
        Path(audit_file).parent.mkdir(parents=True, exist_ok=True)
        with _locked(audit_file):
            try:
                if os.stat(audit_file).st_size >= MAX_BYTES:
                    os.replace(audit_file, audit_file + ".1")
            except FileNotFoundError:
                pass
            fd = os.open(audit_file, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a") as f:
                f.write(line)
    except (OSError, ValueError) as e:
        _warn_once(audit_file, clean_text(e, 120))


def read(audit_file: str, last: int, user: str | None) -> list[dict[str, object]]:
    """The newest `last` entries (oldest first), from the old and the current file; damaged lines are skipped."""
    rows: list[dict[str, object]] = []
    for name in (audit_file + ".1", audit_file):
        try:
            text = Path(name).read_text(errors="replace")
        except OSError:
            continue
        for raw in text.splitlines():
            try:
                row = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(row, dict) or not isinstance(row.get("ts"), int | float):
                continue
            if user is not None and str(row.get("user", "")).lower() != user.lower():
                continue
            rows.append(row)
    return rows[-last:] if last > 0 else []


def counts(audit_file: str, since: float) -> dict[str, dict[str, int]]:
    """{mode: {decision: how many}} for entries at or after `since`. Per mode, because the gateway and session
    mode can both record one sign-in."""
    out: dict[str, dict[str, int]] = {}
    for row in read(audit_file, sys.maxsize, None):
        if float(str(row["ts"])) >= since:
            mode = out.setdefault(str(row.get("mode", "")), {})
            decision = str(row.get("decision", ""))
            mode[decision] = mode.get(decision, 0) + 1
    return out
