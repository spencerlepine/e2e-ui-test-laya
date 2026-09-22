"""Which browsers a test runs in: CHROME_ENABLED, FIREFOX_ENABLED and EDGE_ENABLED in .env.

With one browser enabled, a runner runs in-process as before. With several, it runs itself once per browser,
sequentially, as separate processes (each with its own tab or Firefox, Laya copy, run folder and log): the next browser
starts only after the previous one exits, because concurrent runs conflict. It prefixes each line with the browser's
name, and exits 0 only if every browser's run passed.

Edge is Chromium, so it is driven exactly like Chrome (CDP through Browser Harness), through its own test Edge
(scripts/test_edge.sh) and its own Browser Harness daemon. Browser Harness reads its connection when it is imported,
so an Edge run always starts in a fresh process with that connection set.
"""

import os
import subprocess
import sys
import urllib.request
from pathlib import Path

BROWSERS = ("chrome", "firefox", "edge")
DEFAULTS = {"chrome": True, "firefox": False, "edge": False}
EDGE_PORT = os.environ.get("TEST_EDGE_PORT", "9334")
# The connection a browser's process needs, on top of .env. Chrome uses .env's own BU_CDP_URL and BU_NAME.
CONNECTION = {"edge": {"BU_CDP_URL": f"http://127.0.0.1:{EDGE_PORT}", "BU_NAME": "laya-test-edge"}}
TEST_EDGE = Path(__file__).resolve().parents[1] / "scripts" / "test_edge.sh"


def enabled():
    names = [name for name in BROWSERS
             if os.environ.get(f"{name.upper()}_ENABLED", str(DEFAULTS[name])).strip().lower() == "true"]
    if not names:
        raise SystemExit("No browser enabled: set CHROME_ENABLED, FIREFOX_ENABLED or EDGE_ENABLED to true in .env")
    return names


def add_argument(parser):
    parser.add_argument("--browser", choices=BROWSERS,
                        help="run in this browser only (default: every browser enabled in .env, one after another)")


def choose(args, script, argv):
    """The browser this process runs. With several enabled and none chosen, runs each in turn and exits."""
    name = args.browser
    if name is None:
        names = enabled()
        if len(names) > 1:
            raise SystemExit(run_each(script, argv, names))
        name, argv = names[0], [*argv, "--browser", names[0]]
    connect(name, script, argv)
    return name


def connect(name, script, argv):
    """Make sure this process is connected to the browser's own test instance, restarting itself if not."""
    extra = CONNECTION.get(name)
    if not extra:
        return
    start_test_edge()
    if any(os.environ.get(key) != value for key, value in extra.items()):
        os.execve(sys.executable, [sys.executable, script, *argv], {**os.environ, **extra})


def start_test_edge():
    try:
        urllib.request.urlopen(f"{CONNECTION['edge']['BU_CDP_URL']}/json/version", timeout=1).close()
    except OSError:
        if TEST_EDGE.exists():
            subprocess.run([str(TEST_EDGE)], check=False)


def run_each(script, argv, names):
    """Run the script once per browser, one after another: a browser starts only after the previous one exits.

    Runs are never concurrent, because the browsers' test suites conflict at runtime when they overlap."""
    width = max(map(len, names))
    codes = {}
    for name in names:
        process = subprocess.Popen([sys.executable, script, *argv, "--browser", name], stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, bufsize=1,
                                   env={**os.environ, **CONNECTION.get(name, {})})
        for line in process.stdout:
            print(f"[{name:<{width}}] {line}", end="", flush=True)
        codes[name] = process.wait()
    for name, code in codes.items():
        print(f"{name}: {'PASSED' if code == 0 else f'FAILED (exit {code})'}")
    return 0 if all(code == 0 for code in codes.values()) else 1
