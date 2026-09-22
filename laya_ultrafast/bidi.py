"""A private Firefox driven over WebDriver BiDi: one Firefox process, profile and connection per test run.

Firefox has no CDP (removed in Firefox 141), so Browser Harness cannot drive it. Each run launches the installed
Firefox with a throwaway profile whose prefs replace the test Chrome's flags: fake microphone and camera with no
prompt, notifications and location denied. Parallel runs never share a browser, a session or a downloads folder.
"""

import atexit
import itertools
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

FIREFOX = os.environ.get("FIREFOX_BINARY", "/Applications/Firefox.app/Contents/MacOS/firefox")
PREFS = {
    # Pages get a synthetic microphone and camera without a prompt; always flip these two together.
    "media.navigator.streams.fake": True,
    "media.navigator.permission.disabled": True,
    # Every other permission prompt is denied without asking.
    "permissions.default.desktop-notification": 2,
    "permissions.default.geo": 2,
    "permissions.default.xr": 2,
    # Quiet startup, no updates or telemetry.
    "browser.shell.checkDefaultBrowser": False,
    "browser.aboutwelcome.enabled": False,
    "browser.startup.homepage_override.mstone": "ignore",
    "browser.startup.page": 0,  # a blank first tab: about:home is privileged and refuses automation
    "browser.startup.homepage": "about:blank",
    "browser.newtabpage.enabled": False,
    "datareporting.policy.dataSubmissionEnabled": False,
    "app.update.auto": False,
    # startScreencast writes to the downloads folder; this one is inside the throwaway profile.
    "browser.download.folderList": 2,
}
LISTENING = re.compile(r"WebDriver BiDi listening on (ws://\S+)")


class BiDiError(RuntimeError):
    """A command failed. A RuntimeError, like a failed CDP call, so callers treat both alike."""

    def __init__(self, error, message):
        super().__init__(f"{error}: {message}")
        self.error = error


class Firefox:
    """Launch Firefox, open a BiDi session, and quit it again. call() is safe to use from several threads."""

    def __init__(self, headless=True, timeout=30):
        from websockets.sync.client import connect

        if not Path(FIREFOX).exists():
            raise RuntimeError(f"Firefox not found at {FIREFOX}; install it (brew install --cask firefox)")
        self.profile = Path(tempfile.mkdtemp(prefix="laya-firefox-"))
        self.downloads = self.profile / "downloads"
        self.downloads.mkdir()
        prefs = {**PREFS, "browser.download.dir": str(self.downloads)}
        (self.profile / "user.js").write_text(
            "".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n" for k, v in prefs.items())
        )
        command = [FIREFOX, "-no-remote", "-profile", str(self.profile), "--remote-debugging-port", "0"]
        if headless:
            command.append("-headless")
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        found, url = threading.Event(), []

        def drain():  # read stderr until exit, so the pipe never fills; the first matching line has the endpoint
            for line in self.process.stderr:
                if not found.is_set() and (match := LISTENING.search(line)):
                    url.append(match.group(1))
                    found.set()
            found.set()

        threading.Thread(target=drain, daemon=True).start()
        atexit.register(self.quit)  # a run that exits without closing its agent still quits its Firefox
        self.ws, self.ids, self.lock = None, itertools.count(1), threading.Lock()
        try:
            if not found.wait(timeout) or not url:
                raise RuntimeError("Firefox did not start its WebDriver BiDi endpoint")
            self.ws = connect(url[0] + "/session", max_size=None, open_timeout=10)
            self.version = self.call("session.new", capabilities={})["capabilities"]["browserVersion"]
        except Exception:
            self.quit()
            raise

    def call(self, method, timeout=30, **params):
        with self.lock:
            request = next(self.ids)
            self.ws.send(json.dumps({"id": request, "method": method, "params": params}))
            while True:
                message = json.loads(self.ws.recv(timeout=timeout))
                if message.get("id") != request:
                    continue  # an event; this driver subscribes to none it needs
                if message.get("type") == "error":
                    raise BiDiError(message.get("error"), message.get("message", ""))
                return message["result"]

    def quit(self):
        if self.process.poll() is not None and not self.profile.exists():
            return  # already quit
        try:
            if self.ws:
                self.call("browser.close", timeout=5)
        except Exception:
            pass  # already gone
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
        shutil.rmtree(self.profile, ignore_errors=True)
