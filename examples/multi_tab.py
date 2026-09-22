"""Live E2E across several tabs: one goal, one log, one video per tab, and checks read from each tab itself.

uv run --env-file .env python examples/multi_tab.py --tab NAME=URL [--tab NAME=URL ...] --goal GOAL
    [--expect NAME=TEXT] [--expect-gone NAME=TEXT] [--expect-ended NAME] [--expect-closed NAME]

The goal names the tabs ("In the customer widget ... then in the agent chat UI ..."). The text model plans the steps
once, each on a named tab, and the agent switches tabs as the steps require; nothing here orders the steps. Refer to a
login value as {{NAME}} (listed in LAYA_CREDENTIALS in .env): it is typed from the environment and never logged.

After every step this records, straight from each tab's browser (independent of the agent's observation and DONE):
the tab's own text, each widget iframe's text and visibility, the tab's URL, and its element table.

Each tab's persona comes from its URL (examples/personas.py): /ccp-v2 is the agent chat UI, /agent the agent workspace,
any other site a customer chat widget. The persona probes the tab for the checks, and agent tabs are primed before the
agent's first action: signed in, every leftover contact cleared, Available (--no-prime skips it).

Checks per tab (exit code 0 only if all requested checks pass):
  expect NAME=TEXT       the tab showed TEXT as a whole line (a message in a transcript, not only in a field)
  gone NAME=TEXT         the tab shows no TEXT at the end
  ended NAME             the tab said the chat or call ended (the tab also must have launched a widget)
  closed NAME            at the end the tab is on its start page and shows no widget (and launched one earlier)
Site-specific checks live in examples/, never in laya_ultrafast/.
"""

import argparse
import datetime
import json
import os
import sys
from pathlib import Path

import chat_widget  # examples/ is this script's folder, so its sibling personas are importable
import personas

from laya_ultrafast import Agent, tabs
from laya_ultrafast.browser import StalePage
from laya_ultrafast.capture import RunOutput

CUSTOMER = "customer widget"
AGENT = "agent chat UI"
TABS = [f"{CUSTOMER}=https://tinyurl.com/ye29e563", f"{AGENT}=https://spenlep.my.connect.aws/ccp-v2"]
GOAL = (f'In the {CUSTOMER} tab, launch the chat widget and start a conversation. Then in the {AGENT} tab: log in '
        'with {{CCP_AGENTCHATUI_USERNAME}} and {{CCP_AGENTCHATUI_PASSWORD}} if needed. Set the status to Available '
        'from the status dropdown if needed. Then accept the incoming chat and send "Hello from the agent". Then back '
        f'in the {CUSTOMER} tab, wait until the reply "Hello from the agent" is shown, send "confirm", end the chat, '
        'and close the widget.')
def pairs(values, names):
    """NAME=TEXT values as (name, text). A name must be one of the tabs."""
    result = []
    for value in values:
        name, _, text = value.partition("=")
        if name not in names or not text:
            raise SystemExit(f"Expected NAME=TEXT with NAME one of {names}, got {value!r}")
        result.append((name, text))
    return result


def verify(probes, args, names):
    """Per tab, chat_widget's checks on that tab's probes. `launched` counts only for a tab expected to end or close a
    widget; an agent UI has no widget to launch."""
    expect, gone = pairs(args.expect, names), pairs(args.expect_gone, names)
    checks, passed = {}, True
    for name in names:
        own = [p[name] for p in probes]
        widget = name in args.expect_ended or name in args.expect_closed
        result = chat_widget.verify(own, [t for n, t in expect if n == name], name in args.expect_ended,
                                    [t for n, t in gone if n == name], name in args.expect_closed)
        for check, ok in result["checks"].items():
            if check != "launched" or widget:
                checks[f"{name}: {check}"] = ok
                passed = passed and ok
    return {"passed": passed, "checks": checks}


def expected(args, names):
    lines = [f"{name}: {line}" for name in names for line in chat_widget.expected(argparse.Namespace(
        expect=[t for n, t in pairs(args.expect, names) if n == name],
        expect_gone=[t for n, t in pairs(args.expect_gone, names) if n == name],
        expect_ended=name in args.expect_ended, expect_closed=name in args.expect_closed))]
    return [line for line in lines if "launched" not in line or line.split(":")[0] in
            {*args.expect_ended, *args.expect_closed}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--tab", action="append", help="NAME=URL, repeatable; the first is tab 1 (capture.mov)")
    parser.add_argument("--goal", default=GOAL)
    parser.add_argument("--expect", action="append", default=[], help="NAME=TEXT the tab must show; repeatable")
    parser.add_argument("--expect-gone", action="append", default=[], help="NAME=TEXT the tab may not show at the end")
    parser.add_argument("--expect-ended", action="append", default=[], metavar="NAME",
                        help="the tab must say the chat or call ended")
    parser.add_argument("--expect-closed", action="append", default=[], metavar="NAME",
                        help="the tab's widget must be closed at the end")
    parser.add_argument("--no-prime", action="store_true", help="do not prime agent tabs before the first action")
    parser.add_argument("--output", default="artifacts/multi_tab")
    args = parser.parse_args()
    try:
        opened = tabs.parse(args.tab or TABS)
    except ValueError as error:
        raise SystemExit(str(error)) from None
    names = [name for name, _ in opened]
    kinds = {name: personas.persona(url) for name, url in opened}
    primed = [] if args.no_prime else [name for name in names if kinds[name].prime]
    for name, kind in kinds.items():
        print(f"tab {name!r}: persona {kind.NAME}", flush=True)
    for name in [*args.expect_ended, *args.expect_closed]:
        if name not in names:
            raise SystemExit(f"Unknown tab {name!r}; tabs are {names}")
    folder = (Path(args.output) / f"{datetime.datetime.now():%Y%m%dT%H%M%S}-{os.getpid()}").resolve()
    folder.mkdir(parents=True, exist_ok=True)
    stale, error, seen = [], None, 0
    run = RunOutput(opened, args.goal, runner="examples/multi_tab.py", expected=expected(args, names))
    try:
        agent = Agent(opened, args.goal, before_act=lambda: run.start(agent.browser))
    except Exception as failure:  # no tabs to record, but the run folder still says why the test did not start
        run.finish({}, passed=False, error=f"{type(failure).__name__}: {failure}")
        raise
    for name, tab in zip(names, agent.browser.tabs):
        print(f"tab {name!r}: chrome tab {tab.target}", flush=True)

    def snapshot_all(status):
        page = agent.state["page"]
        return {name: kinds[name].probe(tab, page["pages"][i], len(agent.state["history"]), status)
                for i, (name, tab) in enumerate(zip(names, agent.browser.tabs))}

    priming = {}
    try:
        # Before the first action (and before planning): the agent UI must be idle and Available, or a leftover chat
        # is offered in place of this test's own.
        for name in primed:
            try:
                priming[name] = kinds[name].prime(agent.browser.tab(name))
            except RuntimeError as failure:
                raise RuntimeError(f"priming {name!r} failed: {failure}") from None
        agent.state["page"] = agent.browser.observe(screenshot=False)
    except Exception as failure:
        run.finish(agent.snapshot(), passed=False, checks={f"{name}: primed": name in priming for name in primed},
                   error=f"{type(failure).__name__}: {failure}")
        agent.close()
        raise SystemExit(f"stopped before the test: {failure}") from None
    probes = [snapshot_all("ready")]
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
                where = f"[{step['tab']}] " if step.get("tab") else ""
                print(f"{state['elapsed_ms']:>6} ms  {step['step']:>3}. {step['kind']:<6} {where}{step['action'][:70]}"
                      f"{text}", flush=True)
            probes.append(snapshot_all(state["status"]))  # every tick: a reply or "chat ended" can come during a wait
            seen = len(state["history"])
    except Exception as e:  # keep the trace for any stop, including the action budget
        error = f"{type(e).__name__}: {e}"
        print("stopped:", error)
    finally:
        state = agent.snapshot()
        probes.append(snapshot_all(state["status"]))
        verification = verify(probes, args, names)
        verification["checks"] = {**{f"{name}: primed": True for name in primed}, **verification["checks"]}
        result = {"tabs": opened, "goal": args.goal, "status": state["status"], "error": error, "stale": stale,
                  "priming": priming, "verification": verification}
        trace = {k: v for k, v in state.items() if k != "page"}
        (folder / "state.json").write_text(json.dumps({**trace, **result, "probes": probes}, indent=2, default=str))
        run.finish(state, passed=verification["passed"], checks=verification["checks"], error=error, stale=stale)
        agent.close()
    print(json.dumps(result, indent=2, default=str))
    print("Trace:", folder / "state.json")
    print("Run output:", run.folder)
    raise SystemExit(0 if verification["passed"] else 1)


if __name__ == "__main__":
    sys.exit(main())
