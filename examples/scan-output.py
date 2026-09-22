"""uv run python examples/scan-output.py [RUN_ID ...]

Scans every run folder in output/ and rewrites BUG-REPORT.csv at the repository root (next to output/) with one row
per failing run: testRunId,status,browser,inputPrompt. A run fails when its log.txt ends with RESULT: FAILED. The
report is rebuilt from scratch on every scan, so it always matches the folders on disk and running it twice changes
nothing. Folders without a RESULT line (a run still going, or one killed before it finished) are not reported.

With RUN_IDs (for example 2026-09-22_07-24-17-660-chrome-test-157f6e), it also prints each one's status. Every test
run calls scan() once it has written its log.txt."""

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "output"
REPORT = "BUG-REPORT.csv"  # written next to the output folder, so output/ -> the repository root
FIELDS = ["testRunId", "status", "browser", "inputPrompt"]


def status(folder):
    """PASS, FAIL, or None when the run has no result yet."""
    try:
        lines = (folder / "log.txt").read_text().splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        if line.startswith("RESULT:"):
            return "PASS" if line.split(":", 1)[1].strip() == "PASSED" else "FAIL"
    return None


def browser(folder):
    """From log.txt's header ("browser: edge 153.0"), else from the folder name (...-edge-test-...)."""
    try:
        for line in (folder / "log.txt").read_text().splitlines()[:10]:
            if line.startswith("browser:") and line.split(":", 1)[1].split():
                return line.split(":", 1)[1].split()[0]
    except OSError:
        pass
    parts = folder.name.split("-")
    return parts[parts.index("test") - 1] if "test" in parts[1:] else ""


def prompt(folder):
    try:
        return " ".join((folder / "prompt.txt").read_text().split())
    except OSError:
        return ""


def scan(root=ROOT):
    """Rewrite BUG-REPORT.csv next to root from the run folders in root. Returns the failing rows and incomplete ids."""
    root = Path(root)
    rows, incomplete = [], []
    for folder in sorted(path for path in root.iterdir() if path.is_dir()) if root.is_dir() else []:
        result = status(folder)
        if result is None:
            incomplete.append(folder.name)
        elif result == "FAIL":
            rows.append({"testRunId": folder.name, "status": "FAIL", "browser": browser(folder),
                         "inputPrompt": prompt(folder)})
    with open(report_path(root), "w", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return rows, incomplete


def report_path(root=ROOT):
    return Path(root).resolve().parent / REPORT


def main(run_ids):
    rows, incomplete = scan()
    for run_id in run_ids:
        folder = ROOT / run_id
        if not folder.is_dir():
            print(f"{run_id}: no such run folder ({folder})")
        else:
            print(f"{run_id}: {status(folder) or 'INCOMPLETE (no RESULT line in log.txt)'}")
    print(f"{report_path()}: {len(rows)} failing run(s)"
          + (f"; {len(incomplete)} incomplete, not reported" if incomplete else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
