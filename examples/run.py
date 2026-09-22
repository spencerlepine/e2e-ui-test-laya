"""uv run --env-file .env python examples/run.py --url URL --goal 'A narrow goal'

Prints each executed action and the final URL. Exits 0 only when the agent chose DONE; DONE is still not proof of
success, so check the outcome independently (see README.md). Each run writes output/<time>-<browser>-test-<id>/
with prompt.txt, log.txt and capture.mov (RECORD_VIDEO=false skips the video). CHROME_ENABLED, FIREFOX_ENABLED and
EDGE_ENABLED in .env choose the browsers; with several, they run one after another and the exit code is 0 only if every
run chose DONE."""

import argparse
import sys

from laya_ultrafast import Agent, browsers
from laya_ultrafast.capture import RunOutput

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--goal", action="append", required=True, help="Repeat for an ordered list of goals.")
browsers.add_argument(parser)
args = parser.parse_args()
browser = browsers.choose(args, __file__, sys.argv[1:])

state, seen, error = None, 0, None
run = RunOutput(args.url, args.goal, runner="examples/run.py", browser=browser,
                expected=["the agent chooses DONE (its own claim; this runner checks nothing independently)"])
try:
    agent = Agent(args.url, args.goal, browser=browser, before_act=lambda: run.start(agent.browser))
except Exception as failure:  # no tab to record, but the run folder still says why the test did not start
    run.finish({}, passed=False, error=f"{type(failure).__name__}: {failure}")
    raise
with agent:
    try:
        for state in agent.run():
            for step in state["history"][seen:]:
                text = f" {step['text']!r}" if step.get("text") else ""
                print(f"{state['elapsed_ms']:>6} ms  {step['step']:>2}. {step['kind']:<6} {step['action'][:80]}{text}",
                      flush=True)
            seen = len(state["history"])
    except ValueError as stop:  # an action or model-call budget; the run stopped without DONE
        print("stopped:", stop)
        error = f"{type(stop).__name__}: {stop}"
    except Exception as failure:
        error = f"{type(failure).__name__}: {failure}"
        raise
    finally:
        state = agent.snapshot()
        run.finish(state, passed=state["status"] == "done", error=error)
    print(f"{state['status']} after {len(state['history'])} actions: {state['page']['url']}")
print("Run output:", run.folder)
raise SystemExit(0 if state["status"] == "done" else 1)
