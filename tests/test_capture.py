"""Offline contracts for the run output folder, its log, and the video's frame timing. No browser, no models."""

import datetime
import re
import shutil
import subprocess

import pytest
from PIL import Image

from laya_ultrafast import capture
from laya_ultrafast.capture import FirefoxRecorder, RunOutput, TabRecorder, write_concat


class FakeChrome:
    kind, target, version = "chrome", "TARGET", "140.0.1"


class FakeFirefox:
    kind, target, version = "firefox", "CONTEXT", "156.0.1"

    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def call(self, method, **params):
        self.calls.append(method)
        if self.fail:
            raise RuntimeError("unknown command")
        return {"screencast": "S1", "path": "/nonexistent.webm"}


NOW = datetime.datetime(2026, 9, 22, 14, 3, 7, 123456).astimezone()
STATE = {
    "status": "done",
    "elapsed_ms": 2400,
    "started_at": None,
    "goal_plan": {"requirements": [], "open": None, "finish": "The chat is closed.",
                  "steps": [{"do": "Launch the chat widget", "labels": ["Chat"], "text": None},
                            {"do": "Send hello", "labels": ["Send"], "text": "Hello"}]},
    "history": [
        {"step": 1, "action": "Wait for the page to update", "kind": "wait", "operation": "WAIT", "text": None,
         "confidence": 1.0, "page_changed": False, "url": "https://example.test/", "executed_ms": 100},
        {"step": 2, "action": "Chat", "kind": "click", "operation": "CLICK", "text": None, "confidence": 0.93,
         "page_changed": True, "url": "https://example.test/chat", "executed_ms": 900},
        {"step": 3, "action": "Message", "kind": "fill", "operation": "TYPE_TEXT", "text": "Hello",
         "confidence": 0.8, "page_changed": True, "url": "https://example.test/chat", "executed_ms": 1500},
    ],
    "decisions": [{}, {}, {}, {}],
    "text_calls": [{}],
    "page": {"url": "https://example.test/chat"},
}


def test_every_run_gets_its_own_folder_with_the_prompt(tmp_path):
    first = RunOutput("https://example.test/", ["Open chat", "Say hello"], runner="t", root=tmp_path, now=NOW)
    second = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path, now=NOW)
    assert first.folder != second.folder
    assert re.fullmatch(r"2026-09-22_14-03-07-123-chrome-test-[0-9a-f]{6}", first.name)
    assert (first.folder / "prompt.txt").read_text() == (
        "--url https://example.test/\n--goal Open chat\n--goal Say hello\n")


def test_video_off_still_writes_prompt_and_log(tmp_path, monkeypatch):
    monkeypatch.setenv("RECORD_VIDEO", "false")
    monkeypatch.delenv("LAYA_MODEL", raising=False)
    monkeypatch.delenv("DECISION_MODEL", raising=False)
    monkeypatch.setenv("TEXT_MODEL", "gemma4:latest")
    run = RunOutput("https://example.test/", "Open chat", runner="t", expected=["launched"], root=tmp_path)
    run.start(FakeChrome())
    folder = run.finish(STATE, passed=False, checks={"launched": True, "expect 'Hello'": False})
    assert sorted(p.name for p in folder.iterdir()) == ["log.txt", "prompt.txt"]
    log = (folder / "log.txt").read_text()
    assert log.startswith("---\ntimestamp: ")
    assert "\nlaya-model: aac6fef/laya-typed-decisions-mlx\ntext-model: gemma4:latest\nlength: 2s\n---\n" in log
    assert "\nbrowser: chrome 140.0.1\n" in log
    assert "video: off (RECORD_VIDEO=false)" in log
    assert "finish when: The chat is closed." in log
    assert 'step 2: Send hello  (likely labels: "Send")  type: "Hello"' in log
    assert '#3  TYPE_TEXT "Message" typed "Hello"' in log
    assert "now at: https://example.test/chat" in log
    assert "PASS launched" in log and "FAIL expect 'Hello'" in log
    assert "actions: 2  waits: 1  decisions: 4  text-model calls: 1" in log
    assert log.rstrip().endswith("RESULT: FAILED")


def test_hosted_decisions_say_no_laya_model_was_used(tmp_path, monkeypatch):
    monkeypatch.setenv("DECISION_MODEL", "typesafe")
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path)
    assert "laya-model: none (DECISION_MODEL=typesafe)" in (run.finish(STATE, passed=True) / "log.txt").read_text()


def test_a_run_that_never_started_still_logs_why(tmp_path):
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path)
    log = (run.finish({}, passed=False, error="RuntimeError: Model connection failed") / "log.txt").read_text()
    assert "video: not started (the run stopped before its first action)" in log
    assert "plan: none recorded" in log and "(no actions executed)" in log
    assert "error: RuntimeError: Model connection failed" in log


def test_log_steps_carry_their_offset_into_the_video(tmp_path):
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path)
    run.video_perf = 10.5  # recording started 0.5 s after the first decision began (after planning)
    (run.folder / "capture.mov").write_bytes(b"")
    log = (run.finish({**STATE, "started_at": 10.0}, passed=True) / "log.txt").read_text()
    assert "video=00:00.4  #2" in log  # 0.9 s into the run is 0.4 s into the video


def test_each_frame_lasts_until_the_next_one(tmp_path):
    write_concat([(100.0, "a.jpg"), (100.5, "b.jpg"), (102.0, "c.jpg")], 103.25, tmp_path / "f.txt")
    assert (tmp_path / "f.txt").read_text().splitlines() == [
        "file 'a.jpg'", "duration 0.500", "file 'b.jpg'", "duration 1.500",
        "file 'c.jpg'", "duration 1.250", "file 'c.jpg'"]


def test_a_failed_recorder_never_fails_the_run(tmp_path, monkeypatch):
    def unreachable():
        raise RuntimeError("Chrome is not running")

    monkeypatch.setattr(capture, "browser_ws_url", unreachable)
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path)
    run.start(FakeChrome())
    run.start(FakeChrome())  # called before every action; a failed recorder is not retried
    folder = run.finish(STATE, passed=True)
    assert not (folder / "capture.mov").exists()
    log = (folder / "log.txt").read_text()
    assert "video: failed" in log and "Chrome is not running" in log and "RESULT: PASSED" in log


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg is not installed")
def test_frames_become_a_mov_at_original_speed(tmp_path):
    recorder = TabRecorder("TARGET", tmp_path)
    for i, color in enumerate(["red", "green", "blue"]):
        Image.new("RGB", (321, 201), color).save(tmp_path / f"{i}.jpg")
    recorder.frames = [(0.0, "0.jpg"), (0.4, "1.jpg"), (1.0, "2.jpg")]
    recorder.stopped = 2.0
    recorder.encode(tmp_path / "capture.mov")
    duration = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0",
                               str(tmp_path / "capture.mov")], capture_output=True, text=True).stdout
    assert float(duration) == pytest.approx(2.0, abs=0.1)


def test_bursts_faster_than_the_video_rate_never_drift(tmp_path):
    frames = [(i / 40, f"{i}.jpg") for i in range(400)]  # 10 s of frames at 40 fps
    write_concat(frames, 10.0, tmp_path / "f.txt")
    lines = (tmp_path / "f.txt").read_text().splitlines()
    assert sum(float(line.split()[1]) for line in lines if line.startswith("duration")) == pytest.approx(10.0, abs=0.01)
    assert lines[-1] == "file '399.jpg'"  # the newest frame is kept, so the video ends on the final state


def test_the_folder_names_its_browser(tmp_path):
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path, now=NOW, browser="firefox")
    assert re.fullmatch(r"2026-09-22_14-03-07-123-firefox-test-[0-9a-f]{6}", run.name)


def test_firefox_runs_use_firefoxs_own_screencast(tmp_path):
    browser = FakeFirefox()
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path, browser="firefox")
    run.start(browser)
    assert isinstance(run.recorder, FirefoxRecorder) and browser.calls == ["browsingContext.startScreencast"]
    log = (run.finish(STATE, passed=True) / "log.txt").read_text()
    assert browser.calls[-1] == "browsingContext.stopScreencast"
    assert "browser: firefox 156.0.1" in log and "video: failed" in log  # the fake wrote no file


def test_a_firefox_without_screencast_never_fails_the_run(tmp_path):
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=tmp_path, browser="firefox")
    run.start(FakeFirefox(fail=True))
    log = (run.finish(STATE, passed=True) / "log.txt").read_text()
    assert "video: failed: RuntimeError: unknown command" in log and "RESULT: PASSED" in log
