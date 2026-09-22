"""Offline contract for examples/personas.py: a tab's URL picks its persona (foundation script)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1] / "examples"))
import personas  # noqa: E402


@pytest.mark.parametrize("url, name", [
    ("https://tinyurl.com/ye29e563", "customer widget"),
    ("https://d3e7jcfgb1hla3.cloudfront.net/?snippetCode=x", "customer widget"),
    ("https://spenlep.my.connect.aws/ccp-v2", "agent chat UI"),
    ("https://spenlep.my.connect.aws/ccp-v2/chat#contact", "agent chat UI"),
    ("https://spenlep.my.connect.aws/agent", "agent workspace"),
    ("https://spenlep.my.connect.aws/agent/", "agent workspace"),
    ("https://spenlep.my.connect.aws/agentic", "page"),
    ("https://spenlep.my.connect.aws/home", "page"),
])
def test_a_url_picks_its_persona(url, name):
    assert personas.persona(url).NAME == name


def test_only_agent_personas_are_primed():
    assert personas.persona("https://tinyurl.com/x").prime is None
    assert personas.persona("https://a.my.connect.aws/ccp-v2").prime is not None
    assert personas.persona("https://a.my.connect.aws/agent").prime is not None
    assert personas.persona("https://a.my.connect.aws/home").prime is None
