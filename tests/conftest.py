"""A fake Jellyfin and a fake webhook on localhost, and a tiny generated country database."""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import mmdb_builder

# Documentation ranges (203.0.113.x and the like) count as private to Python, so the fixtures use ordinary-looking ones.
NETWORKS = {"11.0.1.0/24": "DE", "12.0.1.0/24": "US", "2a00:1450::/32": "FR"}


def session(user="pat", device="dev-1", ip="11.0.1.9", uid="aaaa-bbbb", name=None):
    return {
        "UserId": uid,
        "UserName": user,
        "DeviceId": device,
        "DeviceName": name or f"Phone {device}",
        "Client": "Jellyfin Web",
        "RemoteEndPoint": ip,
    }


class Fake:
    def __init__(self):
        self.requests: list[dict] = []
        self.key = "good-key"
        self.sessions: list[dict] = []
        self.users = [
            {"Id": "aaaa-bbbb", "Name": "Pat", "Policy": {"IsAdministrator": False}},
            {"Id": "cccc-dddd", "Name": "Boss", "Policy": {"IsAdministrator": True}},
        ]
        self.delete_status = 204
        self.sessions_status = 200
        self.hook_status = 200
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self, code, payload=None):
                data = json.dumps(payload if payload is not None else {}).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                outer.requests.append({"method": "GET", "path": self.path, "auth": self.headers.get("Authorization")})
                if self.path == "/System/Info/Public":
                    return self._reply(200, {"ServerName": "Fake", "Version": "0.0.0"})
                if self.headers.get("Authorization") != f'MediaBrowser Token="{outer.key}"':
                    return self._reply(401)
                if self.path == "/System/Info":
                    return self._reply(200, {"ServerName": "Fake"})
                if self.path == "/Sessions":
                    return self._reply(outer.sessions_status, outer.sessions)
                if self.path == "/Users":
                    return self._reply(200, outer.users)
                return self._reply(404)

            def do_DELETE(self):
                device = parse_qs(urlparse(self.path).query).get("id", [""])[0]
                outer.requests.append({"method": "DELETE", "path": self.path, "device": device})
                self.send_response(outer.delete_status)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.requests.append({"method": "POST", "path": self.path, "body": json.loads(body)})
                self._reply(outer.hook_status)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def posts(self):
        return [r for r in self.requests if r["method"] == "POST"]

    def deleted(self):
        return [r["device"] for r in self.requests if r["method"] == "DELETE"]


@pytest.fixture
def fake():
    f = Fake()
    yield f
    f.server.shutdown()


@pytest.fixture
def db_file(tmp_path):
    path = tmp_path / "country.mmdb"
    path.write_bytes(mmdb_builder.build(NETWORKS))
    return path


@pytest.fixture
def env(fake, tmp_path, db_file):
    return {
        "LOMBO_VISA_JELLYFIN_URL": fake.url,
        "LOMBO_VISA_JELLYFIN_API_KEY": "good-key",
        "LOMBO_VISA_WEBHOOK_URL": fake.url + "/hook",
        "LOMBO_VISA_HOME_COUNTRIES": "US",
        "LOMBO_VISA_GEOIP_DB": str(db_file),
        "LOMBO_VISA_STATE_FILE": str(tmp_path / "state.json"),
    }
