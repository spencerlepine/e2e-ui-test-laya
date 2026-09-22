#!/usr/bin/env bash
# A separate Microsoft Edge for UI tests, like scripts/test_chrome.sh: its own profile, a DevTools port, and fake media.
# Edge is Chromium, so the agent drives it exactly like the test Chrome (CDP through Browser Harness). Microphone and
# camera are allowed without a prompt and replaced with synthetic devices; every other permission prompt is denied.
# Your everyday Edge and its permissions are untouched. Runs use it when .env sets EDGE_ENABLED=true.
#
#   scripts/test_edge.sh          start it (no-op if it is already running)
#   scripts/test_edge.sh --stop   quit it
set -euo pipefail

PORT="${TEST_EDGE_PORT:-9334}"
PROFILE="${TEST_EDGE_PROFILE:-$HOME/.laya-ultrafast/test-edge}"
EDGE="/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"

running() { curl -sf -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null; }

if [[ "${1:-}" == "--stop" ]]; then
  pkill -f -- "--user-data-dir=$PROFILE" && echo "Test Edge stopped." || echo "Test Edge was not running."
  exit 0
fi
if running; then echo "Test Edge already running on port $PORT."; exit 0; fi
[[ -x "$EDGE" ]] || { echo "Microsoft Edge is not installed at $EDGE." >&2; exit 1; }

mkdir -p "$PROFILE"
nohup "$EDGE" \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port="$PORT" \
  --use-fake-ui-for-media-stream \
  --use-fake-device-for-media-stream \
  --deny-permission-prompts \
  --no-first-run --no-default-browser-check \
  about:blank >/dev/null 2>&1 &

for _ in $(seq 1 30); do running && { echo "Test Edge running on port $PORT (profile $PROFILE)."; exit 0; }; sleep 0.5; done
echo "Test Edge did not start on port $PORT." >&2
exit 1
