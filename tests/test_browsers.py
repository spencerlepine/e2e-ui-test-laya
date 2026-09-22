"""Offline contracts for choosing browsers and for Firefox key input. No browser is launched."""

import argparse

import pytest

from laya_ultrafast import browsers
from laya_ultrafast.firefox import graphemes


@pytest.mark.parametrize("chrome,firefox,edge,expected", [
    (None, None, None, ["chrome"]), ("true", "true", None, ["chrome", "firefox"]),
    ("false", "true", None, ["firefox"]), ("TRUE", "False", None, ["chrome"]),
    ("true", "true", "true", ["chrome", "firefox", "edge"]), ("false", None, "true", ["edge"]),
])
def test_enabled_browsers_come_from_the_environment(monkeypatch, chrome, firefox, edge, expected):
    for name, value in (("CHROME_ENABLED", chrome), ("FIREFOX_ENABLED", firefox), ("EDGE_ENABLED", edge)):
        if value is None:
            monkeypatch.delenv(name, raising=False)
        else:
            monkeypatch.setenv(name, value)
    assert browsers.enabled() == expected


def test_no_browser_enabled_stops_with_a_reason(monkeypatch):
    monkeypatch.setenv("CHROME_ENABLED", "false")
    monkeypatch.setenv("FIREFOX_ENABLED", "false")
    monkeypatch.setenv("EDGE_ENABLED", "false")
    with pytest.raises(SystemExit, match="No browser enabled"):
        browsers.enabled()


def test_an_explicit_browser_runs_in_process(monkeypatch):
    monkeypatch.setenv("FIREFOX_ENABLED", "true")
    parser = argparse.ArgumentParser()
    browsers.add_argument(parser)
    assert browsers.choose(parser.parse_args(["--browser", "firefox"]), "script.py", []) == "firefox"


def test_several_enabled_runs_each_browser_in_turn(monkeypatch):
    monkeypatch.setenv("CHROME_ENABLED", "true")
    monkeypatch.setenv("FIREFOX_ENABLED", "true")
    monkeypatch.delenv("EDGE_ENABLED", raising=False)
    seen = []
    monkeypatch.setattr(browsers, "run_each", lambda script, argv, names: seen.append((script, argv, names)) or 1)
    parser = argparse.ArgumentParser()
    browsers.add_argument(parser)
    with pytest.raises(SystemExit) as stop:
        browsers.choose(parser.parse_args([]), "script.py", ["--url", "u"])
    assert stop.value.code == 1 and seen == [("script.py", ["--url", "u"], ["chrome", "firefox"])]


def test_run_each_passes_only_if_every_browser_passes(tmp_path, capsys):
    script = tmp_path / "s.py"
    script.write_text("import sys\nprint('hi')\nraise SystemExit(0 if sys.argv[-1] == 'chrome' else 3)\n")
    assert browsers.run_each(str(script), [], ["chrome", "firefox"]) == 1
    out = capsys.readouterr().out
    assert "[chrome ] hi" in out and "[firefox] hi" in out
    assert "chrome: PASSED" in out and "firefox: FAILED (exit 3)" in out


def test_run_each_never_overlaps_browsers(tmp_path, capsys):
    log = tmp_path / "log"
    script = tmp_path / "s.py"
    script.write_text(
        "import sys, time\n"
        f"log = open({str(log)!r}, 'a')\n"
        "log.write(f'start {sys.argv[-1]}\\n'); log.flush(); time.sleep(0.3)\n"
        "log.write(f'end {sys.argv[-1]}\\n')\n")
    assert browsers.run_each(str(script), [], ["chrome", "firefox", "edge"]) == 0
    assert log.read_text().split("\n")[:-1] == [
        "start chrome", "end chrome", "start firefox", "end firefox", "start edge", "end edge"]


def test_an_edge_run_restarts_itself_on_the_test_edge_connection(monkeypatch):
    for name, value in (("CHROME_ENABLED", "false"), ("EDGE_ENABLED", "true"), ("BU_NAME", "laya-test"),
                        ("BU_CDP_URL", "http://127.0.0.1:9333")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(browsers, "start_test_edge", lambda: None)
    execs = []
    monkeypatch.setattr(browsers.os, "execve", lambda path, argv, env: execs.append((argv[1:], env)))
    parser = argparse.ArgumentParser()
    browsers.add_argument(parser)
    assert browsers.choose(parser.parse_args([]), "script.py", ["--url", "u"]) == "edge"
    (argv, env), = execs
    assert argv == ["script.py", "--url", "u", "--browser", "edge"]
    assert env["BU_NAME"] == "laya-test-edge" and env["BU_CDP_URL"] == "http://127.0.0.1:9334"


def test_an_edge_run_already_on_its_connection_continues_in_process(monkeypatch):
    monkeypatch.setenv("BU_NAME", "laya-test-edge")
    monkeypatch.setenv("BU_CDP_URL", "http://127.0.0.1:9334")
    monkeypatch.setattr(browsers, "start_test_edge", lambda: None)
    monkeypatch.setattr(browsers.os, "execve", lambda *a: pytest.fail("restarted"))
    parser = argparse.ArgumentParser()
    browsers.add_argument(parser)
    assert browsers.choose(parser.parse_args(["--browser", "edge"]), "script.py", ["--browser", "edge"]) == "edge"


def test_edge_runs_in_turn_get_their_own_connection(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("BU_NAME", "laya-test")
    script = tmp_path / "s.py"
    script.write_text("import os\nprint(os.environ['BU_NAME'])\n")
    assert browsers.run_each(str(script), [], ["chrome", "edge"]) == 0
    out = capsys.readouterr().out
    assert "[chrome] laya-test\n" in out and "[edge  ] laya-test-edge\n" in out


def test_key_presses_keep_emoji_sequences_together():
    assert graphemes("Hi 👍🏽 👨\u200d👩\u200d👧 ❤\ufe0f") == [
        "H", "i", " ", "👍🏽", " ", "👨\u200d👩\u200d👧", " ", "❤\ufe0f"]
