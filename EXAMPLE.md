uv run --env-file .env python examples/chat_widget.py \
 --url 'https://tinyurl.com/ye29e563' \
 --goal 'Launch the chat widget, start a conversation, send "Hello, World!", end the chat, close the widget.' \
 --expect 'Hello, World!' --expect-ended --expect-closed
