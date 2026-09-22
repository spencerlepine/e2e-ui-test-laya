"""Run output for review: one folder per test run with its prompt, a log for AI agents, and a video for people.

    output/<YYYY-MM-DD_HH-MM-SS-mmm>-<chrome|firefox>-test-<id>/
      prompt.txt    the --url and --goal input, as given
      log.txt       intent, expected and actual results, and every executed step (for an AI agent)
      capture.mov   the agent's own tab at original speed, from its first action (after planning) to the end, for a
                    person; RECORD_VIDEO=false turns it off

In Chrome the video is Chrome's screencast of the agent's tab, read over the recorder's own CDP connection. It records
the tab whether it is in front or in the background, never sends input, and never shares the Browser Harness event
buffer, so parallel runs cannot take each other's frames. In Firefox it is Firefox's own WebDriver BiDi screencast of
the run's private Firefox. Recording failures are logged and never change a test's result.
"""

import base64
import datetime
import importlib.util
import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

OUTPUT_ROOT = Path("output")
FPS = 30
MAX_SIZE = (1120, 780)  # the agent's viewport (browser.py) at 1x; a Retina display would otherwise double it


def video_enabled():
    return os.environ.get("RECORD_VIDEO", "true").strip().lower() != "false"


def browser_ws_url():
    from browser_harness.daemon import get_ws_url  # the same Chrome endpoint Browser Harness connects to

    return get_ws_url()


class TabRecorder:
    """Screencast one tab to capture.mov at its original timing. Only reads the tab; errors are kept, not raised."""

    def __init__(self, target_id, frames_dir):
        self.target_id, self.frames_dir = target_id, Path(frames_dir)
        self.frames = []  # (wall-clock seconds, file name)
        self.errors, self.started, self.stopped, self._session = [], None, None, None
        self._stop, self._ready = threading.Event(), threading.Event()
        self._thread = threading.Thread(target=self._run, name=f"record-{target_id[:8]}", daemon=True)

    def start(self, timeout=5):
        self._thread.start()
        self._ready.wait(timeout)
        return not self.errors

    def stop(self, timeout=5):
        self._stop.set()
        self._thread.join(timeout)
        self.stopped = self.stopped or time.time()

    def _save(self, timestamp, data):
        name = f"{len(self.frames):06d}.jpg"
        (self.frames_dir / name).write_bytes(base64.b64decode(data))
        self.frames.append((timestamp, name))

    def _run(self):
        from websockets.sync.client import connect

        ids = iter(range(1, 1 << 30))
        try:
            with connect(browser_ws_url(), max_size=None, open_timeout=5) as ws:
                def call(method, session=None, **params):
                    message = {"id": next(ids), "method": method, "params": params}
                    if session:
                        message["sessionId"] = session
                    ws.send(json.dumps(message))
                    return message["id"]

                def wait(request, deadline=5):
                    end = time.monotonic() + deadline
                    while time.monotonic() < end:
                        message = json.loads(ws.recv(timeout=max(0.01, end - time.monotonic())))
                        if message.get("id") == request:
                            if "error" in message:
                                raise RuntimeError(f"{message['error'].get('message')}")
                            return message.get("result", {})
                        self._event(message, call)
                    raise TimeoutError("Chrome did not answer the recorder")

                session = wait(call("Target.attachToTarget", targetId=self.target_id, flatten=True))["sessionId"]
                self._session = session
                # A first frame now, so the video starts when recording starts even on a page that is not painting.
                self.started = time.time()
                shot = wait(call("Page.captureScreenshot", session, format="jpeg", quality=80))
                self._save(self.started, shot["data"])
                wait(call("Page.startScreencast", session, format="jpeg", quality=80, everyNthFrame=1,
                          maxWidth=MAX_SIZE[0], maxHeight=MAX_SIZE[1]))
                self._ready.set()
                while not self._stop.is_set():
                    try:
                        self._event(json.loads(ws.recv(timeout=0.05)), call)
                    except TimeoutError:
                        continue
                self.stopped = time.time()
                try:
                    wait(call("Page.stopScreencast", session), deadline=1)
                except Exception:
                    pass  # the tab may already be closing
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
        finally:
            self.stopped = self.stopped or time.time()
            self._ready.set()

    def _event(self, message, call):
        if message.get("method") != "Page.screencastFrame" or message.get("sessionId") != self._session:
            return
        params = message["params"]
        call("Page.screencastFrameAck", self._session, sessionId=params["sessionId"])
        timestamp = params["metadata"].get("timestamp") or time.time()
        if not self._stop.is_set():
            self._save(max(timestamp, self.started), params["data"])

    def encode(self, path):
        """Write the frames as a constant-rate H.264 .mov, each frame shown until the next one arrived."""
        if not self.frames:
            raise RuntimeError("no frames were captured")
        write_concat(self.frames, self.stopped, self.frames_dir / "frames.txt")
        encode_mov(["-f", "concat", "-safe", "0", "-i", str(self.frames_dir / "frames.txt")], path,
                   duration=self.stopped - self.frames[0][0])


class FirefoxRecorder:
    """Firefox's own screencast of the agent's tab (WebDriver BiDi browsingContext.startScreencast), converted to
    capture.mov. Firefox writes a .webm into the run's throwaway profile; it is removed with the profile."""

    def __init__(self, browser):
        self.browser, self.errors, self.frames = browser, [], None
        self.started = self.stopped = self.id = self.webm = None

    def start(self):
        try:
            result = self.browser.call("browsingContext.startScreencast", context=self.browser.target)
            self.id, self.webm, self.started = result["screencast"], result["path"], time.time()
        except Exception as error:
            self.errors.append(f"{type(error).__name__}: {error}")
        return not self.errors

    def stop(self):
        if self.id and not self.stopped:
            try:
                self.webm = self.browser.call("browsingContext.stopScreencast", screencast=self.id)["path"]
            except Exception as error:
                self.errors.append(f"{type(error).__name__}: {error}")
            self.stopped = time.time()

    def encode(self, path):
        if not self.webm or not Path(self.webm).stat().st_size:
            raise RuntimeError("Firefox wrote no screencast")
        encode_mov(["-i", self.webm], path)


def encode_mov(inputs, path, duration=None):
    """H.264 .mov at the agent's viewport size: the Mac's hardware encoder, or software H.264 without one."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found; install it with `brew install ffmpeg`")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", *inputs,
               "-vf", f"fps={FPS},scale={MAX_SIZE[0]}:{MAX_SIZE[1]}:force_original_aspect_ratio=decrease:"
               "force_divisible_by=2,format=yuv420p",
               *(["-t", f"{duration:.3f}"] if duration else []),
               "-c:v", "h264_videotoolbox", "-q:v", "65", "-movflags", "+faststart", str(path)]
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:  # no hardware encoder available: fall back to software H.264
        command[command.index("h264_videotoolbox") - 1:command.index("65") + 1] = ["-c:v", "libx264", "-crf", "23"]
        result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError(f"ffmpeg failed: {result.stderr.strip()[-300:]}")

def write_concat(frames, stopped, path):
    """An ffmpeg concat list: each frame lasts until the next one; the last lasts until recording stopped.
    Frames closer than one video frame apart share a slot, which shows the newest of them, so the durations add up
    to the real elapsed time and the video never drifts behind the run."""
    slots = []
    for timestamp, name in frames:
        if slots and timestamp - slots[-1][0] < 1 / FPS:
            slots[-1] = (slots[-1][0], name)
        else:
            slots.append((timestamp, name))
    lines = []
    for (timestamp, name), (following, _) in zip(slots, slots[1:] + [(stopped, None)]):
        lines += [f"file '{name}'", f"duration {max(following - timestamp, 0.001):.3f}"]
    lines.append(f"file '{slots[-1][1]}'")  # concat drops the last duration unless its file repeats; -t trims it
    Path(path).write_text("\n".join(lines) + "\n")


class RunOutput:
    """One test run's review folder. start(browser) just before the agent's first action, so the video skips planning
    (pass it as Agent(before_act=...); later calls do nothing); finish() once the result is known."""

    def __init__(self, url, goals, *, runner, expected=(), root=None, now=None, browser="chrome"):
        # Several tabs: url is [(name, url), ...]. Tab 1 records to capture.mov, tab N to capture-tab-N.mov.
        self.tabs = list(url) if isinstance(url, (list, tuple)) else None
        url = "; ".join(f"{name}={address}" for name, address in self.tabs) if self.tabs else url
        self.url, self.goals = url, [goals] if isinstance(goals, str) else list(goals)
        self.runner, self.expected, self.browser, self.browser_version = runner, list(expected), browser, None
        now = now or datetime.datetime.now().astimezone()
        self.started_at = now
        stamp = f"{now:%Y-%m-%d_%H-%M-%S}-{now.microsecond // 1000:03d}"
        self.name = f"{stamp}-{browser}-test-{secrets.token_hex(3)}"
        self.folder = Path(root or OUTPUT_ROOT).resolve() / self.name
        self.folder.mkdir(parents=True, exist_ok=False)
        inputs = [f"--tab {name}={address}" for name, address in self.tabs] if self.tabs else [f"--url {self.url}"]
        (self.folder / "prompt.txt").write_text("".join(f"{line}\n" for line in inputs)
                                                + "".join(f"--goal {goal}\n" for goal in self.goals))
        self.recorder, self.video, self.video_perf = None, "not started (the run stopped before its first action)", None
        self._frames = None
        self.extra = []  # (file name, recorder, frames folder) for tabs 2..N, started with tab 1

    def start(self, browser):
        if self.recorder or self.video.startswith("off"):
            return
        self.browser_version = self.browser_version or browser_version(browser)
        if not video_enabled():
            self.video = "off (RECORD_VIDEO=false)"
            return
        self._frames = tempfile.TemporaryDirectory(prefix="laya-capture-")
        if getattr(browser, "kind", "chrome") == "firefox":
            self.recorder = FirefoxRecorder(browser)
        else:
            self.recorder = TabRecorder(browser.target, self._frames.name)
        for n, tab in enumerate(getattr(browser, "tabs", [])[1:], 2):
            frames = tempfile.TemporaryDirectory(prefix="laya-capture-")
            self.extra.append((f"capture-tab-{n}.mov", TabRecorder(tab.target, frames.name), frames))
        self.video_perf = time.perf_counter()
        for _name, recorder, _frames in self.extra:
            recorder.start()  # its errors are reported on the log's video line
        if not self.recorder.start():
            self.video = f"failed to start: {'; '.join(self.recorder.errors)}"
            return
        self.video = "recording"

    def finish(self, state, *, passed, checks=None, error=None, stale=(), final_url=None):
        """Stop and encode the video, then write log.txt. Returns the folder."""
        if self.recorder:
            self.recorder.stop()
            try:
                if self.recorder.errors:
                    raise RuntimeError("; ".join(self.recorder.errors))
                self.recorder.encode(self.folder / "capture.mov")
                seconds = self.recorder.stopped - self.recorder.started
                frames = f", {len(self.recorder.frames)} frames" if self.recorder.frames is not None else ""
                self.video = f"capture.mov ({seconds:.1f} s{frames})"
                if state.get("started_at") is not None:
                    offset = state["started_at"] - self.video_perf
                    self.video += f", starting at the first action (t={-offset * 1000:.0f}, after planning)"
            except Exception as failure:
                self.video = f"failed: {failure}"
            finally:
                self._frames.cleanup()
        for name, recorder, frames in self.extra:
            recorder.stop()
            try:
                if recorder.errors:
                    raise RuntimeError("; ".join(recorder.errors))
                recorder.encode(self.folder / name)
                self.video += f"; {name} ({recorder.stopped - recorder.started:.1f} s, {len(recorder.frames)} frames)"
            except Exception as failure:
                self.video += f"; {name} failed: {failure}"
            finally:
                frames.cleanup()
        offset = None
        if (self.folder / "capture.mov").exists() and state.get("started_at") is not None:
            offset = state["started_at"] - self.video_perf
        (self.folder / "log.txt").write_text(
            render_log(self, state, passed=passed, checks=checks, error=error, stale=stale, final_url=final_url,
                       video_offset=offset)
        )
        scan_output(self.folder.parent)
        return self.folder


SCAN_OUTPUT = Path(__file__).resolve().parents[1] / "examples" / "scan-output.py"


def scan_output(root):
    """Rebuild BUG-REPORT.csv next to root (examples/scan-output.py) from every run folder in root. Never changes a
    run's result."""
    if not SCAN_OUTPUT.exists():  # an installed package has no examples/
        return
    try:
        spec = importlib.util.spec_from_file_location("scan_output", SCAN_OUTPUT)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.scan(root)
    except Exception as failure:
        print(f"scan-output: {type(failure).__name__}: {failure}", file=sys.stderr)


def browser_version(browser):
    """"firefox 156.0.1" or "chrome 140.0.7339.80"; None if the browser does not say."""
    if getattr(browser, "version", None):
        return browser.version
    try:
        from browser_harness.helpers import cdp

        return cdp("Browser.getVersion")["product"].split("/")[-1]
    except Exception:
        return None


def short(text, limit=160):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def clock(seconds):
    return f"{int(seconds // 60):02d}:{seconds % 60:04.1f}"


def models():
    """The decision and text models this run used, from the environment (.env), with the code's defaults."""
    from .laya import DEFAULT_MODEL

    decision = os.environ.get("DECISION_MODEL", "laya")
    laya_model = os.environ.get("LAYA_MODEL", DEFAULT_MODEL) if decision == "laya" else None
    return laya_model or f"none (DECISION_MODEL={decision})", os.environ.get("TEXT_MODEL", "deepseek-chat")


def render_log(run, state, *, passed, checks, error, stale, final_url, video_offset):
    plan = state.get("goal_plan") or {}
    history = state.get("history", [])
    laya_model, text_model = models()
    lines = [
        "---",
        f"timestamp: {run.started_at.isoformat(timespec='seconds')}",
        f"browser: {run.browser}" + (f" {run.browser_version}" if run.browser_version else ""),
        f"laya-model: {laya_model}",
        f"text-model: {text_model}",
        f"length: {round(state.get('elapsed_ms', 0) / 1000)}s",
        "---",
        "",
        "LAYA ULTRAFAST TEST RUN LOG",
        "Audience: an AI agent reviewing this run. capture.mov is the same run on video; `video` times below are",
        "offsets into it. `t` is milliseconds since the agent's first decision. `length` above is the whole run,",
        "planning included.",
        "",
        "RUN",
        f"  id: {run.name}",
        f"  started: {run.started_at.isoformat(timespec='seconds')}",
        f"  runner: {run.runner}",
        f"  video: {run.video}",
        "",
        "INTENT",
        *([f"  tab {n}: {name} = {short(address, 300)}" for n, (name, address) in enumerate(run.tabs, 1)]
          if run.tabs else [f"  url: {short(run.url, 300)}"]),
        *(f"  goal: {goal}" for goal in run.goals),
    ]
    if plan:
        lines.append("  plan (the text model's one planning call):")
        if plan.get("finish"):
            lines.append(f"    finish when: {plan['finish']}")
        for req in plan.get("requirements") or []:
            lines.append(f"    requirement: {short(json.dumps(req, ensure_ascii=False))}")
        if plan.get("open"):
            lines.append(f"    open: {short(json.dumps(plan['open'], ensure_ascii=False))}")
        for i, step in enumerate(plan.get("steps") or [], 1):
            labels = ", ".join(f'"{label}"' for label in step.get("labels") or [])
            typed = f'  type: "{step["text"]}"' if step.get("text") else ""
            where = f"[{step['tab']}] " if step.get("tab") else ""
            extra = f'  see: "{step["see"]}"' if step.get("see") else ""
            extra += "  (optional)" if step.get("optional") else ""
            lines.append(f"    step {i}: {where}{step.get('do')}" + (f"  (likely labels: {labels})" if labels else "")
                         + typed + extra)
    else:
        lines.append("  plan: none recorded (the run stopped before planning)")
    lines += ["", "EXPECTED"]
    lines += [f"  - {line}" for line in run.expected] or ["  - (no independent checks requested)"]
    lines += ["", "STEPS"]
    if not history:
        lines.append("  (no actions executed)")
    url = run.url
    for step in history:
        video = f" video={clock(video_offset + step['executed_ms'] / 1000)}" if video_offset is not None else ""
        typed = f' typed "{step["text"]}"' if step.get("text") else ""
        changed = {True: "yes", False: "no", None: "unknown"}[step.get("page_changed")]
        lines.append(
            f"  t={step['executed_ms']:>6}{video}  #{step['step']:<2} {step['operation']:<9} "
            + (f"[{step['tab']}] " if step.get("tab") else "") + f"\"{short(step['action'], 90)}\"{typed}"
            f"  confidence={step['confidence']:.2f}  page changed: {changed}"
        )
        if step.get("url") and step["url"] != url:
            url = step["url"]
            lines.append(f"      now at: {short(url, 200)}")
    for item in stale:
        lines.append(f"  stale page after step {item['step']}: {item['error']} (re-observed; nothing executed)")
    actions = [h for h in history if h["kind"] != "wait"]
    lines += [
        "",
        "ACTUAL",
        f"  agent status: {state.get('status')}" + ("  (DONE is the agent's claim, not proof)"
                                                    if state.get("status") == "done" else ""),
        f"  error: {error or 'none'}",
        f"  elapsed: {state.get('elapsed_ms', 0) / 1000:.1f} s  actions: {len(actions)}  "
        f"waits: {len(history) - len(actions)}  decisions: {len(state.get('decisions', []))}  "
        f"text-model calls: {sum(c.get('calls', 1) for c in state.get('text_calls', []))}",
        f"  final url: {short(final_url or (state.get('page') or {}).get('url', ''), 300)}",
    ]
    if checks:
        lines.append("  checks:")
        lines += [f"    {'PASS' if ok else 'FAIL'} {name}" for name, ok in checks.items()]
    lines += ["", f"RESULT: {'PASSED' if passed else 'FAILED'}", ""]
    return "\n".join(lines)
