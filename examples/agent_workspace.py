"""Persona: the agent in the Amazon Connect agent workspace (any https://<alias>.my.connect.aws/agent page).

uv run --env-file .env python examples/agent_workspace.py                  prime only, then exit
uv run --env-file .env python examples/agent_workspace.py --goal GOAL      prime, then run GOAL on this page alone

The workspace embeds the agent chat UI (CCP v2) in same-origin iframes, so the agent sees the same controls with the
same labels ("Available", "Accept chat", "Close contact") inside a larger page with Customer Profiles, Cases and the
Connect assistant. Priming and checks are the agent chat UI's (examples/agent_chat_ui.py); this file is where
workspace-only fixtures and checks belong. Only one agent page per login at a time: do not open this page and
/ccp-v2 as the same agent in one test.
"""

import agent_chat_ui  # examples/ is this script's folder, so its sibling personas are importable

NAME = "agent workspace"
URL = "https://spenlep.my.connect.aws/agent"
probe, prime = agent_chat_ui.probe, agent_chat_ui.prime


def matches(url):
    return agent_chat_ui.connect_page(url, "/agent")


if __name__ == "__main__":
    agent_chat_ui.main(NAME, URL)
