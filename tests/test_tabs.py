"""Offline contracts for several tabs driven as one page, and for credential references. No browser, no models."""

import pytest
from test_laya import FakeLaya, executed

from laya_ultrafast import credentials, laya, model, tabs
from laya_ultrafast.browser import fingerprint

WAIT = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}


@pytest.fixture
def fake(monkeypatch):
    f = FakeLaya()
    monkeypatch.setattr(laya, "laya", lambda: f)
    return f


def control(tab, i, label, node, kind="click", **extra):
    """An action as Tabs.observe merges it: a tab-qualified id and node, and the tab's name."""
    t = ["customer", "agent"].index(tab)
    role = "textbox" if kind == "fill" else "button"
    return {"id": f"t{t + 1}.e{i}", "kind": kind, "label": label, "role": role, "value": "",
            "node": node * tabs.SLOTS + t, "tab": tab, **extra}


def merged(actions, texts=None):
    texts = texts or {}
    state = {"url": "https://customer.test/", "title": "Customer", "scroll": {"y": 0}, "actions": actions + [WAIT],
             "tabs": [{"name": name, "url": "", "title": "", "text": texts.get(name, "")}
                      for name in ("customer", "agent")]}
    state["text"] = "\n".join(t["text"] for t in state["tabs"])
    state["fingerprint"] = fingerprint(state)
    return state


def policy(steps):
    p = laya.LayaPolicy("A goal across tabs.", tabs=["customer", "agent"])
    p.plan = {"requirements": [], "open": None, "finish": "Done.", "steps": steps}
    p.plan_meta = {"model": "test", "latency_ms": 1}
    return p


def step(tab, do, labels, text=None, see=None, optional=False):
    return {"do": do, "labels": labels, "text": text, "tab": tab, "see": see, "optional": optional}


def settle(p, history, actions, texts=None, limit=6):
    for _ in range(limit):
        d = p.choose(merged(actions, texts), history)
        executed(history, d)
        if d["choice"] != "wait":
            return d
    return d


def test_a_step_acts_only_on_its_own_tab(fake):
    p, history = policy([step("agent", "send the reply", ["Send"])]), []
    actions = [control("customer", 1, "Send", 1), control("agent", 1, "Send", 1)]
    assert settle(p, history, actions)["choice"] == "t2.e1"  # the same label on the other tab is not this step


def test_the_agent_switches_tabs_as_the_steps_require(fake):
    p, history = policy([step("customer", "start the chat", ["Start Chat"]),
                         step("agent", "accept the chat", ["Accept chat"])]), []
    widget = control("customer", 1, "Start Chat", 1)
    assert settle(p, history, [widget])["choice"] == "t1.e1"
    d = settle(p, history, [control("customer", 2, "End chat", 2), control("agent", 1, "Accept chat", 5)])
    assert d["choice"] == "t2.e1" and p.step == 1


def test_a_step_waits_to_see_text_on_its_tab(fake):
    p, history = policy([step("customer", "wait for the reply", [], see="Hello from the agent"),
                         step("customer", "end the chat", ["End chat"])]), []
    actions = [control("customer", 1, "End chat", 1)]
    for _ in range(5):  # the reply is only on the agent's own tab so far
        d = p.choose(merged(actions, {"agent": "Hello from the agent"}), history)
        executed(history, d)
        assert d["choice"] == "wait"
    assert settle(p, history, actions, {"customer": "Agent: Hello from the agent"})["choice"] == "t1.e1"


def test_an_optional_step_without_its_control_is_not_needed(fake):
    p, history = policy([step("agent", "type the username", ["Username"], text="{{CCP_USER}}", optional=True),
                         step("agent", "sign in", ["Sign in"], optional=True),
                         step("agent", "accept the chat", ["Accept chat"])]), []
    d = settle(p, history, [control("agent", 1, "Available", 1), control("agent", 2, "Accept chat", 2)])
    assert d["choice"] == "t2.e2" and p.step == 2  # already signed in: a later step's control shows


def test_an_optional_step_is_done_when_its_control_shows(fake):
    p, history = policy([step("agent", "type the username", ["Username"], text="{{CCP_USER}}", optional=True),
                         step("agent", "accept the chat", ["Accept chat"])]), []
    d = settle(p, history, [control("agent", 1, "Username", 1, kind="fill"), control("agent", 2, "Next", 2)])
    assert (d["operation"], d["text"]) == ("TYPE_TEXT", "{{CCP_USER}}")  # the reference; the executor types the value


def test_an_optional_step_never_asks_the_text_model(fake, monkeypatch):
    monkeypatch.setattr(laya, "resolve_step", lambda *_a: pytest.fail("optional steps are not guessed"))
    p, history = policy([step("agent", "sign in", ["Sign in"], optional=True),
                         step("agent", "accept the chat", ["Accept chat"])]), []
    for _ in range(laya.OPTIONAL_WAITS + 3):
        executed(history, p.choose(merged([control("agent", 1, "Offline", 1)]), history))
    assert p.step == 1


def test_a_password_goes_only_into_a_password_field(fake):
    p, history = policy([step("agent", "type the password", ["Password"], text="{{CCP_PASSWORD}}")]), []
    actions = [control("agent", 1, "Password hint", 1, kind="fill"),
               control("agent", 2, "Password", 2, kind="fill", secret=True)]
    assert settle(p, history, actions)["choice"] == "t2.e2"


def test_a_later_steps_control_that_appears_on_the_same_tab_skips_ahead(fake):
    p, history = policy([step("customer", "start the chat", ["Start Chat"]),
                         step("agent", "open the status dropdown", ["Status"]),
                         step("agent", "choose Available", ["Available"]),
                         step("agent", "accept the chat", ["Accept chat"])]), []
    assert settle(p, history, [control("customer", 1, "Start Chat", 1), control("agent", 1, "Available", 1)])[
        "choice"] == "t1.e1"
    # Already available: the offer arrives while the status steps wait for controls they will never need.
    d = settle(p, history, [control("agent", 1, "Available", 1), control("agent", 2, "Accept chat", 2)])
    assert d["choice"] == "t2.e2" and p.step == 3


def test_a_send_button_that_typing_reveals_is_the_next_send_step(fake):
    p, history = policy([step("customer", "type confirm", ["Type a message"], text="confirm"),
                         step("customer", "send it", ["Send", "Send message"]),
                         step("customer", "end the chat", ["End chat"])]), []
    box = control("customer", 1, "Type a message", 1, kind="fill")
    end = control("customer", 3, "End chat", 3)
    assert settle(p, history, [box, end])["text"] == "confirm"
    assert settle(p, history, [dict(box, value="confirm"), control("customer", 2, "Send Message", 2), end])[
        "choice"] == "t1.e2" and p.proposed == 1  # pressed as "send it", not as a confirmation of typing
    assert settle(p, history, [box, end], {"customer": "You: confirm"})["choice"] == "t1.e3"


def test_a_step_already_done_by_the_previous_steps_confirmation_is_skipped(fake):
    p, history = policy([step("customer", "type confirm", ["Type a message"], text="confirm"),
                         step("customer", "end the chat", ["End chat"]),
                         step("customer", "send it", ["Send", "Send message"])]), []
    p.confirmed, p.step = {1: "Send Message"}, 2
    settle(p, history, [control("customer", 3, "Minimize", 3)])
    assert p.step == 3  # the Send that confirmed step 2 was this step's control


def test_a_running_clock_does_not_keep_the_page_unsettled(fake):
    p, history = policy([step("agent", "close the contact", ["Close contact"])]), []
    choices = []
    for seconds in range(2):  # the second observation differs only in the clock: the page is settled
        d = p.choose(merged([control("agent", 1, f"Customer 0 minutes {seconds} seconds", 1),
                             control("agent", 2, "Close contact", 2)]), history)
        executed(history, d)
        choices.append(d["choice"])
    assert choices == ["wait", "t2.e2"]


def test_tab_plans_keep_a_valid_tab_and_optional_flags():
    steps = model.parse_steps([{"do": "sign in", "labels": ["Sign in"], "tab": "agent", "optional": True},
                               {"do": "wait", "labels": [], "tab": "nowhere", "see": "Hi", "optional": "yes"}],
                              ["customer", "agent"])
    assert steps == [{"do": "sign in", "labels": ["Sign in"], "text": None, "tab": "agent", "see": None,
                      "optional": True},
                     {"do": "wait", "labels": [], "text": None, "tab": None, "see": "Hi", "optional": False}]


def test_a_tab_plan_splits_the_goal_by_tab_then_plans_each_part(monkeypatch):
    replies = iter([
        {"parts": [{"tab": "customer", "goal": "start the chat"}, {"tab": "agent", "goal": "accept it"},
                   {"tab": "elsewhere", "goal": "ignored"}]},
        {"steps": [{"do": "start the chat", "labels": ["Start Chat"]}]},
        {"steps": [{"do": "accept it", "labels": ["Accept"]}]},
    ])
    seen = []

    def chat(system, context, **_options):
        seen.append(context)
        return next(replies), {"model": "test", "latency_ms": 2}

    monkeypatch.setattr(model, "chat_json", chat)
    plan, meta = model.plan_tabs("Start the chat, then accept it.", {"customer": ["Start Chat"], "agent": []})
    assert [(s["tab"], s["do"]) for s in plan["steps"]] == [("customer", "start the chat"), ("agent", "accept it")]
    assert meta["calls"] == 3 and seen[1]["fields_on_page"] == ["Start Chat"]


def test_tab_arguments_are_parsed_and_checked():
    assert tabs.parse(["customer widget=https://a.test/", "agent=https://b.test/x=1"]) == [
        ("customer widget", "https://a.test/"), ("agent", "https://b.test/x=1")]
    for bad in (["noequals"], ["a=ftp://x"], ["a=https://x", "a=https://y"], []):
        with pytest.raises(ValueError):
            tabs.parse(bad)


def test_merged_nodes_are_unique_and_decode_to_their_tab():
    assert len({control("customer", 1, "Send", 7)["node"], control("agent", 1, "Send", 7)["node"]}) == 2
    assert (-5 * tabs.SLOTS + 1) % tabs.SLOTS == 1  # a frame's negative node keeps its tab


class FakeTab:
    def __init__(self, text):
        self.text, self.acted, self.target = text, [], "T"

    def observe(self, screenshot=True):
        actions = [{"id": "e1", "kind": "click", "label": "Send", "role": "button", "value": "", "node": 3}, WAIT]
        page = {"url": "https://x.test/", "title": "X", "text": self.text, "scroll": {"y": 0}, "actions": actions}
        page["fingerprint"] = fingerprint(page)
        return page

    def fresh(self, page, action=None):
        return True

    def act(self, action, page, text=None):
        self.acted.append(action["id"])


def test_tabs_merge_observations_and_route_each_action_to_its_own_tab():
    t = tabs.Tabs.__new__(tabs.Tabs)
    t.names, t.tabs, t.focus, t.watch = ["customer", "agent"], [FakeTab("one"), FakeTab("two")], 0, False
    page = t.observe(screenshot=False)
    assert [a["id"] for a in page["actions"]] == ["t1.e1", "t2.e1", "wait"]
    assert [a["tab"] for a in page["actions"][:2]] == ["customer", "agent"]
    t.act(page["actions"][1], page)
    assert (t.tabs[0].acted, t.tabs[1].acted, t.focus) == ([], ["e1"], 1)


def test_credentials_are_typed_only_from_the_environment_and_only_where_allowed(monkeypatch):
    monkeypatch.setenv("LAYA_CREDENTIALS", "CCP_USER,CCP_PASSWORD")
    monkeypatch.setenv("CCP_USER", "agent-1")
    monkeypatch.setenv("CCP_PASSWORD", "s3cret")
    monkeypatch.setenv("TEXT_MODEL_API_KEY", "sk-live")
    field, password = {"kind": "fill"}, {"kind": "fill", "secret": True}
    assert credentials.prepare("{{CCP_USER}}", field) == "agent-1"
    assert credentials.prepare("{{CCP_PASSWORD}}", password) == "s3cret"
    assert credentials.prepare("Hello", field) == "Hello"
    for text, action in [("{{CCP_PASSWORD}}", field), ("{{CCP_USER}}", password), ("Hello", password),
                         ("{{TEXT_MODEL_API_KEY}}", field), ("{{MISSING}}", field)]:
        with pytest.raises(ValueError):
            credentials.prepare(text, action)


def test_a_step_planned_without_labels_is_named_by_its_own_words(fake):
    p, history = policy([step("customer", "type a message", [], text="confirm")]), []
    actions = [control("customer", 1, "textbox", 1, kind="fill", hint="snippet code editor"),
               control("customer", 2, "Type a message", 2, kind="fill")]
    assert settle(p, history, actions)["choice"] == "t1.e2"


def test_the_text_model_only_sees_controls_sharing_a_word_with_the_step(fake, monkeypatch):
    asked = []
    monkeypatch.setattr(laya, "resolve_step", lambda goal, s, controls: (asked.append(controls), (None, {}))[1])
    p, history = policy([step("customer", "send the reply", ["Send"])]), []
    for _ in range(laya.TAB_GUESS_AFTER + 3):
        executed(history, p.choose(merged([control("customer", 1, "Start a call", 1),
                                           control("customer", 2, "End chat", 2)]), history))
    assert not asked  # nothing shares a word with "send the reply": no guess at all
    p, history = policy([step("agent", "accept the incoming chat", ["Accept chat"])]), []
    for _ in range(laya.TAB_GUESS_AFTER + 3):
        executed(history, p.choose(merged([control("agent", 1, "Chat tab. You have chats", 1),
                                           control("agent", 2, "Settings", 2)]), history))
    assert [list(c.values()) for c in asked][:1] == [["[agent] button Chat tab. You have chats"]]


def test_a_new_text_field_is_never_a_confirmation_and_a_typing_step_is_named_by_its_words(fake):
    p, history = policy([step("agent", "accept the incoming chat", ["Accept chat"]),
                         step("agent", "type a message", ["Message Input", "Chat Box"],
                              text="Hello from the agent")]), []
    assert settle(p, history, [control("agent", 1, "Accept chat", 1)])["choice"] == "t2.e1"
    box = [control("agent", 2, "Type a message and press enter to send", 2, kind="fill"),
           control("agent", 3, "Open Type a message and press enter to send", 2)]
    d = settle(p, history, box)
    assert (d["operation"], d["text"]) == ("TYPE_TEXT", "Hello from the agent")  # not clicked as "to send"
