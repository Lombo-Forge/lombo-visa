"""The small JSON file that remembers things between rounds.

- `devices`: users and devices that were allowed (key "userid:deviceid"), so they are never judged again.
  A device allowed by a travel window carries the window id, so it can be signed out when the window ends.
- `noted`: new sessions already reported (log line and alert), so a round does not repeat itself.
- `windows`: travel windows made with `lombo-visa travel add`, or by a person through the API (those carry
  `"self": true`, which the cool-down reads).
- `baselined`: the first run records the sessions and signed-in devices that already exist as allowed.
- `devices_baselined`: the device list has been recorded too (older versions recorded sessions only).

A missing file means "start fresh". A damaged one is moved aside (`state.json.bad-<time>`, with a loud warning) and
then also starts fresh, so nothing is lost silently; a file that cannot be read at all stops the round instead.
Every command that changes the file holds `locked()` for its whole load-change-save, so two of them never overwrite
each other.
"""

import contextlib
import fcntl
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from .core import Window

NOTED_KEEP_SECONDS = 7 * 86400
MAX_DEVICES = 5000  # remembered user:device pairs


class StateError(Exception):
    """The state file could not be read or written."""


def empty() -> dict[str, Any]:
    return {"baselined": False, "devices_baselined": False, "devices": {}, "noted": {}, "windows": {}}


def _set_aside(path: str) -> str:
    bad = f"{path}.bad-{time.strftime('%Y%m%d-%H%M%S', time.gmtime())}"
    try:
        os.replace(path, bad)
    except OSError as e:
        raise StateError(f"{path} is damaged and could not be moved aside: {e}") from e
    return bad


def load(path: str, quarantine: bool = True) -> dict[str, Any]:
    """The saved state. Missing = fresh. Unreadable (permissions, I/O) = StateError, so the round is skipped.
    Damaged (not JSON, not an object, empty) = moved to `<path>.bad-<time>` with a warning, or StateError when
    `quarantine` is false (doctor and plan never touch the file)."""
    try:
        text = Path(path).read_text()
    except FileNotFoundError:
        return empty()
    except (OSError, UnicodeDecodeError) as e:
        raise StateError(f"cannot read {path}: {e}. The round is skipped; nothing was changed.") from e
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("not a JSON object")
    except (ValueError, RecursionError) as e:
        if not quarantine:
            raise StateError(f"{path} is damaged ({e}). The next run will move it aside and start fresh.") from e
        bad = _set_aside(path)
        print(
            f"[WARN] {path} is damaged ({e}). Moved it to {bad} and starting fresh: travel windows in it are gone "
            "and the sessions and devices signed in now are recorded as allowed.",
            file=sys.stderr,
        )
        return empty()
    out = empty()
    for name in ("devices", "noted", "windows"):
        if isinstance(data.get(name), dict):
            out[name] = data[name]
    out["baselined"] = data.get("baselined") is True
    out["devices_baselined"] = data.get("devices_baselined") is True
    return out


def save(path: str, state: dict[str, Any]) -> None:
    tmp = ""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=Path(path).parent, prefix=".state-")
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(state))
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as e:
        if tmp:
            with contextlib.suppress(OSError):
                Path(tmp).unlink()
        raise StateError(
            f"cannot write {path}: {e}. Mount a writable folder at /data or change LOMBO_VISA_STATE_FILE."
        ) from e


def check_writable(path: str) -> None:
    """Raise StateError unless a file can be created beside `path`. Writes nothing that stays."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=Path(path).parent, prefix=".state-check-")
        os.close(fd)
        Path(tmp).unlink()
    except OSError as e:
        raise StateError(
            f"cannot write {path}: {e}. Mount a writable folder at /data or change LOMBO_VISA_STATE_FILE."
        ) from e


@contextlib.contextmanager
def locked(path: str) -> Iterator[None]:
    """Hold an exclusive lock (`<path>.lock`) while loading, changing and saving, so two commands never clash."""
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    except OSError as e:
        raise StateError(
            f"cannot lock {path}: {e}. Mount a writable folder at /data or change LOMBO_VISA_STATE_FILE."
        ) from e
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)  # closing the file also drops the lock


def windows(state: dict[str, Any]) -> list[Window]:
    """The stored windows. An entry that cannot be read is skipped (so it grants nothing)."""
    out: list[Window] = []
    for key, w in state["windows"].items():
        try:
            revoked = w.get("revoked_at")
            out.append(
                Window(
                    int(key),
                    str(w["user_id"]),
                    str(w["user_name"]),
                    w.get("country") or None,
                    float(w["starts_at"]),
                    float(w["ends_at"]),
                    float(revoked) if revoked is not None else None,
                )
            )
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return out


def _is_self(entry: object) -> bool:
    return isinstance(entry, dict) and entry.get("self") is True


def self_ids(state: dict[str, Any]) -> set[int]:
    """Ids of windows a person opened for themselves through the API (`"self": true`)."""
    out: set[int] = set()
    for key, entry in state["windows"].items():
        if _is_self(entry):
            try:
                out.add(int(key))
            except ValueError:
                continue
    return out


def put_window(state: dict[str, Any], w: Window, own: bool = False) -> None:
    """Store a window. `own=True` marks it self-opened; an existing mark is kept when the window is rewritten
    (a revoke), so the cool-down still sees it."""
    entry: dict[str, Any] = {
        "user_id": w.user_id,
        "user_name": w.user_name,
        "country": w.country,
        "starts_at": w.starts_at,
        "ends_at": w.ends_at,
        "revoked_at": w.revoked_at,
    }
    if own or _is_self(state["windows"].get(str(w.id))):
        entry["self"] = True
    state["windows"][str(w.id)] = entry


SELF_KEEP_SECONDS = 400 * 86400  # longer than the longest cool-down (365 days), so pruning never ends one early


def prune(state: dict[str, Any], now: float) -> None:
    """Forget old notes and windows that ended more than a month ago (self-opened ones: 400 days, for the cool-down)."""
    state["noted"] = {
        k: v for k, v in state["noted"].items() if isinstance(v, int | float) and now - v < NOTED_KEEP_SECONDS
    }
    in_use = {str(d.get("window")) for d in state["devices"].values() if isinstance(d, dict)}
    for key, w in list(state["windows"].items()):
        ends = w.get("ends_at") if isinstance(w, dict) else None
        keep = SELF_KEEP_SECONDS if _is_self(w) else 30 * 86400
        if isinstance(ends, int | float) and now - ends > keep and key not in in_use:
            del state["windows"][key]
