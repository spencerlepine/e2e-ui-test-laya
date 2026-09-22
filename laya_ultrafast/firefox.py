"""The same observed actions in Firefox, over WebDriver BiDi instead of CDP.

FirefoxBrowser keeps Browser's contract and reuses its frame merging, freshness checks and snapshot script. Only the
transport differs:
- Each run launches its own Firefox (bidi.Firefox); HEADLESS_MODE=true starts it headless, false shows its window.
- Every frame is a BiDi browsing context. A cross-origin <iframe> maps to its context through its contentWindow.
- Input for an element in a cross-origin frame goes to that frame's own context, in the frame's coordinates, after the
  same hit-tests through every ancestor. Firefox delivers top-level pointer input only to the top process's
  documents (the page and its same-origin frames), not into cross-origin frames.
"""

import json
import os
import sys
import time

from .bidi import Firefox
from .browser import AFTER_INPUT, READ_STATE, RESOLVE, Browser, StalePage, fingerprint, valid_node

META = "\ue03d" if sys.platform == "darwin" else "\ue009"  # WebDriver key values for Command / Control
JOINERS = {"\u200d", "\ufe0e", "\ufe0f"}  # zero-width joiner and emoji variation selectors
DELETE = "\ue017"


def graphemes(text):
    """Split text into the key presses BiDi accepts: one character each, keeping emoji sequences together."""
    keys = []
    for ch in text:
        if keys and (ch in JOINERS or keys[-1].endswith("\u200d") or 0x1F3FB <= ord(ch) <= 0x1F3FF):
            keys[-1] += ch
        else:
            keys.append(ch)
    return keys


class FirefoxBrowser(Browser):
    kind = "firefox"

    def __init__(self, url):
        self.frames = {}  # context id -> {"host": parent frame context or None, "owner": its <iframe> node}
        self.nodes = {}  # negative node -> (frame context, document time origin, node in that document)
        self.ids = {}
        self.target = None
        headless = os.environ.get("HEADLESS_MODE", "true").strip().lower() != "false"
        self.firefox = Firefox(headless=headless)
        self.version = self.firefox.version
        try:
            self.target = self.firefox.call("browsingContext.getTree", maxDepth=0)["contexts"][0]["context"]
            self.session = self.target  # Browser's name for the page's handle
            self.call("browsingContext.setViewport", context=self.target,
                      viewport={"width": 1120, "height": 780}, devicePixelRatio=1)
            self.call("browsingContext.navigate", context=self.target, url=url, wait="none")
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                try:
                    if self.evaluate("document.readyState") == "complete":
                        break
                except StalePage:
                    pass  # the first document is being replaced
                time.sleep(0.02)
        except Exception:
            self.close()
            raise

    def call(self, method, **params):
        return self.firefox.call(method, **params)

    def evaluate(self, expression):
        return self.frame_evaluate(None, expression)

    def frame_evaluate(self, frame, expression, **_params):
        """Evaluate in a frame's own document; the value comes back as JSON. Promises are awaited."""
        context = self.target if frame is None else frame
        wrapped = (f"(async () => {{ const v = await ({expression}); "
                   "return v === undefined ? null : JSON.stringify(v) })()")
        try:
            response = self.call("script.evaluate", target={"context": context}, expression=wrapped, awaitPromise=True)
        except RuntimeError:
            if frame is not None:
                self.frames.pop(frame, None)
            raise StalePage("Frame detached or navigated") from None
        if response["type"] != "success":
            raise StalePage("Document changed during evaluation")
        value = response["result"].get("value")
        return None if value is None else json.loads(value)

    # Frames the page cannot reach ---------------------------------------------------------------------------

    def locate(self, host, owners):
        """Map each listed <iframe> node in `host`'s document to its browsing context, in one call."""
        for frame, rec in list(self.frames.items()):
            if rec["host"] == host and rec["owner"] not in owners:
                self.forget(frame)
        known = {rec["owner"]: frame for frame, rec in self.frames.items() if rec["host"] == host}
        missing = [owner for owner in owners if owner not in known]
        if not missing:
            return known
        try:
            response = self.call(
                "script.evaluate", target={"context": self.target if host is None else host}, awaitPromise=False,
                expression=f"{json.dumps(missing)}.map(id => window.__jevFast?.nodes.get(id)?.contentWindow ?? null)",
            )
        except RuntimeError:
            raise StalePage("Frame detached or navigated") from None
        if response["type"] != "success":
            raise StalePage("Document changed during evaluation")
        for owner, window in zip(missing, response["result"]["value"]):
            if window.get("type") == "window":
                context = window["value"]["context"]
                self.frames[context] = {"host": host, "owner": owner}
                known[owner] = context
        return known

    def forget(self, frame):
        self.frames.pop(frame, None)
        for child in [f for f, r in self.frames.items() if r["host"] == frame]:
            self.forget(child)

    # Observation and execution ----------------------------------------------------------------------------

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            frame, node = None, action.get("node")
            if valid_node(node) and node < 0 and node in self.nodes and self.nodes[node][0] in self.frames:
                frame, _, node = self.nodes[node]
            try:  # read-only, after execution was logged
                self.frame_evaluate(frame, AFTER_INPUT + "(" + json.dumps({**action, "node": node}) + ")")
            except (RuntimeError, StalePage):
                pass
        for attempt in range(10):
            try:
                info = self.evaluate(READ_STATE)
                if info is None:
                    raise StalePage("Document is navigating")
                info["fingerprint"] = fingerprint(info)
                if screenshot:
                    info["screenshot"] = self.call("browsingContext.captureScreenshot", context=self.target,
                                                   format={"type": "image/jpeg", "quality": 0.72})["data"]
                if info["frames"]:
                    self.read_frames(info)
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        kind = action["kind"]
        if kind == "wait":
            time.sleep(0.1)
        elif kind == "scroll":
            scroll = {"type": "scroll", "x": 550, "y": 650, "deltaX": 0, "deltaY": action["delta"]}
            self.call("input.performActions", context=self.target,
                      actions=[{"type": "wheel", "id": "wheel", "actions": [scroll]}])
        elif valid_node(action.get("node")) and action["node"] < 0:
            self.frame_act(action, text)
        else:
            if not valid_node(action.get("node")):
                raise ValueError("Invalid observed node")
            try:
                target = self.evaluate(RESOLVE + "(" + json.dumps(action) + ")")
            except StalePage:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.") from None
                raise
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                self.press(self.target, kind, target, text)
        self.after_input = action if kind != "wait" else None
        return {"executed": action["id"]}

    def frame_act(self, action, text):
        """Resolve the node in its frame and hit-test it through each cross-origin ancestor, then send the input to
        the frame itself, at the point in the frame's own viewport."""
        kind = action["kind"]
        frame, _, local = self.nodes[action["node"]]
        point = self.frame_evaluate(frame, RESOLVE + "(" + json.dumps({**action, "node": local, "commit": False}) + ")")
        lifted = point
        for child in self.chain(frame):
            if lifted is None:
                break
            rec = self.frames[child]
            lifted = self.frame_evaluate(rec["host"], "window.__jevFast?.hop(window.__jevFast.nodes.get("
                                         f"{rec['owner']}),{lifted['x']},{lifted['y']}) ?? null")
        if lifted is None:
            raise StalePage("Target changed or is covered. Observe again.")
        if kind == "select":
            try:
                done = self.frame_evaluate(frame, RESOLVE + "(" + json.dumps({**action, "node": local}) + ")")
            except StalePage:
                done = None
            if done is None:
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
        else:
            self.press(frame, kind, point, text)
        return {"executed": action["id"]}

    def press(self, context, kind, point, text):
        """Click the point; for a field, select its contents and type the text in their place."""
        x, y = round(point["x"]), round(point["y"])
        actions = [{"type": "pointer", "id": "mouse", "parameters": {"pointerType": "mouse"}, "actions": [
            {"type": "pointerMove", "x": x, "y": y}, {"type": "pointerDown", "button": 0},
            {"type": "pointerUp", "button": 0}]}]
        self.call("input.performActions", context=context, actions=actions)
        if kind == "fill":
            keys = [{"type": "keyDown", "value": META}, {"type": "keyDown", "value": "a"},
                    {"type": "keyUp", "value": "a"}, {"type": "keyUp", "value": META}]
            if text:
                keys += [a for key in graphemes(text) for a in ({"type": "keyDown", "value": key},
                                                                {"type": "keyUp", "value": key})]
            else:
                keys += [{"type": "keyDown", "value": DELETE}, {"type": "keyUp", "value": DELETE}]
            self.call("input.performActions", context=context, actions=[{"type": "key", "id": "keys", "actions": keys}])
        self.call("input.releaseActions", context=context)

    def close(self):
        if getattr(self, "firefox", None):
            self.firefox.quit()
            self.firefox = None
        self.target = None

