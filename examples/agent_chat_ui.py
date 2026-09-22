"""Persona: the agent in the Amazon Connect agent chat UI (CCP v2, any https://<alias>.my.connect.aws/ccp-v2 page).

uv run --env-file .env python examples/agent_chat_ui.py                    prime only, then exit
uv run --env-file .env python examples/agent_chat_ui.py --goal GOAL        prime, then run GOAL on this page alone

Like every persona (examples/personas.py): NAME, matches(url), probe(...) for independent checks, and prime(...).

Priming makes every test start from the same state, because the CCP takes one chat at a time and a test that stopped
early leaves its customer's chat queued, to be offered before the next test's own chat:
  1. wait for the CCP to load; sign in with CCP_AGENTCHATUI_USERNAME and CCP_AGENTCHATUI_PASSWORD if it asks
  2. set the status to Available, so queued leftover chats arrive now
  3. accept every offered chat, end it, and close the contact (a rejected or missed chat only comes back), until
     none arrives for --quiet seconds
It leaves the agent Available and idle. examples/multi_tab.py primes every agent tab this way before the agent's
first action. The agent workspace embeds this same CCP, so examples/agent_workspace.py reuses the primer. Priming is
a test fixture: it clicks this site's controls by their exact labels and is not the agent, which never gets
site-specific steps. Every primed click is printed and returned for the run log.
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import chat_widget  # examples/ is this script's folder, so its sibling personas are importable

from laya_ultrafast import credentials
from laya_ultrafast.browser import Browser, StalePage
from laya_ultrafast.laya import observed

NAME = "agent chat UI"
URL = "https://spenlep.my.connect.aws/ccp-v2"
STATUSES = {"Available", "Offline"}  # this instance's agent statuses; the status button shows the current one
# Rejecting or missing a chat puts it back in the queue, so a leftover is cleared by accepting, ending and closing it.
LEFTOVERS = ("Accept chat", "End chat", "Close contact", "Remove Missed chat")
LOGIN = {"username": "{{CCP_AGENTCHATUI_USERNAME}}", "password": "{{CCP_AGENTCHATUI_PASSWORD}}"}


def connect_page(url, path):
    host, page = urlparse(url).hostname or "", urlparse(url).path
    return host.endswith(".my.connect.aws") and (page == path or page.startswith(path + "/"))


def matches(url):
    return connect_page(url, "/ccp-v2")


def probe(browser, page, step, status):
    """The page's own text plus every frame's (the CCP's views are same-origin iframes), fields and elements."""
    record = chat_widget.probe(browser, page, step, status)
    try:
        record["frame_texts"].insert(0, browser.evaluate("document.body?.innerText ?? ''"))
    except StalePage:
        pass
    return record


def prime(browser, say=print, quiet=15):
    """Signed in, every leftover contact cleared, Available. Returns what it did; raises RuntimeError if it cannot."""
    return Primer(browser, say).run(quiet=quiet)


class Primer:
    def __init__(self, browser, say=print):
        self.browser, self.say, self.done = browser, say, []

    def controls(self):
        for _ in range(5):
            try:
                return observed(self.browser.observe(screenshot=False))
            except StalePage:
                time.sleep(0.3)
            except RuntimeError as error:  # Chrome dropped the tab's session: the tab was closed or crashed
                raise RuntimeError(f"the agent UI tab is gone ({error})") from None
        return []

    def find(self, label=None, role=None, secret=None):
        return next((e for e in self.controls() if (label is None or e["label"] == label)
                     and (role is None or e["role"] == role) and (secret is None or e["secret"] == secret)), None)

    def act(self, e, text=None, what=None):
        """Act on a control of a fresh observation. Returns False if it changed before input (nothing happened)."""
        for _ in range(3):
            page = self.browser.observe(screenshot=False)
            current = next((c for c in observed(page) if c["label"] == e["label"] and c["role"] == e["role"]), None)
            if current is None:
                return False
            kind = "fill" if text is not None else "click"
            try:
                self.browser.act(current["actions"][kind], page,
                                 text=credentials.prepare(text, current["actions"][kind]) if text else None)
            except StalePage:
                time.sleep(0.3)
                continue
            self.done.append(what or f"{kind} {e['label']!r}")
            self.say(f"prime: {self.done[-1]}")
            return True
        return False

    def status(self):
        return next((e for e in self.controls() if e["role"] == "button" and e["label"] in STATUSES), None)

    def wait_for(self, check, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if result := check():
                return result
            time.sleep(0.5)
        return None

    def sign_in(self):
        user = self.find(role="textbox", secret=False)
        if not user or not credentials.references(LOGIN["username"]):
            return False
        self.act(user, LOGIN["username"], "type the username")
        if nxt := self.find("Next"):
            self.act(nxt)
        password = self.wait_for(lambda: self.find(secret=True), 15)
        if not password:
            return False
        self.act(password, LOGIN["password"], "type the password")
        if button := self.find("Sign in"):
            self.act(button)
        return True

    def run(self, quiet=15, load=45):
        """Prime the CCP. Returns the list of what it did; raises RuntimeError if it cannot."""
        ready = self.wait_for(lambda: self.status() or self.find(role="textbox", secret=False), load)
        if ready and ready["role"] == "textbox":
            self.sign_in()
            ready = self.wait_for(self.status, load)
        if not ready:
            raise RuntimeError("The agent UI did not load its status control (not signed in?)")
        if self.status()["label"] != "Available":
            self.act(self.status())
            item = self.wait_for(lambda: self.find("Available", "menuitem"), 5)
            if not item or not self.act(item, what="set the status to Available"):
                raise RuntimeError("Could not set the status to Available")
        quiet_since = time.monotonic()
        while time.monotonic() - quiet_since < quiet:
            labels = {e["label"]: e for e in self.controls()}
            leftover = next((labels[label] for label in LEFTOVERS if label in labels), None)
            if leftover and self.act(leftover, what=f"clear a leftover contact: {leftover['label']!r}"):
                quiet_since = time.monotonic()
                time.sleep(1.5)
                continue
            time.sleep(1)
        status = self.status()
        if not status or status["label"] != "Available":
            raise RuntimeError(f"Status is {status and status['label']!r} after priming, not 'Available'")
        self.say(f"prime: idle and Available (no contact for {quiet:g} s)")
        return self.done


def main(name=NAME, url=URL):
    """Prime the page; with --goal, then run the goal on it as a one-tab examples/multi_tab.py run (primed again right
    before its first action, with its checks, log and video)."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=url)
    parser.add_argument("--quiet", type=float, default=15, help="seconds without a contact before it counts as clean")
    parser.add_argument("--goal", help="then run this goal on this page alone")
    parser.add_argument("--expect", action="append", default=[], help="with --goal: text the page must show")
    args = parser.parse_args()
    if args.goal:
        run = [sys.executable, str(Path(__file__).with_name("multi_tab.py")), "--tab", f"{name}={args.url}",
               "--goal", args.goal, *[f"--expect={name}={text}" for text in args.expect]]
        raise SystemExit(subprocess.call(run))
    browser = Browser(args.url)
    try:
        prime(browser, quiet=args.quiet)
    except RuntimeError as error:
        raise SystemExit(f"prime failed: {error}") from None
    finally:
        try:
            browser.close()
        except RuntimeError:
            pass  # already gone


if __name__ == "__main__":
    main()
