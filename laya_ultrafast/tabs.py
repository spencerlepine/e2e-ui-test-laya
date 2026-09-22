"""Several named tabs observed and driven as one page: one goal, one plan, one log, and one video per tab.

Every tab is its own Chrome tab and CDP session (Browser). An observation reads each tab and merges their controls:
each keeps its tab's name, gets an id and node that are unique across tabs ("t2.e5"), and is executed in its own tab
with that tab's own freshness checks. The policy decides which tab each step acts on; this class only routes.
"""

import hashlib
import os
import time

from browser_harness.helpers import cdp

from .browser import Browser, StalePage

SLOTS = 64  # a merged node is node * SLOTS + tab index; nodes stay ints, unique across tabs, and keep their sign
WAIT = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}


def parse(values):
    """`--tab NAME=URL` values as [(name, url)]. Names must be distinct; the goal refers to tabs by name."""
    tabs = []
    for value in values:
        name, sep, url = value.partition("=")
        if not sep or not name.strip() or not url.strip().startswith(("http://", "https://")):
            raise ValueError(f"Expected --tab NAME=URL, got {value!r}")
        tabs.append((name.strip(), url.strip()))
    if len({name for name, _ in tabs}) != len(tabs):
        raise ValueError("Tab names must be distinct")
    if not 1 <= len(tabs) <= SLOTS:
        raise ValueError(f"Give 1 to {SLOTS} tabs")
    return tabs


class Tabs:
    kind = "chrome"

    def __init__(self, tabs):
        self.names, self.urls, self.tabs = [name for name, _ in tabs], [url for _, url in tabs], []
        # HEADLESS_MODE=false brings the tab the agent acts in to the front, so a person watches it switch.
        self.watch = os.environ.get("HEADLESS_MODE", "true").strip().lower() == "false"
        self.focus = 0  # the tab of the latest action
        try:
            for url in self.urls:
                self.tabs.append(Browser(url))
        except Exception:
            self.close()
            raise

    @property
    def target(self):
        return self.tabs[0].target if self.tabs else None

    def tab(self, name):
        return self.tabs[self.names.index(name)]

    def observe(self, screenshot=True):
        pages = [tab.observe(screenshot=screenshot and i == self.focus) for i, tab in enumerate(self.tabs)]
        actions = []
        for i, (name, page) in enumerate(zip(self.names, pages)):
            actions += [{**a, "id": f"t{i + 1}.{a['id']}", "node": a["node"] * SLOTS + i, "tab": name}
                        for a in page["actions"] if a["kind"] in {"click", "fill", "select"}]
        focus = pages[self.focus]
        merged = {
            "url": focus["url"], "title": focus["title"], "scroll": focus["scroll"],
            "text": "\n".join(f"[{name}]\n{page['text']}" for name, page in zip(self.names, pages)),
            "tabs": [{"name": name, "url": page["url"], "title": page["title"], "text": page["text"]}
                     for name, page in zip(self.names, pages)],
            "actions": actions + [WAIT], "pages": pages,
            "fingerprint": hashlib.sha256("".join(page["fingerprint"] for page in pages).encode()).hexdigest(),
        }
        if screenshot and "screenshot" in focus:
            merged["screenshot"] = focus["screenshot"]
        return merged

    def locate(self, action, page):
        """The tab an action belongs to and the action as that tab observed it."""
        prefix, _, local = action["id"].partition(".")
        i = int(prefix[1:]) - 1
        return i, next(a for a in page["pages"][i]["actions"] if a["id"] == local)

    def fresh(self, page, action=None):
        if action is not None and action["kind"] != "wait":
            i, local = self.locate(action, page)
            return self.tabs[i].fresh(page["pages"][i], local)
        return all(tab.fresh(p) for tab, p in zip(self.tabs, page["pages"]))

    def act(self, action, page, text=None):
        if action["kind"] == "wait":
            time.sleep(0.1)  # nothing to change; every tab is observed again next
            return {"executed": "wait"}
        i, local = self.locate(action, page)
        if not self.tabs[i].fresh(page["pages"][i], local):
            raise StalePage("Page changed since this decision. Observe again.")
        if self.watch and i != self.focus:
            cdp("Target.activateTarget", targetId=self.tabs[i].target)
        self.focus = i
        return self.tabs[i].act(local, page["pages"][i], text=text)

    def close(self):
        for tab in self.tabs:
            try:
                tab.close()
            except Exception:
                pass  # one tab failing to close must not leave the others open
