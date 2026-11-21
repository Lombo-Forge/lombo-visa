"""A small Jellyfin client, standard library only.

Authentication uses an API key (Jellyfin: Dashboard > API Keys) in the Authorization header.
Every failure becomes a JellyfinError with a short message, so callers print it instead of crashing.

Calls used: GET /Sessions (who is signed in, from which address), GET /Users (name to id, admin flag),
DELETE /Devices?id= (sign a device out: Jellyfin removes the device and its access tokens).
"""

import http.client
import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, cast

from .core import Session, clean_text


class JellyfinError(Exception):
    """A request failed. The message names the path and the reason."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: urllib would send the API key on to wherever it points."""

    def redirect_request(  # type: ignore[no-untyped-def]  # signature comes from the standard library
        self, req, fp, code, msg, headers, newurl
    ):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


class Jellyfin:
    def __init__(self, base_url: str, api_key: str, timeout: float = 10):
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.timeout = timeout

    def _call(self, method: str, path: str, auth: bool = True) -> Any:
        try:
            # S310 is silenced here and below: config.py only accepts http(s) URLs.
            req = urllib.request.Request(self.base + path, method=method)  # noqa: S310
            if auth:
                req.add_header("Authorization", f'MediaBrowser Token="{self.key}"')
            with _OPENER.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                where = clean_text(e.headers.get("Location") if e.headers else "", 100)
                raise JellyfinError(
                    f"{path}: Jellyfin answered with a redirect to {where!r}. "
                    "Set LOMBO_VISA_JELLYFIN_URL to the final address."
                ) from e
            raise JellyfinError(f"{path}: HTTP {e.code}") from e
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            raise JellyfinError(f"{path}: {e}") from e
        if not raw:
            return None
        try:
            return json.loads(raw)
        except (ValueError, RecursionError) as e:
            raise JellyfinError(f"{path}: answer is not JSON ({e})") from e

    def public_info(self) -> dict[str, Any]:
        """Server name and version. Needs no key, so it tells 'unreachable' apart from 'bad key'."""
        return cast(dict[str, Any], self._call("GET", "/System/Info/Public", auth=False))

    def info(self) -> dict[str, Any]:
        """Needs a valid key."""
        return cast(dict[str, Any], self._call("GET", "/System/Info"))

    def sessions(self) -> list[Session]:
        """Sessions that belong to a signed-in user. Entries without a user or an address field are kept
        with empty text so the rule can treat them as unknown."""
        raw = self._call("GET", "/Sessions")
        if not isinstance(raw, list):
            raise JellyfinError("/Sessions: answer is not a list")
        out: list[Session] = []
        for s in raw:
            if not isinstance(s, dict) or not s.get("UserId"):
                continue
            out.append(
                Session(
                    user_id=str(s.get("UserId", "")),
                    user_name=str(s.get("UserName") or ""),
                    device_id=str(s.get("DeviceId") or ""),
                    device_name=str(s.get("DeviceName") or ""),
                    client=str(s.get("Client") or ""),
                    ip=str(s.get("RemoteEndPoint") or ""),
                )
            )
        return out

    def users(self) -> list[dict[str, Any]]:
        raw = self._call("GET", "/Users")
        if not isinstance(raw, list):
            raise JellyfinError("/Users: answer is not a list")
        return [u for u in raw if isinstance(u, dict)]

    def sign_out_device(self, device_id: str) -> None:
        """Remove the device and with it its access tokens. A 404 means it is already gone."""
        try:
            self._call("DELETE", "/Devices?" + urllib.parse.urlencode({"id": device_id}))
        except JellyfinError as e:
            if "HTTP 404" not in str(e):
                raise
