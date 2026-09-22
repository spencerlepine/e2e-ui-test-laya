"""Model-free execution check on the live chat widget: the same Browser.observe/act path the agent uses, with the
widget's controls found by label here (site-specific, so never in laya_ultrafast/). Sends one real message.

uv run --env-file .env python scripts/check_chat_widget.py [--url URL]
"""

import argparse
import datetime
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from examples.chat_widget import URL, probe, verify  # noqa: E402
from laya_ultrafast.browser import Browser, StalePage  # noqa: E402


def table(page):
    return [(a["id"], a["kind"], a.get("role"), a["label"][:60], a.get("node")) for a in page["actions"]]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=URL, help="page that hosts the chat widget")
    args = parser.parse_args()
    folder = (Path("artifacts/chat_widget_check") / f"{datetime.datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    browser = Browser(args.url)
    probes, log = [], []

    def record(note):
        page = browser.observe(screenshot=False)
        probes.append({**probe(browser, page, len(log), "ready"), "note": note})
        return page

    def step(pattern, kind="click", text=None, wait=8.0, optional=False):
        deadline = time.monotonic() + wait
        while True:
            page = record(f"looking for {pattern}")
            match = [a for a in page["actions"] if a["kind"] == kind and re.search(pattern, a["label"], re.I)]
            if match:
                action = match[0]
                try:
                    browser.act(action, page, text=text)
                except StalePage as e:
                    log.append({"stale": str(e), "action": action["label"]})
                    continue  # nothing executed; observe again
                log.append({"executed": action["label"], "kind": kind, "node": action["node"], "rect": action["rect"]})
                print("executed", kind, repr(action["label"]), "node", action["node"], action["rect"], flush=True)
                return action
            if time.monotonic() > deadline:
                if optional:
                    return None
                print("NOT FOUND", pattern, json.dumps(table(page), indent=0))
                raise SystemExit(f"No {kind} matching {pattern}")
            time.sleep(0.3)

    try:
        step(r"^Start Chat$")
        # The composer re-renders when an agent joins; type once the conversation is live.
        deadline = time.monotonic() + 30
        while not any(re.search("end chat", a["label"], re.I) for a in record("waiting for the chat")["actions"]):
            if time.monotonic() > deadline:
                raise SystemExit("The conversation did not start")
            time.sleep(0.5)
        step(r"Type a message", "fill", text="Hello, World!")
        time.sleep(0.5)
        page = record("after typing")
        typed = [a.get("value") for a in page["actions"] if a["kind"] == "fill" and "Type a message" in a["label"]]
        print("message box after typing:", typed, flush=True)
        if not step(r"^send", optional=True, wait=3):
            print("no send button observed:", [r[3] for r in table(page) if r[1] == "click"], flush=True)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            record("waiting for the transcript")
            if verify(probes, ["Hello, World!"])["checks"]["expect 'Hello, World!'"]:
                break
            time.sleep(0.5)
        step(r"end chat")
        step(r"^(end chat|yes|confirm|end)$", optional=True, wait=4)
        time.sleep(1)
        record("after ending")
        step(r"close|minimize")
        time.sleep(1)
        print("final table", json.dumps(table(record("final"))))
    finally:
        result = {"log": log, "verification": verify(probes, ["Hello, World!"], ended=True, closed=True)}
        (folder / "state.json").write_text(json.dumps({**result, "probes": probes}, indent=2, default=str))
        browser.close()
        print(json.dumps(result, indent=2, default=str))
        print("Trace:", folder / "state.json")


if __name__ == "__main__":
    main()
