"""Offline contracts for the local Laya policy. A fake stands in for the model; nothing is downloaded."""

import datetime
import time
from unittest.mock import Mock

import pytest

from laya_ultrafast import agent as loop
from laya_ultrafast import laya, model
from laya_ultrafast.browser import fingerprint


class FakeLaya:
    """Answers each choice question with `prefer(question_id, criteria)`, or the first option."""

    def __init__(self, prefer=None):
        self.prefer = prefer or (lambda _qid, criteria: next(iter(criteria)))
        self.calls = []

    def system_one(self, state, questions):
        self.calls.append((state, questions))
        answers = {}
        for qid, q in questions.items():
            labels = list(q["criteria"])
            choice = self.prefer(qid, q["criteria"])
            rest = (1 - 0.7) / max(1, len(labels) - 1)
            probabilities = {label: 0.7 if label == choice else rest for label in labels}
            if len(labels) == 1:
                probabilities = {choice: 1.0}
            answers[qid] = {"choice": choice, "probabilities": probabilities, "confidence": 0.5}
        return {"answers": answers, "usage": {"input_tokens": 10}}


def page(actions, url="https://example.test/", title="Search", text="Search"):
    state = {"url": url, "title": title, "text": text, "scroll": {"y": 0}, "actions": actions}
    state["fingerprint"] = fingerprint(state)
    return state


FORM = [
    {"id": "e1", "kind": "fill", "label": "Destination", "role": "searchbox", "value": "", "node": 1},
    {"id": "e2", "kind": "click", "label": "Open Destination", "role": "searchbox", "value": "", "node": 1},
    {"id": "e3", "kind": "click", "label": "Find stays", "role": "button", "value": "", "node": 2},
    {"id": "e4", "kind": "click", "label": "View Casa Flora", "role": "button", "value": "", "node": 3},
    {"id": "wait", "kind": "wait", "label": "Wait for the page to update"},
]
PLAN = {"requirements": [{"what": "destination", "value": "Lisbon"}], "open": "Casa Flora", "finish": "It is open."}


@pytest.fixture
def fake(monkeypatch):
    f = FakeLaya()
    monkeypatch.setattr(laya, "laya", lambda: f)
    return f


def policy(plan=PLAN):
    p = laya.LayaPolicy("Find a stay in Lisbon and open Casa Flora.")
    p.plan, p.plan_meta = plan, {"model": "test", "latency_ms": 1}
    return p


def executed(history, decision, label=""):
    history.append({"choice": decision["choice"], "action": label})


def test_a_requirement_named_by_its_field_label_needs_no_mapping_call(fake):
    p = policy({"requirements": [{"what": "Destination", "value": "Lisbon"}], "open": None, "finish": "Seen."})
    d = p.choose(page(FORM), [])
    assert d["choice"] == "e1" and not any(k.startswith("field_") for _s, q in fake.calls for k in q)


def test_goal_values_are_typed_without_a_per_field_text_call(fake):
    p = policy()
    d = p.choose(page(FORM), [])
    assert (d["operation"], d["choice"], d["text"]) == ("TYPE_TEXT", "e1", "Lisbon")
    assert d["target"] == "1"


def test_typed_text_is_submitted_before_opening_a_result(fake):
    p, history = policy(), []
    executed(history, p.choose(page(FORM), history))
    filled = [dict(a, value="Lisbon") if a.get("node") == 1 else a for a in FORM]
    d = p.choose(page(filled), history)
    assert (d["operation"], d["choice"]) == ("CLICK", "e3")  # Find stays, not View Casa Flora


def test_item_page_title_finishes_the_goal(fake):
    p = policy({"requirements": [], "open": "Casa Flora", "finish": "Casa Flora is open."})
    d = p.choose(page(FORM, title="Casa Flora · Forma"), [])
    assert d["operation"] == "DONE" and d["choice"] == "DONE"


def test_a_search_results_title_is_not_the_item_page():
    assert laya.titled("Casa Flora · Forma", "Casa Flora")
    assert not laya.titled("Casa Flora - Search results - Forma", "Casa Flora")


def test_every_target_is_an_observed_action(fake):
    fake.prefer = lambda qid, criteria: list(criteria)[-1]
    p = policy({"requirements": [], "open": None, "finish": "Done."})
    d = p.choose(page(FORM), [])
    assert d["choice"] in {a["id"] for a in FORM} | {"DONE", "BLOCKED"}


def test_invalid_model_answer_executes_nothing(monkeypatch):
    broken = Mock(system_one=Mock(return_value={
        "answers": {"field_0": {"choice": "999", "probabilities": {"999": 1.0}, "confidence": 1.0}},
        "usage": {"input_tokens": 1},
    }))
    monkeypatch.setattr(laya, "laya", lambda: broken)
    form = [dict(a) for a in FORM] + [
        {"id": "e5", "kind": "fill", "label": "Guests", "role": "textbox", "value": "", "node": 4},
    ]
    plan = {"requirements": [{"what": "city", "value": "Lisbon"}], "open": None, "finish": "Seen."}
    with pytest.raises(ValueError, match="Invalid Laya"):
        policy(plan).choose(page(form), [])


def test_a_target_that_never_executes_is_dropped(fake):
    p = policy({"requirements": [], "open": "Casa Flora", "finish": "Casa Flora is open."})
    first = p.choose(page(FORM), [])
    assert first["choice"] == "e4"
    p.choose(page(FORM), [])  # The covered click raised StalePage; history did not grow.
    third = p.choose(page(FORM), [])
    assert third["choice"] != "e4"


@pytest.mark.parametrize(
    ("value", "current", "expected"),
    [
        ("October 20, 2026", "Tue, Oct 20", True),
        ("October 20, 2026", "Wed, Oct 21", False),
        ("Zurich", "Zürich", True),
        ("London", "", False),
        ("one-way", "Round trip", None),
    ],
)
def test_plain_code_settles_what_it_can(value, current, expected):
    element = {"role": "combobox", "current": current, "options": []}
    assert laya.settled({"what": "x", "value": value}, element) is expected


def test_checkbox_requirements_follow_the_checked_state():
    checked = {"role": "checkbox", "current": "checked", "options": []}
    assert laya.settled({"what": "free cancellation", "value": "checked"}, checked)
    assert not laya.settled({"what": "free cancellation", "value": "off"}, checked)


def test_plan_requires_a_finish_condition():
    plan, _ = model.parse_plan({"requirements": [{"what": "to", "value": "London"}], "finish": "Seen."}, {})
    assert plan == {"requirements": [{"what": "to", "value": "London"}], "open": None, "finish": "Seen.", "steps": []}
    with pytest.raises(ValueError, match="no valid plan"):
        model.parse_plan({"requirements": []}, {})


def test_local_text_model_needs_no_key_but_remote_does(monkeypatch):
    monkeypatch.delenv("TEXT_MODEL_API_KEY", raising=False)
    post = Mock(return_value={"choices": [{"message": {"content": '{"text":"Zurich"}'}}]})
    monkeypatch.setattr(model, "post_json", post)
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "http://localhost:11434/v1")
    assert model.field_text({"goal": "Fly from Zurich"})[0] == "Zurich"
    monkeypatch.setenv("TEXT_MODEL_BASE_URL", "https://openrouter.ai/api/v1")
    with pytest.raises(ValueError, match="TEXT_MODEL_API_KEY"):
        model.field_text({"goal": "Fly from Zurich"})


def test_agent_types_the_planned_value_without_the_text_helper(monkeypatch):
    helper = Mock()
    monkeypatch.setattr(loop, "field_text", helper)
    a = loop.Agent.__new__(loop.Agent)
    a.screenshots, a.pending_text = False, None
    p = page(FORM)
    a.state = {
        "browser": Mock(fresh=Mock(return_value=True), observe=Mock(return_value=p)),
        "page": p, "goal": "g", "history": [], "decisions": [], "status": "predicted",
        "started_at": time.perf_counter(), "record": False, "text_calls": [],
        "decision": {"choice": "e1", "operation": "TYPE_TEXT", "target": "1", "text": "Lisbon", "confidence": 1.0,
                     "probabilities": {"e1": 1.0}, "latency_ms": 1, "usage": {}},
    }
    a.command("act", {"fingerprint": p["fingerprint"]})
    helper.assert_not_called()
    a.state["browser"].act.assert_called_once()
    assert a.state["browser"].act.call_args.kwargs["text"] == "Lisbon"


def test_flight_goal_uses_the_requested_date():
    from examples.flights import goal

    assert "October 20, 2026" in goal(datetime.date(2026, 10, 20))


def test_skyscanner_verification_reads_the_search_url():
    from examples.skyscanner import verify

    day = datetime.date(2026, 10, 20)
    good = {
        "url": "https://www.skyscanner.net/transport/flights/zrh/lond/261020/?adultsv2=1&cabinclass=economy&rtn=0",
        "text": "Best CHF 120 Cheapest CHF 98 Fastest",
    }
    assert verify(good, day)["passed"]
    assert not verify(dict(good, url=good["url"].replace("261020", "261021")), day)["passed"]
    assert not verify(dict(good, url="https://www.skyscanner.net/sttc/px/captcha-v2/index.html"), day)["passed"]


# Step-by-step goals ------------------------------------------------------------------------------------------

STEPS = {"requirements": [], "open": None, "finish": "Closed.", "steps": [
    {"do": "launch the chat widget", "labels": ["Chat widget"], "text": None},
    {"do": "escalate to voice", "labels": ["Start a call", "Call"], "text": None},
    {"do": "end the voice call", "labels": ["End call"], "text": None},
]}
WAIT = {"id": "wait", "kind": "wait", "label": "Wait for the page to update"}


def button(i, label, node):
    return {"id": f"e{i}", "kind": "click", "label": label, "role": "button", "value": "", "node": node}


def run_step(p, history, actions, text="Search"):
    """One observation and decision; the decision is executed."""
    d = p.choose(page(actions + [WAIT], text=text), history)
    executed(history, d)
    return d


def settle(p, history, actions, limit=4, text="Search"):
    """Observe the same page until the policy acts: steps act only on a settled page."""
    for _ in range(limit):
        d = run_step(p, history, actions, text)
        if d["choice"] != "wait":
            return d
    return d


def test_steps_act_only_on_a_settled_page(fake):
    p, history = policy(STEPS), []
    p.step = 1
    actions = [button(1, "Minimize Chat", 1), button(2, "Start a call", 2)]
    assert run_step(p, history, actions)["choice"] == "wait"  # first sight of this page
    assert run_step(p, history, actions)["choice"] == "e2"


def test_a_step_clicks_the_control_its_label_names_then_moves_on(fake):
    p, history = policy(STEPS), []
    p.step = 1
    d = settle(p, history, [button(1, "Minimize Chat", 1), button(2, "Start a call", 2), button(3, "End chat", 3)])
    assert (d["operation"], d["choice"]) == ("CLICK", "e2")
    d = settle(p, history, [button(1, "Minimize Chat", 1), button(4, "Mute", 4), button(5, "End call", 5)])
    assert d["choice"] == "e5" and p.step == 2


def test_a_step_no_label_names_asks_the_text_model_then_skips(fake, monkeypatch):
    monkeypatch.setattr(laya, "MAX_STEP_WAITS", 6)
    asked = []

    def resolve(goal, step, controls):
        asked.append(dict(controls))
        index = next((i for i, text in controls.items() if "Start Chat" in text), None)
        return index, {"model": "test", "latency_ms": 1}

    monkeypatch.setattr(laya, "resolve_step", resolve)
    p, history = policy(STEPS), []
    actions = [button(1, "Copy Link", 1), button(2, "Start Chat", 2)]
    choices = [run_step(p, history, actions)["choice"] for _ in range(laya.GUESS_AFTER)]
    assert choices == ["wait"] * laya.GUESS_AFTER and not asked  # labels first; the page may still be rendering
    assert run_step(p, history, actions)["choice"] == "e2"  # the text model picked an observed control
    assert list(asked[0].values()) == ["button Start Chat"]  # indices and labels, sharing a word with the step
    assert p.calls and p.calls[0]["field"] == "step: launch the chat widget"

    p2, history2 = policy(STEPS), []
    for _ in range(12):
        d = run_step(p2, history2, [button(1, "Copy Link", 1)])
    assert p2.step >= 1 and d["choice"] == "wait"  # "none" twice over: the launch step was skipped
    assert sum(k[0] == 0 for k in p2.resolved) == 1  # one call per step and set of visible controls


def test_a_failing_text_model_only_means_waiting(fake, monkeypatch):
    def fail(*_args):
        raise RuntimeError("Model connection failed; no action executed.")

    monkeypatch.setattr(laya, "resolve_step", fail)
    p, history = policy(STEPS), []
    for _ in range(laya.GUESS_AFTER + 3):
        assert run_step(p, history, [button(2, "Start Chat", 2)])["choice"] == "wait"


def test_step_labels_copied_from_page_fields_are_dropped(fake, monkeypatch):
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["textbox (snippet code editor)", "Message"], "text": "Hello"}]}
    monkeypatch.setattr(laya, "plan_goal", lambda goal, labels: (plan, {"model": "test", "latency_ms": 1}))
    p = laya.LayaPolicy("Open the chat and send Hello")
    editor = {"id": "e1", "kind": "fill", "label": "textbox", "hint": "snippet code editor", "role": "textbox",
              "value": "", "node": 1}
    p.choose(page([editor, WAIT]), [])
    assert p.plan["steps"][0]["labels"] == ["Message"]


def test_a_confirmation_the_step_opened_is_accepted(fake):
    p, history = policy(STEPS), []
    p.step = 1
    settle(p, history, [button(1, "Minimize Chat", 1), button(2, "Start a call", 2)])
    d = settle(p, history, [button(1, "Minimize Chat", 1), button(6, "Cancel", 6), button(7, "Yes, Start a call", 7)])
    assert d["choice"] == "e7" and p.step == 2  # "Yes" completes the escalation; it is not the next step
    assert settle(p, history, [button(1, "Minimize Chat", 1), button(5, "End call", 5)])["choice"] == "e5"


def test_a_control_relabelled_in_place_counts_as_new(fake):
    p, history = policy(STEPS), []
    p.step = 1
    settle(p, history, [button(1, "Minimize Chat", 1), button(2, "Start a call", 2)])
    # The site turns the same element into the confirmation button.
    d = settle(p, history, [button(1, "Minimize Chat", 1), button(6, "Cancel", 6), button(2, "Yes, Start a call", 2)])
    assert d["choice"] == "e2" and p.step == 2


def test_typed_step_fills_then_sends(fake):
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["Type a message"], "text": "Hello, World!"}]}
    p, history = policy(plan), []
    box = {"id": "e1", "kind": "fill", "label": "Type a message", "role": "textbox", "value": "", "node": 9}
    d = settle(p, history, [box])
    assert (d["operation"], d["text"]) == ("TYPE_TEXT", "Hello, World!")
    d = settle(p, history, [dict(box, value="Hello, World!"), button(2, "Send Message", 10)])
    assert d["choice"] == "e2"  # the Send button that typing revealed
    assert settle(p, history, [dict(box, value="")], text="You: Hello, World!")["operation"] == "DONE"


def test_a_control_used_by_one_step_is_never_reused(fake):
    plan = {"requirements": [], "open": None, "finish": "Done.", "steps": [
        {"do": "start the chat", "labels": ["Start Chat"], "text": None},
        {"do": "start a conversation", "labels": ["Start chat"], "text": None}]}
    p, history = policy(plan), []
    assert settle(p, history, [button(1, "Start Chat", 1)])["choice"] == "e1"
    assert settle(p, history, [button(1, "Start Chat", 1), button(2, "Help", 2)])["choice"] == "wait"


def test_plan_steps_are_validated():
    plan, _ = model.parse_plan({"requirements": [], "finish": "Closed.", "steps": [
        {"do": "end the call", "labels": ["End call", 3, ""], "text": ""}, {"labels": ["no do"]}]}, {})
    assert plan["steps"] == [{"do": "end the call", "labels": ["End call"], "text": None}]


def test_a_step_without_its_own_control_gives_way_to_the_next_one(fake):
    plan = {"requirements": [], "open": None, "finish": "Done.", "steps": [
        {"do": "start a conversation", "labels": ["Chat widget"], "text": None},
        {"do": "escalate to voice", "labels": ["Call"], "text": None},
        {"do": "close the widget", "labels": ["Minimize"], "text": None}]}
    p, history = policy(plan), []
    p.base = {(9, "Open chat")}  # the last action (opening the chat) happened before any of these appeared
    widget = [button(1, "Minimize Chat", 1), button(2, "End chat", 2)]
    for _ in range(laya.GUESS_AFTER + 2):  # no guess: nothing shares a word with the step itself
        assert run_step(p, history, widget)["choice"] == "wait"
    d = settle(p, history, widget + [button(3, "Start a call", 3)])
    assert d["choice"] == "e3" and p.step == 1  # the next step's control appeared: "start" skipped


def test_a_control_that_was_already_there_does_not_skip_a_loading_step(fake):
    plan = {"requirements": [], "open": None, "finish": "Done.", "steps": [
        {"do": "end the voice call", "labels": ["End call"], "text": None},
        {"do": "close the widget", "labels": ["Minimize"], "text": None}]}
    p, history = policy(plan), []
    p.base = {(1, "Minimize Chat")}  # Minimize was on screen before the call started
    for _ in range(4):
        assert run_step(p, history, [button(1, "Minimize Chat", 1)])["choice"] == "wait"
    assert settle(p, history, [button(1, "Minimize Chat", 1), button(5, "End call", 5)])["choice"] == "e5"


def test_the_next_step_waits_for_the_page_to_react_then_tries_again(fake):
    plan = {"requirements": [], "open": None, "finish": "Done.", "steps": [
        {"do": "end the voice call", "labels": ["End call"], "text": None},
        {"do": "close the widget", "labels": ["Minimize"], "text": None}]}
    p, history = policy(plan), []
    call = [button(1, "Minimize Chat", 1), button(5, "End call", 5)]
    assert settle(p, history, call)["choice"] == "e5"
    waits = [run_step(p, history, call)["choice"] for _ in range(laya.SETTLE_WAITS)]
    assert waits == ["wait"] * laya.SETTLE_WAITS  # the call screen has not reacted yet
    assert run_step(p, history, call)["choice"] == "e5"  # nothing happened: "End call" is decided again
    assert settle(p, history, [button(1, "Minimize Chat", 1)])["choice"] == "e1"  # it reacted: next step
    assert p.tries[0] == 2


def test_a_looser_label_does_not_make_another_control_a_confirmation(fake):
    plan = {"requirements": [], "open": None, "finish": "Done.", "steps": [
        {"do": "start a conversation", "labels": ["Start chat", "Chat"], "text": None},
        {"do": "end the chat", "labels": ["End chat"], "text": None}]}
    p, history = policy(plan), []
    assert settle(p, history, [button(1, "Start Chat", 1)])["choice"] == "e1"
    d = settle(p, history, [button(1, "Start Chat", 1), button(2, "Minimize Chat", 2), button(3, "End chat", 3)])
    assert d["choice"] == "e3"  # not Minimize Chat as a "confirmation" of starting the chat
    d = settle(p, history, [button(4, "Cancel", 4), button(5, "End chat", 5)])
    assert d["choice"] == "e5"  # the dialog's own "End chat" confirms it


def test_typed_text_lost_to_a_reset_is_typed_again(fake):
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["Type a message"], "text": "Hello, World!"},
        {"do": "end the chat", "labels": ["End chat"], "text": None}]}
    p, history = policy(plan), []
    box = {"id": "e1", "kind": "fill", "label": "Type a message", "role": "textbox", "value": "", "node": 9}
    assert settle(p, history, [box])["text"] == "Hello, World!"
    # The chat connected and reset the message box: the text is gone and was never sent.
    d = settle(p, history, [box, button(2, "End chat", 2)], limit=laya.SETTLE_WAITS + 2)
    assert (d["choice"], d["text"]) == ("e1", "Hello, World!") and p.step == 0  # End chat waited


def test_a_message_sent_too_early_is_typed_and_sent_again(fake):
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["Type a message"], "text": "Hello, World!"},
        {"do": "end the chat", "labels": ["End chat"], "text": None}]}
    p, history = policy(plan), []
    box = {"id": "e1", "kind": "fill", "label": "Type a message", "role": "textbox", "value": "", "node": 9}
    typed = dict(box, value="Hello, World!")
    settle(p, history, [box])
    assert settle(p, history, [typed, button(2, "Send Message", 10)])["choice"] == "e2"
    # The box emptied, but no transcript shows the message: it was dropped.
    for _ in range(laya.SETTLE_WAITS + 2):
        d = run_step(p, history, [box, button(3, "End chat", 3)])
        if d["choice"] != "wait":
            break
    assert (d["choice"], d["text"]) == ("e1", "Hello, World!")
    assert settle(p, history, [typed, button(2, "Send Message", 10)])["choice"] == "e2"  # the same Send again
    d = settle(p, history, [box, button(3, "End chat", 3)], text="You: Hello, World!")
    assert d["choice"] == "e3"  # delivered: on to the next step


def test_a_typing_step_is_never_skipped_for_the_next_steps_control(fake, monkeypatch):
    monkeypatch.setattr(laya, "resolve_step", lambda *_a: (None, {"model": "test", "latency_ms": 1}))
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["Message input"], "text": "Hello, World!"},
        {"do": "end the chat", "labels": ["End chat"], "text": None}]}
    p, history = policy(plan), []
    p.base = {(9, "Open chat")}
    for _ in range(laya.GUESS_AFTER + 3):
        d = run_step(p, history, [button(3, "End chat", 3)])
    assert d["choice"] == "wait" and p.step == 0  # End chat appeared, but the message was never typed


def test_a_resolved_control_survives_a_page_change_during_the_model_call(fake, monkeypatch):
    calls = []

    def resolve(goal, step, controls):
        calls.append(controls)
        return next(i for i, text in controls.items() if "Type a message" in text), {"model": "test", "latency_ms": 1}

    monkeypatch.setattr(laya, "resolve_step", resolve)
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send hello", "labels": ["Message input"], "text": "Hello, World!"}]}
    p, history = policy(plan), []
    box = {"id": "e2", "kind": "fill", "label": "Type a message", "role": "textbox", "value": "", "node": 9}
    for _ in range(laya.GUESS_AFTER + 1):
        d = p.choose(page([button(1, "Minimize Chat", 1), box, WAIT]), history)
        if d["choice"] != "wait":
            break  # decided, but the page changes before it runs: not executed
        executed(history, d)
    assert d["choice"] == "e2"
    # New controls shift the box to e3; the remembered control is the same element.
    moved = [button(1, "Minimize Chat", 1), button(2, "End chat", 2), dict(box, id="e3")]
    d = settle(p, history, moved)
    assert (d["choice"], d["text"]) == ("e3", "Hello, World!") and len(calls) == 1


def test_an_emoji_the_text_model_picks_is_clicked_then_sent(fake, monkeypatch):
    def resolve(goal, step, controls):
        return next(i for i, text in controls.items() if "smiley" in text), {"model": "test", "latency_ms": 1}

    monkeypatch.setattr(laya, "resolve_step", resolve)
    plan = {"requirements": [], "open": None, "finish": "Sent.", "steps": [
        {"do": "send the smiley face emoji", "labels": ["Send emoji"], "text": None}]}
    p, history = policy(plan), []
    picker = [button(1, "😀, grinning", 1), button(2, "😃, smiley", 2), button(3, "😄, smile", 3)]
    for _ in range(laya.GUESS_AFTER + 2):
        d = run_step(p, history, picker)
        if d["choice"] != "wait":
            break
    assert d["choice"] == "e2"  # no label says "smiley face emoji"; the text model picked the observed 😃
    d = settle(p, history, picker + [button(4, "Send Message", 4)])
    assert d["choice"] == "e4"  # choosing the emoji revealed Send: it completes the step
