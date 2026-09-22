"""Observed actions through Browser Harness; one CDP session, no per-step subprocess."""

import hashlib
import json
import os
import sys
import time
from pathlib import Path

from browser_harness.admin import ensure_daemon
from browser_harness.helpers import cdp

# Atomically read visible content and controls, preserving actual DOM node identity.
READ_STATE = Path(__file__).with_name("snapshot.js").read_text()
MARKER = f"(() => {{ const state={READ_STATE}; return state?.marker ?? null; }})()"
# Resolve an observed node just before input: visible, enabled, unoccluded in its own document and in every
# same-origin ancestor frame. Returns the point in this document's top viewport. A dropdown changes last.
RESOLVE = """(action => {
  const c=window.__jevFast, e=c?.nodes.get(action.node);
  if (!e?.isConnected || e.matches(':disabled') || e.closest('[aria-disabled="true"],[inert]') ||
      !e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true})) return null;
  if (action.kind==='fill' && (e.readOnly || e.getAttribute('aria-readonly')==='true')) return null;
  const w=e.ownerDocument.defaultView, r=e.getBoundingClientRect(), x=r.x+r.width/2, y=r.y+r.height/2;
  if (!r.width || !r.height || x<0 || y<0 || x>=w.innerWidth || y>=w.innerHeight) return null;
  if (!e.contains(e.ownerDocument.elementFromPoint(x,y))) return null;
  const point=c.lift(w,x,y);
  if (!point) return null;
  if (action.kind==='select') {
    if (e.tagName!=='SELECT' || ![...e.options].some(o=>o.value===action.value &&
        !o.disabled && !o.closest('optgroup[disabled]'))) return null;
    if (action.commit===false) return point;
    e.value=action.value;
    e.dispatchEvent(new Event('input',{bubbles:true}));
    e.dispatchEvent(new Event('change',{bubbles:true}));
  }
  return point;
})"""
# After input, wait briefly for the field's autocomplete options, in the field's own document.
AFTER_INPUT = """(action => new Promise(resolve => {
  const field=window.__jevFast?.nodes.get(action.node), doc=field?.ownerDocument ?? document;
  const autocomplete=action.kind==='fill' && field?.getAttribute('role')==='combobox';
  let frames=0, stopped=false;
  const finish=()=>{stopped=true;resolve()};
  setTimeout(finish,autocomplete ? 200 : 50);
  const ready=()=>{
    if (stopped) return;
    const ids=(field?.getAttribute('aria-controls')||field?.getAttribute('aria-owns')||'')
      .split(/\\s+/).filter(Boolean);
    const roots=ids.length ? ids.map(id=>doc.getElementById(id)).filter(Boolean) : [doc];
    const options=roots.flatMap(root=>[...root.querySelectorAll('[role="option"]')]);
    if (++frames>=2 && (!autocomplete || options.some(e=>{
      const r=e.getBoundingClientRect();
      return r.width && r.height && r.bottom>0 && r.top<doc.defaultView.innerHeight &&
        e.checkVisibility({checkOpacity:true,checkVisibilityCSS:true});
    }))) finish();
    else requestAnimationFrame(ready);
  };
  requestAnimationFrame(ready);
}))"""


class StalePage(ValueError):
    """A decision no longer refers to the observed page."""


def valid_node(node):
    """Observed nodes are code-owned ints: positive in the page's own document (including same-origin frames),
    negative for a document in a frame the page cannot reach, mapped by Browser to (frame, document, node)."""
    return type(node) is int and node != 0


class Browser:
    def __init__(self, url):
        ensure_daemon()
        self.frames = {}  # frame id -> {"host": parent frame id or None, "owner": its <iframe> node, context}
        self.nodes = {}  # negative node -> (frame id, document time origin, node in that document)
        self.ids = {}
        # HEADLESS_MODE=false opens the agent's tab in front, so a person can watch it work. The default keeps it
        # in the background of the user's Chrome window. Browser Harness always drives a real (headed) Chrome.
        watch = os.environ.get("HEADLESS_MODE", "true").strip().lower() == "false"
        self.target = cdp("Target.createTarget", url="about:blank", background=not watch)["targetId"]
        if watch:
            cdp("Target.activateTarget", targetId=self.target)
        try:
            self.session = cdp("Target.attachToTarget", targetId=self.target, flatten=True)["sessionId"]
            self.call("Emulation.setDeviceMetricsOverride", width=1120, height=780, deviceScaleFactor=1, mobile=False)
            # Keep rAF/menus rendering in an owned background tab, without activating the user's Chrome tab.
            self.call("Emulation.setFocusEmulationEnabled", enabled=True)
            # A browser's first network navigation can take several seconds (a fresh test Edge profile: up to ~13 s).
            # Wait for this one command longer than Browser Harness's 5 s default; it is sent once, never retried.
            self.call("Page.navigate", url=url, _response_timeout=30)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if self.evaluate("document.readyState") == "complete":
                    break
                time.sleep(0.02)
        except Exception:
            self.close()  # never leave a half-opened tab behind in the test browser
            raise

    def call(self, method, **params):
        return cdp(method, session_id=self.session, **params)

    def evaluate(self, expression):
        response = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        if response.get("exceptionDetails"):
            raise StalePage("Document changed during evaluation")
        return response.get("result", {}).get("value")

    # Frames the page cannot reach ---------------------------------------------------------------------------

    def context(self, frame):
        return {"session": self.session, "context": None} if frame is None else self.frames[frame]

    def frame_evaluate(self, frame, expression, **params):
        """Evaluate in a frame's own document. A detached session or destroyed context means the frame changed."""
        ctx = self.context(frame)
        if ctx["context"] is not None:
            params["contextId"] = ctx["context"]
        try:
            response = cdp("Runtime.evaluate", session_id=ctx["session"], expression=expression,
                           returnByValue=True, **params)
        except RuntimeError:
            self.frames.pop(frame, None)
            raise StalePage("Frame detached or navigated") from None
        if response.get("exceptionDetails"):
            raise StalePage("Frame changed during evaluation")
        return response.get("result", {}).get("value")

    def locate(self, host, owners):
        """Find the frame behind each listed <iframe> node in `host`'s document. Known frames cost nothing; a new
        one costs a frame tree, a target list, and three calls to name its <iframe>."""
        for frame, rec in list(self.frames.items()):
            if rec["host"] == host and rec["owner"] not in owners:
                self.forget(frame)  # its <iframe> is gone or no longer visible
        known = {rec["owner"]: frame for frame, rec in self.frames.items() if rec["host"] == host}
        if set(owners) <= set(known):
            return known
        ctx = self.context(host)
        session = ctx["session"]
        try:
            tree = cdp("Page.getFrameTree", session_id=session)["frameTree"]
        except RuntimeError:
            raise StalePage("Frame detached or navigated") from None
        hosted, stack = [], [tree]
        while stack:
            node = stack.pop()
            hosted.append(node["frame"]["id"])
            stack.extend(node.get("childFrames", []))
        # A cross-site frame is its own target. A cross-origin frame of the same site shares this process.
        candidates = [(frame, False) for frame in hosted[1:]]
        candidates += [
            (t["targetId"], True) for t in cdp("Target.getTargets")["targetInfos"]
            if t["type"] == "iframe" and t.get("parentFrameId") in hosted
        ]
        extra = {"executionContextId": ctx["context"]} if ctx["context"] is not None else {}
        for frame, own_target in candidates:
            if frame in self.frames:
                continue
            try:
                backend = cdp("DOM.getFrameOwner", session_id=session, frameId=frame)["backendNodeId"]
                obj = cdp("DOM.resolveNode", session_id=session, backendNodeId=backend,
                          objectGroup="laya-frames", **extra)["object"]["objectId"]
                owner = cdp("Runtime.callFunctionOn", session_id=session, objectId=obj, returnByValue=True,
                            functionDeclaration="function(){return window.__jevFast?.ids.get(this) ?? null}",
                            )["result"].get("value")
            except (RuntimeError, KeyError):
                continue
            if owner not in owners:
                continue  # a same-origin frame read by the snapshot, or one not visible
            if own_target:
                child = {"session": cdp("Target.attachToTarget", targetId=frame, flatten=True)["sessionId"],
                         "context": None}
            else:
                child = {"session": session, "context": cdp(
                    "Page.createIsolatedWorld", session_id=session, frameId=frame, worldName="laya-ultrafast",
                )["executionContextId"]}
            self.frames[frame] = {"host": host, "owner": owner, **child}
            known[owner] = frame
        cdp("Runtime.releaseObjectGroup", session_id=session, objectGroup="laya-frames")
        return known

    def forget(self, frame):
        rec = self.frames.pop(frame, None)
        for child in [f for f, r in self.frames.items() if r["host"] == frame]:
            self.forget(child)
        if rec and rec["context"] is None:
            try:
                cdp("Target.detachFromTarget", sessionId=rec["session"])
            except RuntimeError:
                pass  # Chrome already dropped it with the frame

    def node_id(self, frame, origin, node):
        key = (frame, origin, node)
        if key not in self.ids:
            self.ids[key] = -(len(self.ids) + 1)
            self.nodes[self.ids[key]] = key
        return self.ids[key]

    def read_frames(self, info):
        """Merge the snapshots of frames the page cannot reach, in top-level coordinates."""
        controls = [a for a in info["actions"] if a["kind"] not in {"click", "fill", "select"}]
        actions = [a for a in info["actions"] if a["kind"] in {"click", "fill", "select"}]
        texts, states = [info["text"]], {}
        pending = [(None, (0, 0), None, info["frames"])]
        while pending:
            host, offset, clip, frames = pending.pop(0)
            located = self.locate(host, [f["node"] for f in frames])
            for listed in frames:
                frame = located.get(listed["node"])
                if frame is None:
                    continue  # not attached yet; its <iframe> in page_key changes once it is
                sub = self.frame_evaluate(frame, READ_STATE)
                if sub is None:
                    raise StalePage("Frame is navigating")
                box = shift(listed["rect"], *offset)
                inner = clip_box(listed["clip"], offset, clip)
                kept, guards = place(sub, box, inner, lambda n, f=frame, o=sub["page_key"][0]: self.node_id(f, o, n))
                actions += kept
                info["guards"].update(guards)
                texts.append(sub["text"])
                states[frame] = {"page_key": sub["page_key"], "marker": sub["marker"]}
                pending.append((frame, (box["x"], box["y"]), inner, sub["frames"]))
        info["omitted_actions"] += max(0, len(actions) - 250)
        actions = actions[:250]
        for i, action in enumerate(actions):
            action["id"] = f"e{i + 1}"
        info["actions"] = actions + controls
        info["text"] = "\n".join(t for t in texts if t)[:6000]
        info["frame_states"] = states
        info["fingerprint"] = fingerprint(info)

    def chain(self, frame):
        frames = []
        while frame is not None:
            frames.append(frame)
            frame = self.frames[frame]["host"]
        return frames

    # Observation, freshness, execution --------------------------------------------------------------------

    def observe(self, screenshot=True):
        if getattr(self, "after_input", None):
            action, self.after_input = self.after_input, None
            frame, node = None, action.get("node")
            if valid_node(node) and node < 0 and node in self.nodes and self.nodes[node][0] in self.frames:
                frame, _, node = self.nodes[node]
            # This is read-only and happens after execution was logged, even if navigation interrupts it.
            try:
                self.frame_evaluate(frame, AFTER_INPUT + "(" + json.dumps({**action, "node": node}) + ")",
                                    awaitPromise=True)
            except (RuntimeError, StalePage):
                pass
        for attempt in range(10):
            try:
                info = browser_operation(
                    {"operation": "observe", "session": self.session, "screenshot": screenshot}
                )
                if info["frames"]:
                    self.read_frames(info)
                return info
            except StalePage:
                if attempt == 9:
                    raise
                time.sleep(0.02)
        raise StalePage("Page did not settle")

    def fresh(self, page, action=None):
        if action is not None and action["kind"] in {"click", "select"}:
            node = action["node"]
            if not valid_node(node):
                return False
            if node < 0:
                return self.frame_fresh(page, node)
            current = self.evaluate(
                "(() => { const c=window.__jevFast; "
                f"return c ? [c.pageKey(),c.guard(c.nodes.get({node}))] : null; }})()"
            )
            return current == [page["page_key"], page["guards"].get(str(node))]
        if self.evaluate(MARKER) != page["marker"]:
            return False
        try:
            return all(frame in self.frames and self.frame_evaluate(frame, MARKER) == state["marker"]
                       for frame, state in page.get("frame_states", {}).items())
        except StalePage:
            return False

    def frame_fresh(self, page, node):
        """The page and every frame between it and the node are unchanged, and so is the node itself."""
        states = page.get("frame_states", {})
        if node not in self.nodes or self.nodes[node][0] not in self.frames:
            return False
        frame, _, local = self.nodes[node]
        try:
            if self.evaluate("window.__jevFast?.pageKey()") != page["page_key"]:
                return False
            for ancestor in self.chain(frame):
                target = local if ancestor == frame else 0
                current = self.frame_evaluate(
                    ancestor, f"(() => {{ const c=window.__jevFast; return c ? [c.pageKey(),"
                    f"c.guard(c.nodes.get({target}))] : null; }})()"
                )
                expected = states.get(ancestor, {}).get("page_key")
                if current is None or current[0] != expected:
                    return False
                if ancestor == frame and current[1] != page["guards"].get(str(node)):
                    return False
        except StalePage:
            return False
        return True

    def act(self, action, page, text=None):
        if not self.fresh(page, action):
            raise StalePage("Page changed since this decision. Observe again.")
        if action["kind"] == "wait":
            time.sleep(0.1)
        if action["kind"] in {"click", "fill", "select"} and valid_node(action["node"]) and action["node"] < 0:
            result = self.frame_act(action, text)
        else:
            result = browser_operation({"operation": "act", "session": self.session, "action": action, "text": text})
        self.after_input = action if action["kind"] != "wait" else None
        return result

    def frame_act(self, action, text):
        """Resolve the node in its frame, lift the point through each cross-origin ancestor (hit-testing its
        <iframe>), then send input to the page. A dropdown in such a frame changes only after all checks."""
        kind = action["kind"]
        frame, _, local = self.nodes[action["node"]]
        point = self.frame_evaluate(frame, RESOLVE + "(" + json.dumps(
            {**action, "node": local, "commit": False}) + ")")
        for child in self.chain(frame):
            if point is None:
                break
            rec = self.frames[child]
            point = self.frame_evaluate(rec["host"], "window.__jevFast?.hop(window.__jevFast.nodes.get("
                                        f"{rec['owner']}),{point['x']},{point['y']}) ?? null")
        if point is None:
            raise StalePage("Target changed or is covered. Observe again.")
        if kind == "select":
            try:
                done = self.frame_evaluate(frame, RESOLVE + "(" + json.dumps({**action, "node": local}) + ")")
            except StalePage:
                done = None
            if done is None:
                raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
        else:
            press(self.call, kind, point["x"], point["y"], text)
        return {"executed": action["id"]}

    def close(self):
        if self.target:
            cdp("Target.closeTarget", targetId=self.target)
            self.target = None


def shift(rect, dx, dy):
    return {**rect, "x": rect["x"] + dx, "y": rect["y"] + dy}


def clip_box(clip, offset, outer):
    """A frame's visible area in top-level coordinates, inside its parent frame's visible area."""
    left, top, right, bottom = clip[0] + offset[0], clip[1] + offset[1], clip[2] + offset[0], clip[3] + offset[1]
    if outer:
        left, top, right, bottom = max(left, outer[0]), max(top, outer[1]), min(right, outer[2]), min(bottom, outer[3])
    return [left, top, right, bottom]


def place(sub, box, clip, qualify):
    """Move a frame snapshot's actions into top-level coordinates. Elements whose center is outside the frame's
    visible area are dropped. Node IDs become the page's own frame-qualified IDs."""
    kept, guards = [], {}
    for action in sub["actions"]:
        if action["kind"] not in {"click", "fill", "select"}:
            continue
        rect = shift(action["rect"], box["x"], box["y"])
        x, y = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
        if not (clip[0] <= x < clip[2] and clip[1] <= y < clip[3]):
            continue
        node = qualify(action["node"])
        guards[str(node)] = sub["guards"].get(str(action["node"]))
        kept.append({**action, "rect": rect, "node": node})
    return kept, guards


def press(call, kind, x, y, text):
    for event in ("mousePressed", "mouseReleased"):
        call("Input.dispatchMouseEvent", type=event, x=x, y=y, button="left", clickCount=1)
    if kind == "fill":
        call(
            "Input.dispatchKeyEvent",
            type="keyDown",
            key="a",
            code="KeyA",
            modifiers=4 if sys.platform == "darwin" else 2,
            commands=["selectAll"],
        )
        call(
            "Input.dispatchKeyEvent",
            type="keyUp",
            key="a",
            code="KeyA",
            modifiers=4 if sys.platform == "darwin" else 2,
        )
        call("Input.insertText", text=text)


def fingerprint(state):
    content = {k: state[k] for k in ("url", "text", "actions", "scroll")}
    return hashlib.sha256(json.dumps(content, sort_keys=True).encode()).hexdigest()


def browser_operation(request):
    operation = request["operation"]
    session = request["session"]

    def call(method, **params):
        return cdp(method, session_id=session, **params)

    def evaluate(expression):
        result = call("Runtime.evaluate", expression=expression, returnByValue=True)
        if result.get("exceptionDetails"):
            if operation == "act" and request["action"]["kind"] == "select":
                raise RuntimeError("Dropdown execution was interrupted; inspect before retrying.")
            raise StalePage("Document changed during evaluation")
        return result.get("result", {}).get("value")

    if operation == "act":
        action = request["action"]
        kind = action["kind"]
        if kind == "scroll":
            call("Input.dispatchMouseEvent", type="mouseWheel", x=550, y=650, deltaX=0, deltaY=action["delta"])
        elif kind != "wait":
            if not valid_node(action["node"]) or action["node"] < 0:
                raise ValueError("Invalid observed node")
            # Code-owned node IDs refer to actual observed elements, never model-generated selectors.
            target = evaluate(RESOLVE + "(" + json.dumps(action) + ")")
            if target is None:
                if kind == "select":
                    raise RuntimeError("Dropdown execution was not confirmed; inspect before retrying.")
                raise StalePage("Target changed or is covered. Observe again.")
            if kind != "select":
                press(call, kind, target["x"], target["y"], request["text"])
        return {"executed": action["id"]}

    info = evaluate(READ_STATE)
    if info is None:
        raise StalePage("Document is navigating")
    info["fingerprint"] = fingerprint(info)
    if request.get("screenshot", True):
        info["screenshot"] = call("Page.captureScreenshot", format="jpeg", quality=72)["data"]
    return info
