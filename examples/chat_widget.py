"""Live E2E on a chat widget that runs in an iframe, with checks read from the browser itself.

uv run --env-file .env python examples/chat_widget.py --url URL --goal GOAL [--expect TEXT] [--expect-gone TEXT]
    [--expect-ended] [--expect-closed]

After every step this records, straight from the browser (independent of the agent's observation and DONE):
each widget iframe's own document text, whether it is visible, the page URL, and the full element table.

Checks (exit code 0 only if all requested checks pass):
  launched       a widget iframe with content was visible at some step
  expect         each --expect TEXT appeared as a whole line of a widget's text, not only in a field (a sent message)
  gone           each --expect-gone TEXT is in no widget's text at the end (a call screen that closed)
  ended          with --expect-ended: a widget said the chat or call ended
  widget_closed  with --expect-closed: at the end the tab is still on the start page and no widget is visible
Every run also saves the full element table per step, so a new widget's controls can be read from the trace.
Site-specific checks live here, never in laya_ultrafast/.
"""

import argparse
import datetime
import json
import os
import re
import sys
from pathlib import Path
from urllib.parse import urlparse

from laya_ultrafast import Agent, browsers
from laya_ultrafast.browser import StalePage
from laya_ultrafast.capture import RunOutput

NAME = "customer widget"  # persona interface (examples/personas.py): NAME, matches, probe, prime
URL = "https://tinyurl.com/37p7u4vf"
GOAL = 'Launch the chat widget, start a conversation, send "Hello, World!", end the chat, close the widget.'
ENDED = re.compile(r"(chat|conversation|call) (has )?ended|ended the (chat|call)|chat is over", re.I)
WIDGETS = """JSON.stringify([...document.querySelectorAll('iframe')].map(f => {
  let text = null;
  try { text = f.contentDocument?.body?.innerText ?? null; } catch (e) {}
  const shown = f.checkVisibility({checkOpacity: true, checkVisibilityCSS: true});
  return {id: f.id, src: f.src, text, visible: shown && f.offsetWidth > 0 && f.offsetHeight > 0};
}))"""


prime = None  # a customer starts from a fresh tab


def matches(url):
    """Any page that is not an Amazon Connect agent page hosts a customer chat widget."""
    return not (urlparse(url).hostname or "").endswith(".my.connect.aws")


def probe(browser, page, step, status):
    """Browser state after a step. Cross-origin frames are read in their own session."""
    iframes = json.loads(browser.evaluate(WIDGETS))
    texts = [f["text"] for f in iframes if f["text"]]
    for frame in list(browser.frames):
        try:
            texts.append(browser.frame_evaluate(frame, "document.body?.innerText ?? ''"))
        except StalePage:
            pass
    return {
        "step": step, "status": status, "url": browser.evaluate("location.href"),
        "iframes": [{k: f[k] for k in ("id", "src", "visible")} | {"has_content": bool((f["text"] or "").strip())}
                    for f in iframes],
        "frame_texts": [t for t in texts if t and t.strip()],
        "field_values": [(a.get("value") or "")[:200] for a in page["actions"] if a["kind"] == "fill"],
        "elements": [(a["id"], a["kind"], a.get("role"), a["label"][:80]) for a in page["actions"]],
    }


def verify(probes, expect=(), ended=False, gone=(), closed=False):
    """Pass/fail per requested check, from the recorded browser state. The first probe is the start page."""
    def seen(text):
        """Steps where a widget shows the text as a whole line (how a transcript shows a message) more often than
        its fields hold it. A picker's 😃 inside a row of emojis, or text still in the message box, is not sent."""
        return [p["step"] for p in probes
                if sum(line.strip() == text for t in p["frame_texts"] for line in t.splitlines())
                > sum(v.strip() == text for v in p["field_values"])]

    def live(f):  # a visible widget: same-origin with content, or cross-origin (its text is read separately)
        return f["visible"] and (f["has_content"] or bool(f["src"]))

    checks = {"launched": any(live(f) for p in probes for f in p["iframes"])}
    for text in expect:
        checks[f"expect {text!r}"] = bool(seen(text))
    if ended:
        checks["ended"] = any(ENDED.search(t) for p in probes for t in p["frame_texts"])
    final = probes[-1] if probes else None
    for text in gone:
        checks[f"gone {text!r}"] = bool(final) and not any(text in t for t in final["frame_texts"])
    same_page = bool(final) and urlparse(final["url"])[:3] == urlparse(probes[0]["url"])[:3]
    if closed:
        checks["widget_closed"] = same_page and not any(live(f) for f in final["iframes"])
    return {"passed": all(checks.values()), "checks": checks,
            "steps_with_text": {text: seen(text) for text in expect}, "final_url": final and final["url"]}


def expected(args):
    """The requested checks in words, for the run log."""
    lines = ["launched: a widget iframe with content is visible at some step"]
    lines += [f"expect {t!r}: a widget shows it as its own line (sent, not just typed)" for t in args.expect]
    lines += [f"gone {t!r}: no widget shows it at the end" for t in args.expect_gone]
    if args.expect_ended:
        lines.append("ended: a widget says the chat or call ended")
    if args.expect_closed:
        lines.append("widget_closed: the tab is still on the start page and no widget is visible at the end")
    return lines


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL, help="page that hosts the chat widget")
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--expect", action="append", default=[], help="text a widget must show; repeatable")
    parser.add_argument("--expect-gone", action="append", default=[], help="text no widget may show at the end")
    parser.add_argument("--expect-ended", action="store_true", help="a widget must say the chat or call ended")
    parser.add_argument("--expect-closed", action="store_true", help="the widget must be closed at the end")
    parser.add_argument("--output", default="artifacts/chat_widget")
    browsers.add_argument(parser)
    args = parser.parse_args()
    browser = browsers.choose(args, __file__, sys.argv[1:])
    folder = (Path(args.output) / f"{datetime.datetime.now():%Y%m%dT%H%M%S}-{browser}-{os.getpid()}").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stale, error, seen = [], None, 0
    run = RunOutput(args.url, args.goal, runner="examples/chat_widget.py", expected=expected(args), browser=browser)
    try:
        agent = Agent(args.url, args.goal, browser=browser, before_act=lambda: run.start(agent.browser))
    except Exception as failure:  # no tab to record, but the run folder still says why the test did not start
        run.finish({}, passed=False, error=f"{type(failure).__name__}: {failure}")
        raise
    print(f"{browser} tab {agent.browser.target} (its own tab and session)", flush=True)
    probes = [probe(agent.browser, agent.state["page"], 0, "ready")]
    try:
        while agent.state["status"] not in {"done", "blocked"}:
            try:
                state = agent.command("tick")
            except StalePage as e:  # nothing executed on the stale decision; observe again
                stale.append({"step": len(agent.state["history"]), "error": str(e)})
                agent.state["page"] = agent.browser.observe(screenshot=False)
                continue
            for step in state["history"][seen:]:
                text = f" {step['text']!r}" if step.get("text") else ""
                print(f"{state['elapsed_ms']:>6} ms  {step['step']:>2}. {step['kind']:<6} {step['action'][:80]}{text}",
                      flush=True)
            seen = len(state["history"])
            probes.append(probe(agent.browser, agent.state["page"], seen, state["status"]))
    except Exception as e:  # keep the trace for any stop, including the action budget
        error = f"{type(e).__name__}: {e}"
        print("stopped:", error)
    finally:
        state = agent.snapshot()
        probes.append(probe(agent.browser, agent.state["page"], len(state["history"]), state["status"]))
        result = {"url": args.url, "goal": args.goal, "status": state["status"], "error": error, "stale": stale,
                  "verification": verify(probes, args.expect, args.expect_ended, args.expect_gone, args.expect_closed)}
        (folder / "state.json").write_text(json.dumps({**state, **result, "probes": probes}, indent=2, default=str))
        run.finish(state, passed=result["verification"]["passed"], checks=result["verification"]["checks"],
                   error=error, stale=stale, final_url=result["verification"]["final_url"])
        agent.close()
    print(json.dumps(result, indent=2, default=str))
    print("Trace:", folder / "state.json")
    print("Run output:", run.folder)
    raise SystemExit(0 if result["verification"]["passed"] else 1)


if __name__ == "__main__":
    main()
