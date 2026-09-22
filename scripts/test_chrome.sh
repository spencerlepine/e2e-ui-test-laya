#!/usr/bin/env bash
# A separate Chrome for UI tests: its own profile, a DevTools port, and fake media.
# Microphone and camera are allowed without a prompt, and pages get a synthetic microphone and camera
# (a tone and a test pattern), so a test never uses your real devices. Every other permission prompt
# (notifications, location, ...) is denied without asking. Your everyday Chrome and its
# permissions are untouched. The agent uses it when .env sets BU_CDP_URL=http://127.0.0.1:9333 and BU_NAME=laya-test.
#
#   scripts/test_chrome.sh          start it (no-op if it is already running)
#   scripts/test_chrome.sh --stop   quit it
set -euo pipefail

PORT="${TEST_CHROME_PORT:-9333}"
PROFILE="${TEST_CHROME_PROFILE:-$HOME/.laya-ultrafast/test-chrome}"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

running() { curl -sf -m 1 "http://127.0.0.1:$PORT/json/version" >/dev/null; }

if [[ "${1:-}" == "--stop" ]]; then
  pkill -f -- "--user-data-dir=$PROFILE" && echo "Test Chrome stopped." || echo "Test Chrome was not running."
  exit 0
fi
if running; then echo "Test Chrome already running on port $PORT."; exit 0; fi

mkdir -p "$PROFILE"
nohup "$CHROME" \
  --user-data-dir="$PROFILE" \
  --remote-debugging-port="$PORT" \
  --use-fake-ui-for-media-stream \
  --use-fake-device-for-media-stream \
  --deny-permission-prompts \
  --no-first-run --no-default-browser-check \
  about:blank >/dev/null 2>&1 &

for _ in $(seq 1 30); do running && { echo "Test Chrome running on port $PORT (profile $PROFILE)."; exit 0; }; sleep 0.5; done
echo "Test Chrome did not start on port $PORT." >&2
exit 1
