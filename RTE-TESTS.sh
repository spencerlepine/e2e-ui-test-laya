#!/usr/bin/env bash
# Rich text editor bug bash. Each TESTS entry is a pair: GOAL, EXPECTED.
# EXPECTED: "" = nothing should be sent (no --expect); "A|B" = several --expect flags.
# Usage: ./RTE-TESTS.sh          run all
#        ./RTE-TESTS.sh 2 10     run tests 2 and 10 only
#
# The agent can only click and type (typing selects the whole box, then inserts the text in one go). It cannot
# press keys, select part of the text, or use a real IME, so check these by hand:
#   - Enter sends; Shift+Enter adds a new line; Tab to Send + Enter/Space sends
#   - multi-item lists (Enter between items); bold on part of a message; select text, then Bold or Link
#   - type, delete everything, click Send: nothing is sent
#   - Korean/Japanese with a real IME: click Send or press Enter while the last syllable is still being composed
cd "$(dirname "$0")"

URL='https://tinyurl.com/ye29e563'
LONG=$(printf 'This is a long message test. %.0s' {1..15}); LONG=${LONG% }
LONG_KO=$(printf '한국어 긴 메시지 테스트입니다. %.0s' {1..10}); LONG_KO=${LONG_KO% }

TESTS=(
  # "Launch the chat widget and start a conversation. Confirm the message box shows the placeholder 'Type a message' and the toolbar shows Bold, Italic, Numbered list, Bulleted list, Link, Emoji and Attach buttons. End the chat, close the widget."
  # ""
  # "Launch the chat widget, start a conversation, type 'Hello, World!' in the message box and click the Send button. Confirm 'Hello, World!' appears in the transcript and the message box is empty. End the chat, close the widget."
  # "Hello, World!"
  # "Launch the chat widget, start a conversation, send 'First message', then send 'Second message'. Confirm both appear in the transcript in that order and the message box is empty. End the chat, close the widget."
  # "First message|Second message"
  # "Launch the chat widget, start a conversation, click the Send button with the message box empty. Confirm no new message appears in the transcript. End the chat, close the widget."
  # ""
  # "Launch the chat widget, start a conversation, type only spaces in the message box and click Send. Confirm no new message appears in the transcript. End the chat, close the widget."
  # ""
  # "Launch the chat widget, start a conversation, type '   Padded text   ' in the message box and click Send. Confirm 'Padded text' appears in the transcript. End the chat, close the widget."
  # "Padded text"
  # "Launch the chat widget, start a conversation, click the Bold button, type 'Bold text' and click Send. Confirm 'Bold text' appears in the transcript in bold. End the chat, close the widget."
  # "Bold text"
  # "Launch the chat widget, start a conversation, click the Italic button, type 'Italic text' and click Send. Confirm 'Italic text' appears in the transcript in italics. End the chat, close the widget."
  # "Italic text"
  # "Launch the chat widget, start a conversation, click the Bold button, click the Italic button, type 'Bold italic' and click Send. Confirm 'Bold italic' appears in the transcript in bold italics. End the chat, close the widget."
  # "Bold italic"
  # "Launch the chat widget, start a conversation, click the Bold button twice, type 'Plain text' and click Send. Confirm 'Plain text' appears in the transcript and is not bold. End the chat, close the widget."
  # "Plain text"
  # "Launch the chat widget, start a conversation, click the Bold button, type 'Bold message' and click Send. Then type 'Next message' and click Send. Confirm 'Next message' appears in the transcript and is not bold. End the chat, close the widget."
  # "Bold message|Next message"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, type 'Apple' and click Send. Confirm the transcript shows 'Apple' as a bulleted list item. End the chat, close the widget."
  # "Apple"
  # "Launch the chat widget, start a conversation, click the Numbered list button, type 'Only item' and click Send. Confirm the transcript shows '1. Only item' as a numbered list. End the chat, close the widget."
  # "Only item"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, click the Bulleted list button again, type 'Not a list' and click Send. Confirm 'Not a list' appears in the transcript without a bullet. End the chat, close the widget."
  # "Not a list"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, type 'One item' and click Send. Then type 'Plain after list' and click Send. Confirm 'Plain after list' appears in the transcript without a bullet. End the chat, close the widget."
  # "One item|Plain after list"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, click the Bold button, type 'Bold bullet' and click Send. Confirm the transcript shows a bullet with 'Bold bullet' in bold. End the chat, close the widget."
  # "Bold bullet"
  # "Launch the chat widget, start a conversation, click the Link button and add a link to https://example.com with the text 'Example site', then click Send. Confirm 'Example site' appears in the transcript as a clickable link. End the chat, close the widget."
  # "Example site"
  # "Launch the chat widget, start a conversation, click the Link button, then cancel or close the link dialog without entering a URL. Confirm the message box is still usable by sending 'After link cancel' and seeing it in the transcript. End the chat, close the widget."
  # "After link cancel"
  # "Launch the chat widget, start a conversation, type 'See https://example.com' in the message box and click Send. Confirm the transcript shows 'https://example.com' as a clickable link. End the chat, close the widget."
  # "See https://example.com"
  # "Launch the chat widget and start a conversation. Click the Emoji button and confirm an emoji picker opens. Click the Emoji button again and confirm the picker closes. End the chat, close the widget."
  # ""
  # "Launch the chat widget, start a conversation, open the emoji picker, and send the smiley face emoji. End the chat, close the widget."
  # "😃"
  # "Launch the chat widget, start a conversation, type 'Nice work ', open the emoji picker, pick the thumbs up emoji, and click Send. Confirm the transcript shows 'Nice work 👍' in one message. End the chat, close the widget."
  # "Nice work 👍"
  # "Launch the chat widget, start a conversation, open the emoji picker and pick the smiley face emoji twice, then click Send. Confirm the transcript shows '😃😃'. End the chat, close the widget."
  # "😃😃"
  # "Launch the chat widget, start a conversation, open the emoji picker, pick the smiley face emoji, and click Send. Confirm the emoji picker is closed and the message box is empty. End the chat, close the widget."
  # "😃"
  # "Launch the chat widget, start a conversation, type 'Café déjà vu 你好 👋' in the message box and click Send. Confirm the transcript shows 'Café déjà vu 你好 👋' exactly. End the chat, close the widget."
  # "Café déjà vu 你好 👋"
  # "Launch the chat widget, start a conversation, type 'Price: \$5 & 10% off <today>' in the message box and click Send. Confirm the transcript shows 'Price: \$5 & 10% off <today>' exactly. End the chat, close the widget."
  # "Price: \$5 & 10% off <today>"
  # "Launch the chat widget, start a conversation, type '*not italic* and **not bold**' in the message box and click Send. Confirm the transcript shows the asterisks exactly as typed. End the chat, close the widget."
  # "*not italic* and **not bold**"
  # "Launch the chat widget, start a conversation, type '$LONG' in the message box and click Send. Confirm the full message appears in the transcript, wrapped inside the widget, and the message box is empty. End the chat, close the widget."
  # "$LONG"
  # "Launch the chat widget, start a conversation, click the Bold button, type 'Draft', then replace the message box text with 'Final text' and click Send. Confirm 'Final text' appears in the transcript. End the chat, close the widget."
  # "Final text"
  # "Launch the chat widget and start a conversation. Click the Attach (paperclip) button and confirm a file chooser opens or the widget asks for a file. Cancel it, then send 'After attach cancel' and confirm it appears in the transcript. End the chat, close the widget."
  # "After attach cancel"
  # "Launch the chat widget, start a conversation, type 'Draft message' in the message box without sending, then end the chat. Confirm the message box and formatting toolbar are no longer shown after the chat ends and 'Draft message' does not appear in the transcript. Close the widget."
  # ""
  # "Launch the chat widget, start a conversation, send 'Before reopen', then close the widget and open it again. Confirm 'Before reopen' is still in the transcript and the message box is empty. End the chat, close the widget."
  # "Before reopen"

  # --- Korean (Hangul) ---
  "Launch the chat widget, start a conversation, type '안녕하세요' in the message box exactly as written, then click Send. Confirm the transcript shows '안녕하세요' exactly and the message box is empty. End the chat, close the widget."
  "안녕하세요"
  "Launch the chat widget, start a conversation, type '네' in the message box exactly as written, then click Send. Confirm the transcript shows '네' exactly and the message box is empty. End the chat, close the widget."
  "네"
  "Launch the chat widget, start a conversation, type '서울역 닭갈비 값' in the message box exactly as written, then click Send. Confirm the transcript shows '서울역 닭갈비 값' exactly and the message box is empty. End the chat, close the widget."
  "서울역 닭갈비 값"
  "Launch the chat widget, start a conversation, type '가 힣 똠 뷁 쀍' in the message box exactly as written, then click Send. Confirm the transcript shows '가 힣 똠 뷁 쀍' exactly and the message box is empty. End the chat, close the widget."
  "가 힣 똠 뷁 쀍"
  "Launch the chat widget, start a conversation, type 'ㅋㅋㅋ ㅠㅠ' in the message box exactly as written, then click Send. Confirm the transcript shows 'ㅋㅋㅋ ㅠㅠ' exactly and the message box is empty. End the chat, close the widget."
  "ㅋㅋㅋ ㅠㅠ"
  # "Launch the chat widget, start a conversation, type '주문번호 A123 확인 부탁드립니다' in the message box exactly as written, then click Send. Confirm the transcript shows '주문번호 A123 확인 부탁드립니다' exactly and the message box is empty. End the chat, close the widget."
  # "주문번호 A123 확인 부탁드립니다"
  # "Launch the chat widget, start a conversation, type '가격은 5,000원입니까?!' in the message box exactly as written, then click Send. Confirm the transcript shows '가격은 5,000원입니까?!' exactly and the message box is empty. End the chat, close the widget."
  # "가격은 5,000원입니까?!"
  # "Launch the chat widget, start a conversation, type '   공백 테스트   ' in the message box exactly as written, then click Send. Confirm the transcript shows '공백 테스트' exactly and the message box is empty. End the chat, close the widget."
  # "공백 테스트"
  # "Launch the chat widget, start a conversation, type '링크 https://example.com' in the message box exactly as written, then click Send. Confirm the transcript shows '링크 https://example.com' exactly and the message box is empty. End the chat, close the widget."
  # "링크 https://example.com"
  # "Launch the chat widget, start a conversation, type '첫 번째 메시지' and click Send, then type '두 번째 메시지' and click Send. Confirm both appear whole in the transcript as separate messages and the message box is empty after each send. End the chat, close the widget."
  # "첫 번째 메시지|두 번째 메시지"
  # "Launch the chat widget, start a conversation, type '안녕하세요' and click Send, then type 'Hello again' and click Send. Confirm both appear whole in the transcript as separate messages and the message box is empty after each send. End the chat, close the widget."
  # "안녕하세요|Hello again"
  # "Launch the chat widget, start a conversation, type 'Hello first' and click Send, then type '안녕하세요' and click Send. Confirm both appear whole in the transcript as separate messages and the message box is empty after each send. End the chat, close the widget."
  # "Hello first|안녕하세요"
  # "Launch the chat widget, start a conversation, click the Bold button, type '굵은 글씨' in the message box exactly as written, then click Send. Confirm the transcript shows '굵은 글씨' exactly and the message box is empty. End the chat, close the widget."
  # "굵은 글씨"
  # "Launch the chat widget, start a conversation, click the Italic button, type '기울임 글씨' in the message box exactly as written, then click Send. Confirm the transcript shows '기울임 글씨' exactly and the message box is empty. End the chat, close the widget."
  # "기울임 글씨"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, type '목록 항목' in the message box exactly as written, then click Send. Confirm the transcript shows '목록 항목' exactly and the message box is empty. End the chat, close the widget."
  # "목록 항목"
  # "Launch the chat widget, start a conversation, click the Numbered list button, type '첫 단계' in the message box exactly as written, then click Send. Confirm the transcript shows '첫 단계' exactly and the message box is empty. End the chat, close the widget."
  # "첫 단계"
  # "Launch the chat widget, start a conversation, type '좋아요 ' in the message box, open the emoji picker, pick the thumbs up emoji, then click Send. Confirm the transcript shows '좋아요 👍' in one message and the message box is empty. End the chat, close the widget."
  # "좋아요 👍"
  # "Launch the chat widget, start a conversation, type '$LONG_KO' in the message box exactly as written, then click Send. Confirm the full message appears in the transcript and the message box is empty. End the chat, close the widget."
  # "$LONG_KO"

  # --- Japanese ---
  # "Launch the chat widget, start a conversation, type 'こんにちは' in the message box exactly as written, then click Send. Confirm the transcript shows 'こんにちは' exactly and the message box is empty. End the chat, close the widget."
  # "こんにちは"
  # "Launch the chat widget, start a conversation, type 'ありがとうございます' in the message box exactly as written, then click Send. Confirm the transcript shows 'ありがとうございます' exactly and the message box is empty. End the chat, close the widget."
  # "ありがとうございます"
  # "Launch the chat widget, start a conversation, type '本を読みたいけど、時間がありません' in the message box exactly as written, then click Send. Confirm the transcript shows '本を読みたいけど、時間がありません' exactly and the message box is empty. End the chat, close the widget."
  # "本を読みたいけど、時間がありません"
  # "Launch the chat widget, start a conversation, type 'ちょっと待ってください' in the message box exactly as written, then click Send. Confirm the transcript shows 'ちょっと待ってください' exactly and the message box is empty. End the chat, close the widget."
  # "ちょっと待ってください"
  # "Launch the chat widget, start a conversation, type 'ラーメンとカタカナのテスト' in the message box exactly as written, then click Send. Confirm the transcript shows 'ラーメンとカタカナのテスト' exactly and the message box is empty. End the chat, close the widget."
  # "ラーメンとカタカナのテスト"
  # "Launch the chat widget, start a conversation, type 'ｶﾀｶﾅ ＡＢＣ１２３' in the message box exactly as written, then click Send. Confirm the transcript shows 'ｶﾀｶﾅ ＡＢＣ１２３' exactly and the message box is empty. End the chat, close the widget."
  # "ｶﾀｶﾅ ＡＢＣ１２３"
  # "Launch the chat widget, start a conversation, type '「はい」、そうです。' in the message box exactly as written, then click Send. Confirm the transcript shows '「はい」、そうです。' exactly and the message box is empty. End the chat, close the widget."
  # "「はい」、そうです。"
  # "Launch the chat widget, start a conversation, type '　全角スペース　' in the message box exactly as written, then click Send. Confirm the transcript shows '全角スペース' exactly and the message box is empty. End the chat, close the widget."
  # "全角スペース"
  # "Launch the chat widget, start a conversation, type only full-width spaces '　　　' in the message box and click Send. Confirm no new message appears in the transcript. End the chat, close the widget."
  # ""
  # "Launch the chat widget, start a conversation, type 'こんにちは' and click Send, then type 'さようなら' and click Send. Confirm both appear whole in the transcript as separate messages and the message box is empty after each send. End the chat, close the widget."
  # "こんにちは|さようなら"
  # "Launch the chat widget, start a conversation, type 'こんにちは' and click Send, then type '안녕하세요' and click Send. Confirm both appear whole in the transcript as separate messages and the message box is empty after each send. End the chat, close the widget."
  # "こんにちは|안녕하세요"
  # "Launch the chat widget, start a conversation, click the Bold button, type '太字のテキスト' in the message box exactly as written, then click Send. Confirm the transcript shows '太字のテキスト' exactly and the message box is empty. End the chat, close the widget."
  # "太字のテキスト"
  # "Launch the chat widget, start a conversation, click the Bulleted list button, type 'リスト項目' in the message box exactly as written, then click Send. Confirm the transcript shows 'リスト項目' exactly and the message box is empty. End the chat, close the widget."
  # "リスト項目"
  # "Launch the chat widget, start a conversation, type 'いいね ' in the message box, open the emoji picker, pick the thumbs up emoji, then click Send. Confirm the transcript shows 'いいね 👍' in one message and the message box is empty. End the chat, close the widget."
  # "いいね 👍"
)

COUNT=$(( ${#TESTS[@]} / 2 ))
ONLY=" $* "
PASSED=(); FAILED=()

for (( n=1; n<=COUNT; n++ )); do
  [[ $# -gt 0 && "$ONLY" != *" $n "* ]] && continue
  GOAL="${TESTS[(n-1)*2]}"
  EXPECTED="${TESTS[(n-1)*2+1]}"

  EXPECT_ARGS=()
  if [[ -n "$EXPECTED" ]]; then
    IFS='|' read -ra PARTS <<< "$EXPECTED"
    for p in "${PARTS[@]}"; do EXPECT_ARGS+=(--expect "$p"); done
  fi

  echo "=== Test $n/$COUNT: $GOAL"
  echo "=== Expect: ${EXPECTED:-(nothing sent)}"
  if uv run --env-file .env python examples/chat_widget.py \
      --url "$URL" \
      --goal "$GOAL" \
      "${EXPECT_ARGS[@]}" --expect-ended --expect-closed; then
    PASSED+=("$n")
  else
    FAILED+=("$n")
  fi
done

echo
echo "Passed (${#PASSED[@]}): ${PASSED[*]}"
echo "Failed (${#FAILED[@]}): ${FAILED[*]}"
[[ ${#FAILED[@]} -eq 0 ]]
