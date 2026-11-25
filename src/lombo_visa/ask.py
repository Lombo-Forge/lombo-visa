"""The optional "ask to be let in" page the gateway serves at /visa/ask: a person refused abroad types their name
and the owner gets a webhook message. No sign-in, so everything is limited: tries per address and in all, one alert
per name a day, the same answer whatever was typed, and the address is never sent or kept.
"""

import json
import threading
import time
from collections import deque
from collections.abc import Callable
from urllib.parse import parse_qs

from . import core, notify
from .config import Config

PATH = "/visa/ask"
PER_ADDRESS, IN_ALL, HOUR, DAY = 3, 20, 3600.0, 86400.0
ANSWER = "The server owner has been told."
BUSY = "Too many tries. Try again in an hour, or contact the server owner another way."

PAGE = """<!doctype html><html lang="en"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Sign-in not allowed from here</title>
<style>body{font:16px system-ui,sans-serif;max-width:26em;margin:15vh auto;padding:0 1em;color-scheme:light dark}
input,button{font:inherit;padding:.5em;margin:.25em 0;width:100%;box-sizing:border-box}</style>
<h1>Sign-in not allowed from here</h1>
<p>Travelling? Ask the server owner to allow your trip.</p>
<form method="post" action="/visa/ask">
<label for="name">Your account name</label><input id="name" name="name" maxlength="40" required autocomplete="username">
<button type="submit">Ask to be let in</button></form></html>"""


def page(cfg: Config) -> bytes:
    """The owner's own page when set and readable, else the built-in one."""
    if cfg.ask_page_file:
        try:
            with open(cfg.ask_page_file, "rb") as f:
                return f.read(256 * 1024)
        except OSError:
            pass  # doctor reports it
    return PAGE.encode()


def name_from(content_type: str, body: bytes) -> str:
    """The typed name from a plain HTML form or from JSON {"name": ...}; "" when there is none."""
    try:
        if content_type.partition(";")[0].strip().lower() == "application/json":
            value = json.loads(body).get("name")
        else:
            value = parse_qs(body.decode()).get("name", [""])[0]
    except (ValueError, AttributeError, RecursionError):
        return ""
    return core.chat_text(value.strip(), 40) if isinstance(value, str) else ""


class Asks:
    def __init__(self, cfg: Config, log: Callable[[str], None], clock: Callable[[], float] = time.time) -> None:
        self.cfg, self.log, self.clock = cfg, log, clock
        self.by_address: dict[str, deque[float]] = {}
        self.all: deque[float] = deque()
        self.told: dict[str, float] = {}
        self.lock = threading.Lock()

    def ask(self, name: str, address: str, country: str) -> tuple[int, str]:
        if not name:
            return 422, "Type your account name."
        with self.lock:
            now = self.clock()
            for times in (self.all, self.by_address.setdefault(address, deque())):
                while times and now - times[0] >= HOUR:
                    times.popleft()
            mine = self.by_address[address]
            if len(mine) >= PER_ADDRESS or len(self.all) >= IN_ALL:
                return 429, BUSY
            mine.append(now)
            self.all.append(now)
            if len(self.by_address) > 1024:
                del self.by_address[next(iter(self.by_address))]
            self.told = {n: t for n, t in self.told.items() if now - t < DAY}
            if name.lower() in self.told:
                return 200, ANSWER
            self.told[name.lower()] = now
        where = "an unknown country" if country == core.UNKNOWN else country
        self.send(f"LomboVisa: {name} cannot sign in from {where} and asks you to allow their trip.")
        return 200, ANSWER

    def send(self, message: str) -> None:
        if self.cfg.dry_run or not self.cfg.webhook_url:
            self.log("ask: someone asked to be let in (no alert: dry run or no webhook)")
            return
        url = self.cfg.webhook_url

        def post() -> None:
            try:
                notify.send(url, message)
            except notify.NotifyError as e:
                self.log(f"ask: {core.clean_text(e, 200)}")

        threading.Thread(target=post, daemon=True).start()  # the answer never waits for the webhook
