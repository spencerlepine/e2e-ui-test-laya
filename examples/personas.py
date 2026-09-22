"""Which persona (foundation script) a tab's URL belongs to. Each persona module has the same interface:

  NAME           the default tab name ("agent chat UI")
  matches(url)   whether a URL is this persona's page
  probe(...)     the page's state after a step, read straight from the browser, for independent checks
  prime          None, or prime(browser, say) to put the page in a known state before the agent's first action

  https://<alias>.my.connect.aws/ccp-v2...   agent chat UI     examples/agent_chat_ui.py
  https://<alias>.my.connect.aws/agent...    agent workspace   examples/agent_workspace.py
  any other site                             chat widget       examples/chat_widget.py
Other my.connect.aws pages have no persona yet: they get the generic page probe and no priming.
"""

import sys

import agent_chat_ui
import agent_workspace
import chat_widget

PERSONAS = (agent_chat_ui, agent_workspace, chat_widget)


class Page:
    """No persona: the page's own text and frames are still probed, and nothing is primed."""

    NAME, prime = "page", None
    probe = staticmethod(agent_chat_ui.probe)


def persona(url):
    return next((p for p in PERSONAS if p.matches(url)), Page)


if __name__ == "__main__":
    for url in sys.argv[1:]:
        print(f"{url}: {persona(url).NAME}")
