"""Local-browser iframe regressions: same-origin, cross-site (OOPIF), same-site cross-origin, nested, late,
clipped, covered and stale frames. No model calls or external websites.

One local server answers on three hosts: 127.0.0.1 (the page), localhost (another site, so Chrome runs its frames
out of process) and a.localhost (the same site as localhost, another origin: in process, but unreachable).

uv run python scripts/check_frames.py            # all checks
uv run python scripts/check_frames.py --count    # CDP calls per observe/fresh/act on a page without frames
"""

import argparse
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, urlparse

import laya_ultrafast.browser as browser_module
from laya_ultrafast.browser import Browser, StalePage

CONTROLS = """<!doctype html><title>{tag}</title>
<style>body{{margin:8px}} button,input,select{{display:block;width:140px;height:30px;margin:4px 0}}</style>
<div style="height:{pad}px"></div>
<button id="b" onclick="window.clicks=(window.clicks||0)+1">{tag} button</button>
<input id="i" aria-label="{tag} input">
<select id="s" aria-label="{tag} select"><option>One</option><option>Two</option></select>
<p>{tag} text</p><div style="height:600px"></div>"""

NEST = """<!doctype html><title>nest</title><body style="margin:0">
<p>nest text</p>
<iframe id="deep" src="http://127.0.0.1:{port}/controls?tag=deep"
  style="position:absolute;left:30px;top:40px;width:220px;height:200px;border:3px solid"></iframe>
<iframe id="sub" src="http://a.localhost:{port}/controls?tag=sub"
  style="position:absolute;left:270px;top:40px;width:220px;height:200px;border:6px solid;padding:4px"></iframe>
</body>"""

PARENT = """<!doctype html><title>Frames</title><body style="margin:0">
<button id="top" onclick="window.clicks=(window.clicks||0)+1" style="width:120px;height:30px">Top button</button>
<iframe id="same" src="/controls?tag=same"
  style="position:absolute;left:40px;top:60px;width:230px;height:190px;border:7px solid;padding:5px"></iframe>
<iframe id="cross" src="http://localhost:{port}/controls?tag=cross"
  style="position:absolute;left:300px;top:60px;width:230px;height:190px;border:9px solid;padding:3px"></iframe>
<iframe id="late" style="position:absolute;left:560px;top:60px;width:230px;height:190px;border:1px solid"></iframe>
<iframe id="nested" src="http://localhost:{port}/nest"
  style="position:absolute;left:40px;top:290px;width:520px;height:270px;border:2px solid"></iframe>
<div style="position:absolute;left:600px;top:300px;width:170px;height:230px;overflow:hidden">
  <iframe id="clip" src="http://localhost:{port}/controls?tag=clip&pad=120"
    style="margin-left:-20px;margin-top:-30px;width:400px;height:400px;border:4px solid"></iframe>
</div>
<iframe id="hidden" src="http://localhost:{port}/controls?tag=hidden" style="width:0;height:0;border:0"></iframe>
<iframe id="away" src="http://localhost:{port}/controls?tag=away"
  style="position:absolute;left:3000px;top:60px"></iframe>
</body>"""

NO_FRAMES = """<!doctype html><title>No frames</title>
<button id="b" onclick="window.clicks=(window.clicks||0)+1">Continue</button>
<input id="i" aria-label="City">
<select id="s" aria-label="Category"><option>All</option><option>Design</option></select>
<p>Plain page</p>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        port = self.server.server_address[1]
        body = {
            "/parent": PARENT.format(port=port),
            "/nest": NEST.format(port=port),
            "/controls": CONTROLS.format(tag=query.get("tag", "frame"), pad=query.get("pad", "0")),
        }.get(url.path, "")
        self.send_response(200 if body else 404)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, *_args):
        pass


def find(page, label, kind=None):
    return next(a for a in page["actions"] if a["label"].startswith(label) and (kind is None or a["kind"] == kind))


def settle(browser, test, attempts=100):
    for _ in range(attempts):
        page = browser.observe(screenshot=False)
        if test(page):
            return page
        browser.evaluate("new Promise(r => setTimeout(r, 50))")
    raise AssertionError("Frames did not load")


def frame_value(browser, action, expression):
    """Read DOM state in the element's own document, independent of the agent's observation."""
    node = action["node"]
    if node > 0:
        return browser.evaluate(f"(e => {expression})(window.__jevFast.nodes.get({node}))")
    frame, _, local = browser.nodes[node]
    return browser.frame_evaluate(frame, f"(e => {expression})(window.__jevFast.nodes.get({local}))")


def expect_stale(browser, page, action, label, passed):
    assert not browser.fresh(page, action), label
    try:
        browser.act(action, page, text="x")
    except StalePage:
        passed.append(label)
    else:
        raise AssertionError(label + ": acted on a stale frame")


def check(port):
    browser = Browser(f"http://127.0.0.1:{port}/parent")
    passed = []
    labels = ("same", "cross", "deep", "sub", "clip")
    try:
        page = settle(browser, lambda p: all(any(a["label"] == f"{t} button" for a in p["actions"]) for t in labels))
        for tag in ("same", "cross", "deep", "sub"):
            assert f"{tag} text" in page["text"], tag
        assert "nest text" in page["text"]
        assert not any(a["label"].startswith(("hidden", "away")) for a in page["actions"])
        passed.append("controls and text from same-origin, cross-site, nested and same-site frames are observed")
        passed.append("zero-size and offscreen frames are skipped")
        assert {a["node"] > 0 for a in page["actions"] if a["label"].startswith("same")} == {True}
        assert {a["node"] < 0 for a in page["actions"] if a["label"].startswith(("cross", "deep", "sub"))} == {True}
        passed.append("same-origin frame nodes share the page's IDs; other frames get code-owned negative IDs")
        # Chrome with origin isolation runs every unreachable frame as its own target; without it, a same-site
        # cross-origin frame shares the page's process and is read through an isolated world.
        worlds = sum(rec["context"] is not None for rec in browser.frames.values())
        passed.append(f"unreachable frames: {len(browser.frames) - worlds} target sessions, {worlds} isolated worlds")

        # Rects are top-level: the same button's center, hit-tested by the page, lands inside its own frame.
        for tag, frame_id in (("same", "same"), ("cross", "cross"), ("sub", "nested")):
            rect = find(page, f"{tag} button")["rect"]
            x, y = rect["x"] + rect["w"] / 2, rect["y"] + rect["h"] / 2
            hit = browser.evaluate(f"document.elementFromPoint({x},{y})?.id")
            assert hit == frame_id, (tag, hit)
        passed.append("rects are in top-level coordinates, including borders, padding and nesting")

        for tag in labels:
            page = browser.observe(screenshot=False)
            button, field = find(page, f"{tag} button"), find(page, f"{tag} input", "fill")
            select = find(page, f"{tag} select", "select")
            browser.act(button, page)
            page = browser.observe(screenshot=False)
            browser.act(find(page, f"{tag} input", "fill"), page, text=f"typed in {tag}")
            page = browser.observe(screenshot=False)
            browser.act(find(page, f"{tag} select", "select"), page)
            assert frame_value(browser, button, "e.ownerDocument.defaultView.clicks") == 1, tag
            assert frame_value(browser, field, "e.value") == f"typed in {tag}", tag
            assert frame_value(browser, select, "e.value") == "Two", tag
            browser.observe(screenshot=False)
            passed.append(f"{tag}: click fired, text typed, dropdown changed")
        assert browser.evaluate("window.clicks") is None
        passed.append("frame input never reached the page's own controls")

        # The clipped frame is scrolled inside and clipped by its container; the click must still land.
        clip = find(browser.observe(screenshot=False), "clip button")
        frame = browser.nodes[clip["node"]][0]
        browser.frame_evaluate(frame, "scrollTo(0, 60)")
        page = browser.observe(screenshot=False)
        clip = find(page, "clip button")
        container = browser.evaluate("(r => [r.left, r.top, r.right, r.bottom])"
                                     "(document.querySelector('#clip').parentElement.getBoundingClientRect())")
        x, y = clip["rect"]["x"] + clip["rect"]["w"] / 2, clip["rect"]["y"] + clip["rect"]["h"] / 2
        assert container[0] <= x < container[2] and container[1] <= y < container[3], (x, y, container)
        browser.act(clip, page)
        assert frame_value(browser, clip, "e.ownerDocument.defaultView.clicks") == 2
        passed.append("scrolled, clipped, offset frame: click lands on the right element")

        # Late frame: an <iframe> without src gets one after load, like a chat widget.
        page = browser.observe(screenshot=False)
        browser.evaluate(f"document.querySelector('#late').src='http://localhost:{port}/controls?tag=late'")
        assert not browser.fresh(page)
        page = settle(browser, lambda p: any(a["label"] == "late button" for a in p["actions"]))
        late = find(page, "late button")
        browser.act(late, page)
        assert frame_value(browser, late, "e.ownerDocument.defaultView.clicks") == 1
        passed.append("a frame given its src after load is observed and clicked; the old marker is stale")

        # Covered by the page: the frame's own hit test passes, the page's must fail.
        page = browser.observe(screenshot=False)
        cross = find(page, "cross button")
        before = frame_value(browser, cross, "e.ownerDocument.defaultView.clicks")
        browser.evaluate("const cover=document.createElement('div'); cover.id='cover';"
                         "cover.style.cssText='position:fixed;left:280px;top:40px;width:300px;height:260px;"
                         "z-index:9;background:white'; document.body.append(cover)")
        assert browser.fresh(page, cross)
        try:
            browser.act(cross, page)
        except StalePage:
            pass
        else:
            raise AssertionError("A frame element covered by the page was clicked")
        assert frame_value(browser, cross, "e.ownerDocument.defaultView.clicks") == before
        browser.evaluate("document.querySelector('#cover').remove()")
        passed.append("frame element covered by a page overlay is rejected before input")

        page = browser.observe(screenshot=False)
        deep = find(page, "deep button")
        browser.evaluate("const c=document.createElement('div'); c.id='cover';"
                         "c.style.cssText='position:fixed;left:60px;top:320px;width:120px;height:120px;"
                         "z-index:9;background:white'; document.body.append(c)")
        try:
            browser.act(deep, page)
        except StalePage:
            pass
        else:
            raise AssertionError("A nested frame element covered by the page was clicked")
        assert frame_value(browser, deep, "e.ownerDocument.defaultView.clicks") == 1
        browser.evaluate("document.querySelector('#cover').remove()")
        passed.append("nested frame element covered by a page overlay is rejected before input")

        # Decisions made before a frame navigated or was removed must not execute.
        page = browser.observe(screenshot=False)
        cross = find(page, "cross button")
        browser.evaluate(f"document.querySelector('#cross').src='http://localhost:{port}/controls?tag=cross'")
        settle(browser, lambda p: any(a["label"] == "cross button" for a in p["actions"]))
        expect_stale(browser, page, cross, "cross-site frame reloaded: old decision raises StalePage", passed)
        assert not browser.fresh(page)

        page = browser.observe(screenshot=False)
        same = find(page, "same button")
        browser.evaluate("document.querySelector('#same').src='/controls?tag=same'")
        settle(browser, lambda p: any(a["label"] == "same button" for a in p["actions"]))
        expect_stale(browser, page, same, "same-origin frame reloaded: old decision raises StalePage", passed)

        page = browser.observe(screenshot=False)
        sub, typed = find(page, "sub button"), find(page, "sub input", "fill")
        nested_frame = browser.nodes[find(page, "deep button")["node"]][0]
        browser.frame_evaluate(browser.frames[nested_frame]["host"], "document.querySelector('#sub').remove()")
        expect_stale(browser, page, sub, "same-site frame removed: old click raises StalePage", passed)
        expect_stale(browser, page, typed, "same-site frame removed: old fill raises StalePage", passed)

        page = browser.observe(screenshot=False)
        cross = find(page, "cross button")
        browser.evaluate("document.querySelector('#cross').remove()")
        expect_stale(browser, page, cross, "cross-site frame removed: old decision raises StalePage", passed)
        assert not browser.fresh(page)
        page = browser.observe(screenshot=False)
        assert not any(a["label"].startswith(("cross", "sub")) for a in page["actions"])
        passed.append("removed frames leave the next observation")
    finally:
        browser.close()
    return passed


def count():
    """CDP calls per observe, terminal fresh, click fresh, and act, on a page with no frames."""
    calls = []
    original = browser_module.cdp

    def counting(method, *args, **kwargs):
        calls.append(method)
        return original(method, *args, **kwargs)

    browser_module.cdp = counting
    browser = Browser("data:text/html," + quote(NO_FRAMES))
    counts = {}
    try:
        def measure(name, run):
            calls.clear()
            result = run()
            counts[name] = len(calls)
            return result

        page = measure("observe", lambda: browser.observe(screenshot=False))
        measure("observe with screenshot", lambda: browser.observe(screenshot=True))
        page = browser.observe(screenshot=False)
        click = find(page, "Continue")
        measure("fresh", lambda: browser.fresh(page))
        measure("fresh click", lambda: browser.fresh(page, click))
        measure("act click", lambda: browser.act(click, page))
        measure("observe after click", lambda: browser.observe(screenshot=False))
        page = browser.observe(screenshot=False)
        measure("act fill", lambda: browser.act(find(page, "City", "fill"), page, text="Zurich"))
        page = browser.observe(screenshot=False)
        measure("act select", lambda: browser.act(find(page, "Category", "select"), page))
        assert browser.evaluate("window.clicks") == 1
        assert browser.evaluate("document.querySelector('#i').value") == "Zurich"
        assert browser.evaluate("document.querySelector('#s').value") == "Design"
    finally:
        browser.close()
        browser_module.cdp = original
    return counts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--count", action="store_true", help="only count CDP calls on a page without frames")
    args = parser.parse_args()
    if args.count:
        print(json.dumps(count()))
        return
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        passed = check(server.server_address[1])
    finally:
        server.shutdown()
    print("\n".join(passed))
    print("CDP calls without frames:", json.dumps(count()))
    print(f"PASS: {len(passed)} browser frame checks; no model calls")


if __name__ == "__main__":
    main()
