"""Discord answers 403 to urllib's default User-Agent, so the webhook must send its own."""

import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from lombo_visa import notify


def test_webhook_sends_its_own_user_agent():
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            seen.append(self.headers["User-Agent"])
            self.rfile.read(int(self.headers["Content-Length"]))
            self.send_response(204)
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        notify.send(f"http://127.0.0.1:{srv.server_port}/", "hi")
    finally:
        srv.shutdown()
    assert seen == ["lombo-visa"]
