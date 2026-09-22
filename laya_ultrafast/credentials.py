"""Credentials a goal names by reference, such as {{CCP_AGENTCHATUI_PASSWORD}}. Their values stay server-side.

The goal, the plan, the text model, decisions, logs and traces only ever hold the reference. The executor swaps in the
value from the environment just before input. Only names listed in LAYA_CREDENTIALS can be referenced, so a goal cannot
type the agent's own settings (an API key) into a page. A credential named like a password is typed only into a
password field, and a password field only ever receives such a credential. Snapshots never read a password's value.
"""

import os
import re

REFERENCE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)\}\}")
SECRET_NAME = re.compile(r"PASSWORD|PASSCODE|SECRET|TOKEN")


def allowed():
    return {name.strip() for name in os.environ.get("LAYA_CREDENTIALS", "").split(",") if name.strip()}


def references(text):
    return REFERENCE.findall(text or "")


def secret(text):
    """The text is exactly one reference to a password-like credential."""
    match = REFERENCE.fullmatch((text or "").strip())
    return bool(match and SECRET_NAME.search(match[1]))


def prepare(text, action):
    """The text to type into `action`, with references replaced by their values. Refuses before any input."""
    names = references(text)
    if action.get("secret") and not secret(text):
        raise ValueError("A password field only receives a password credential; nothing typed.")
    if not names:
        return text
    if secret(text) != bool(action.get("secret")):
        raise ValueError("A password credential is typed only into a password field; nothing typed.")
    missing = [name for name in names if name not in allowed() or not os.environ.get(name)]
    if missing:
        raise ValueError(f"Credential {missing[0]} is not set or not listed in LAYA_CREDENTIALS; nothing typed.")
    return REFERENCE.sub(lambda m: os.environ[m[1]], text)
