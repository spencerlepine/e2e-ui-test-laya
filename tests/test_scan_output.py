"""Offline contracts for examples/scan-output.py: BUG-REPORT.csv, next to output/, lists exactly the failing runs."""

import csv
import importlib.util
from pathlib import Path

import pytest
from test_capture import STATE

from laya_ultrafast.capture import RunOutput

spec = importlib.util.spec_from_file_location("scan_output", Path(__file__).parents[1] / "examples" / "scan-output.py")
scan_output = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan_output)


@pytest.fixture
def output(tmp_path):
    folder = tmp_path / "output"
    folder.mkdir()
    return folder


def run_folder(root, name, result=None, prompt="--url https://example.test/\n--goal Open chat, say hi\n",
               browser="edge 153.0"):
    folder = root / name
    folder.mkdir()
    (folder / "prompt.txt").write_text(prompt)
    if result:
        (folder / "log.txt").write_text(f"---\nbrowser: {browser}\n---\n\nRESULT: {result}\n")
    return folder


def report(output):
    with open(output.parent / "BUG-REPORT.csv", newline="") as file:
        return list(csv.DictReader(file))


def test_only_failing_runs_are_reported_with_a_one_line_prompt(output):
    run_folder(output, "2026-09-22_08-10-38-500-edge-test-2539b3", "FAILED",
               prompt="--url https://example.test/\n--goal Send \"Hello, World!\"\n")
    run_folder(output, "2026-09-22_07-24-17-660-chrome-test-157f6e", "PASSED")
    run_folder(output, "2026-09-22_08-41-56-773-chrome-test-38828e")  # no log yet: still running or killed
    rows, incomplete = scan_output.scan(output)
    assert report(output) == rows == [{
        "testRunId": "2026-09-22_08-10-38-500-edge-test-2539b3", "status": "FAIL", "browser": "edge",
        "inputPrompt": '--url https://example.test/ --goal Send "Hello, World!"'}]
    assert incomplete == ["2026-09-22_08-41-56-773-chrome-test-38828e"]
    assert not (output / "BUG-REPORT.csv").exists()


def test_the_report_is_rebuilt_from_disk_and_scanning_twice_changes_nothing(output):
    failed = run_folder(output, "2026-09-22_08-11-42-355-edge-test-756d26", "FAILED")
    scan_output.scan(output)
    first = (output.parent / "BUG-REPORT.csv").read_bytes()
    scan_output.scan(output)
    assert (output.parent / "BUG-REPORT.csv").read_bytes() == first
    (failed / "log.txt").write_text("RESULT: PASSED\n")  # a rerun or fix: the row disappears on the next scan
    scan_output.scan(output)
    assert report(output) == []


def test_the_browser_falls_back_to_the_folder_name(output):
    run_folder(output, "2026-09-22_07-50-24-876-firefox-test-790283", "FAILED", browser="")
    assert scan_output.scan(output)[0][0]["browser"] == "firefox"


def test_the_default_report_is_at_the_repository_root():
    assert scan_output.report_path() == Path(__file__).resolve().parents[1] / "BUG-REPORT.csv"


def test_every_finished_run_updates_the_report(output):
    run = RunOutput("https://example.test/", "Open chat", runner="t", root=output)
    run.finish(STATE, passed=False)
    assert [row["testRunId"] for row in report(output)] == [run.name]
