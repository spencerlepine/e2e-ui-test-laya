"""Offline contracts for frame-qualified nodes, frame freshness, and frame coordinates. No browser, no models."""

from unittest.mock import Mock

import pytest

import laya_ultrafast.browser as browser
from laya_ultrafast import laya, model
from laya_ultrafast.browser import StalePage, clip_box, place, valid_node
from tests.test_laya import FakeLaya, page


@pytest.mark.parametrize("node", [1, 42, -1, -7])
def test_observed_nodes_are_nonzero_ints(node):
    assert valid_node(node)


@pytest.mark.parametrize("node", [0, True, False, "1", "-1", 1.0, None, "f1:3", [1]])
def test_anything_else_is_not_a_node(node):
    assert not valid_node(node)


def frame_browser(responses=()):
    """A Browser with one cross-origin frame "F" (hosted by the page) holding local node 5 as node -1."""
    b = browser.Browser.__new__(browser.Browser)
    b.session, b.frames, b.nodes, b.ids = "top", {}, {}, {}
    b.frames["F"] = {"host": None, "owner": 3, "session": "frame", "context": None}
    assert b.node_id("F", 100.0, 5) == -1
    return b


def frame_page(guard=("guard",)):
    return {
        "page_key": ["top-key"], "marker": ["top-marker"], "guards": {"-1": list(guard)},
        "frame_states": {"F": {"page_key": ["frame-key"], "marker": ["frame-marker"]}},
    }


def evaluator(values):
    """cdp stand-in: Runtime.evaluate answers from `values` by session; records every call."""
    calls = []

    def cdp(method, session_id=None, **params):
        calls.append((method, session_id, params))
        if method != "Runtime.evaluate":
            return {}
        value = values[session_id]
        value = value(params["expression"]) if callable(value) else value
        return {"result": {"value": value}}

    return cdp, calls


def test_frame_node_ids_are_stable_per_document_and_new_per_reload():
    b = frame_browser()
    assert b.node_id("F", 100.0, 5) == -1  # same frame, same document, same element
    assert b.node_id("F", 100.0, 6) == -2
    assert b.node_id("F", 200.0, 5) == -3  # the frame reloaded: element 5 of the new document is another node
    assert b.nodes[-3] == ("F", 200.0, 5)


def test_frame_click_is_fresh_only_when_page_frame_and_node_are_unchanged(monkeypatch):
    b = frame_browser()
    cdp, _calls = evaluator({"top": ["top-key"], "frame": [["frame-key"], ["guard"]]})
    monkeypatch.setattr(browser, "cdp", cdp)
    action = {"kind": "click", "node": -1}
    assert b.fresh(frame_page(), action)
    assert not b.fresh(frame_page(guard=("other",)), action)
    changed = frame_page()
    changed["frame_states"]["F"]["page_key"] = ["navigated"]
    assert not b.fresh(changed, action)
    changed = frame_page()
    changed["page_key"] = ["top-changed"]
    assert not b.fresh(changed, action)


def test_detached_frame_or_unknown_node_is_never_fresh(monkeypatch):
    b = frame_browser()
    cdp, _calls = evaluator({"top": ["top-key"], "frame": [["frame-key"], ["guard"]]})
    monkeypatch.setattr(browser, "cdp", cdp)
    assert not b.fresh(frame_page(), {"kind": "click", "node": -9})
    del b.frames["F"]
    assert not b.fresh(frame_page(), {"kind": "click", "node": -1})
    assert not b.fresh(frame_page())  # the terminal check covers every frame that was observed


def test_terminal_freshness_checks_each_frame_marker(monkeypatch):
    b = frame_browser()
    cdp, _calls = evaluator({"top": ["top-marker"], "frame": ["frame-marker"]})
    monkeypatch.setattr(browser, "cdp", cdp)
    assert b.fresh(frame_page())
    cdp, _calls = evaluator({"top": ["top-marker"], "frame": ["frame-navigated"]})
    monkeypatch.setattr(browser, "cdp", cdp)
    assert not b.fresh(frame_page())


def test_pages_without_frames_keep_one_evaluation_per_freshness_check(monkeypatch):
    b = frame_browser()
    cdp, calls = evaluator({"top": ["top-marker"]})
    monkeypatch.setattr(browser, "cdp", cdp)
    assert b.fresh({"page_key": [], "marker": ["top-marker"], "guards": {}})
    assert len(calls) == 1


def test_covered_frame_target_gets_no_input(monkeypatch):
    b = frame_browser()

    def frame(expression):
        return [["frame-key"], ["guard"]] if "pageKey" in expression else {"x": 10, "y": 10}

    # The frame's own hit test passes; the page's hit test on the <iframe> fails.
    cdp, calls = evaluator({"top": lambda e: ["top-key"] if "pageKey" in e else None, "frame": frame})
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(StalePage, match="covered"):
        b.act({"id": "e1", "kind": "click", "node": -1}, frame_page())
    assert not any(method.startswith("Input.") for method, _s, _p in calls)


def test_frame_click_is_sent_to_the_page_at_the_lifted_point(monkeypatch):
    b = frame_browser()

    def top(expression):
        return ["top-key"] if "pageKey" in expression else {"x": 110, "y": 60}

    def frame(expression):
        return [["frame-key"], ["guard"]] if "pageKey" in expression else {"x": 10, "y": 10}

    cdp, calls = evaluator({"top": top, "frame": frame})
    monkeypatch.setattr(browser, "cdp", cdp)
    b.act({"id": "e1", "kind": "click", "node": -1}, frame_page())
    hop = next(p["expression"] for m, s, p in calls if m == "Runtime.evaluate" and "hop" in p["expression"])
    assert "nodes.get(3),10,10" in hop  # the frame's <iframe> in the page, with the frame-local point
    clicks = [(s, p["x"], p["y"]) for m, s, p in calls if m == "Input.dispatchMouseEvent"]
    assert clicks == [("top", 110, 60), ("top", 110, 60)]


def test_frame_dropdown_changes_only_after_every_ancestor_check(monkeypatch):
    b = frame_browser()
    order = []

    def frame(expression):
        if "pageKey" in expression:
            return [["frame-key"], ["guard"]]
        order.append("commit" if '"commit": false' not in expression else "check")
        return {"x": 10, "y": 10}

    def top(expression):
        if "hop" in expression:
            order.append("hop")
            return None
        return ["top-key"]

    cdp, _calls = evaluator({"top": top, "frame": frame})
    monkeypatch.setattr(browser, "cdp", cdp)
    with pytest.raises(StalePage):
        b.act({"id": "e1", "kind": "select", "node": -1, "value": "Two"}, frame_page())
    assert order == ["check", "hop"]  # covered: the frame's dropdown was never changed


def test_frame_actions_move_to_page_coordinates_and_drop_clipped_ones():
    sub = {
        "actions": [
            {"id": "e1", "kind": "click", "node": 5, "label": "Send", "rect": {"x": 10, "y": 10, "w": 20, "h": 10}},
            {"id": "e2", "kind": "click", "node": 6, "label": "Far", "rect": {"x": 10, "y": 500, "w": 20, "h": 10}},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "guards": {"5": ["g5"], "6": ["g6"]},
    }
    box = {"x": 100, "y": 50, "w": 300, "h": 200}
    clip = clip_box([100, 50, 400, 250], (0, 0), [0, 0, 1120, 780])
    kept, guards = place(sub, box, clip, lambda n: -n)
    assert [a["label"] for a in kept] == ["Send"]
    assert kept[0]["rect"] == {"x": 110, "y": 60, "w": 20, "h": 10}
    assert kept[0]["node"] == -5 and guards == {"-5": ["g5"]}


def test_nested_clip_stays_inside_the_parent_frame():
    # A frame at (10, 20) in its parent, whose own visible area ends at y=100 in the page.
    assert clip_box([0, 0, 500, 500], (10, 20), [0, 0, 300, 100]) == [10, 20, 300, 100]


def test_frame_snapshots_merge_into_one_numbered_page(monkeypatch):
    b = frame_browser()
    info = {
        "url": "https://example.test/", "text": "Page", "scroll": {"y": 0}, "omitted_actions": 0,
        "guards": {"1": ["g1"]}, "page_key": ["top-key"],
        "actions": [
            {"id": "e1", "kind": "click", "node": 1, "label": "Top", "rect": {"x": 0, "y": 0, "w": 10, "h": 10}},
            {"id": "wait", "kind": "wait", "label": "Wait"},
        ],
        "frames": [{"node": 3, "rect": {"x": 100, "y": 50, "w": 300, "h": 200}, "clip": [100, 50, 400, 250]}],
    }
    sub = {
        "text": "Chat", "frames": [], "page_key": [100.0], "marker": ["frame-marker"], "guards": {"5": ["g5"]},
        "actions": [{"id": "e1", "kind": "fill", "node": 5, "label": "Message",
                     "rect": {"x": 10, "y": 10, "w": 20, "h": 10}}],
    }
    monkeypatch.setattr(b, "locate", Mock(return_value={3: "F"}))
    monkeypatch.setattr(b, "frame_evaluate", Mock(return_value=sub))
    b.read_frames(info)
    assert [(a["id"], a["label"], a.get("node")) for a in info["actions"]] == [
        ("e1", "Top", 1), ("e2", "Message", -1), ("wait", "Wait", None)]
    assert info["text"] == "Page\nChat"
    assert info["guards"]["-1"] == ["g5"]
    assert info["frame_states"] == {"F": {"page_key": [100.0], "marker": ["frame-marker"]}}
    assert info["fingerprint"] == browser.fingerprint(info)


def test_frame_nodes_index_and_round_trip_through_the_policies(monkeypatch):
    actions = [
        {"id": "e1", "kind": "click", "label": "Launch chat", "role": "button", "value": "", "node": 4},
        {"id": "e2", "kind": "fill", "label": "Message", "role": "textbox", "value": "", "node": -1},
        {"id": "e3", "kind": "click", "label": "Open Message", "role": "textbox", "value": "", "node": -1},
        {"id": "e4", "kind": "click", "label": "End chat", "role": "button", "value": "", "node": -2},
    ]
    elements, targets, _controls = model.action_space(actions)
    assert [e["index"] for e in elements] == ["1", "2", "3"]
    assert targets["TYPE_TEXT"]["2"]["node"] == -1
    assert not any("node" in e for e in elements)  # the model sees indices, never node identities

    fake = FakeLaya()
    monkeypatch.setattr(laya, "laya", lambda: fake)
    p = laya.LayaPolicy("Send hello in the chat.")
    p.plan, p.plan_meta = {"requirements": [{"what": "Message", "value": "hello"}], "open": None,
                           "finish": "Sent."}, {}
    d = p.choose(page(actions + [{"id": "wait", "kind": "wait", "label": "Wait"}]), [])
    assert (d["operation"], d["choice"], d["text"]) == ("TYPE_TEXT", "e2", "hello")
    assert p.fields[0] == -1 and str(p.fields[0]) == "-1" and int(str(p.fields[0])) == -1
