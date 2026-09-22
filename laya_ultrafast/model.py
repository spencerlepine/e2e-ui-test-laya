"""TypeSafe decisions (optional cloud backend) and the OpenAI-compatible text model helpers."""

import json
import math
import os
import time
from urllib.parse import urlparse

import httpx

from .questions import GOAL_PLAN, NEXT_ACTION, STEP_PLAN, STEP_TARGET, TAB_SPLIT, TAB_STEPS, TARGET, TEXT_VALUE

CLIENT = httpx.Client(http2=True, timeout=25)


def post_json(url, key, body, timeout=None):
    for attempt in range(3):
        try:
            response = CLIENT.post(url, json=body, headers={"Authorization": f"Bearer {key}"},
                                   **({"timeout": timeout} if timeout else {}))
        except httpx.HTTPError:
            raise RuntimeError("Model connection failed; no action executed.") from None
        if response.status_code in {429, 529, 503} and attempt < 2:
            time.sleep(0.5 * 2**attempt)
            continue
        if response.is_error:
            raise RuntimeError(f"Model provider returned HTTP {response.status_code}; no action executed.")
        return response.json()
    raise RuntimeError("Model unavailable")


def validate_choice(answer, ids):
    try:
        probabilities = answer["probabilities"]
        numbers = [*probabilities.values(), answer["confidence"]]
        valid = (
            answer["choice"] in ids
            and set(probabilities) == set(ids)
            and all(type(n) in (int, float) and math.isfinite(n) and 0 <= n <= 1 for n in numbers)
            and abs(sum(probabilities.values()) - 1) < 0.02
            and probabilities[answer["choice"]] >= max(probabilities.values()) - 1e-6
        )
    except (KeyError, TypeError, ValueError):
        valid = False
    if not valid:
        raise ValueError("Invalid TypeSafe response; no action executed.")
    return answer


def action_space(actions):
    """One index per observed element; each operation has its own valid target choices."""
    elements, indices, targets, controls = [], {}, {}, {}
    operations = {"click": "CLICK", "fill": "TYPE_TEXT", "select": "SELECT"}
    for action in actions:
        kind = action["kind"]
        if kind not in operations:
            controls[action["id"].upper()] = action
            continue
        node = action["node"]
        if node not in indices:
            index = str(len(elements) + 1)
            indices[node] = index
            element = {k: action[k] for k in ("role", "value", "checked", "selected", "expanded") if k in action}
            element.update(index=index, label=action["label"].split(" → ")[0], operations=[])
            if kind == "select":
                element["value"] = action.get("current_value", "")
                element["options"] = []
            elements.append(element)
        index = indices[node]
        operation = operations[kind]
        group = targets.setdefault(operation, {})
        element = elements[int(index) - 1]
        if operation not in element["operations"]:
            element["operations"].append(operation)
        target = index
        if kind == "select":
            target = f"{index}:{len(element['options']) + 1}"
            element["options"].append({"index": target, "label": action["label"], "value": action["value"]})
        group[target] = action
    return elements, targets, controls


def choose(state, goal, history):
    elements, targets, controls = action_space(state["actions"])
    labels = {
        "CLICK": "Click an element, button, menu option, autocomplete suggestion, or calendar day.",
        "TYPE_TEXT": "Enter or replace text in an editable field. A small LLM will supply the value from the goal.",
        "SELECT": "Select an observed dropdown value.",
    }
    operations = {key: labels[key] for key in targets}
    operations.update({key: value["label"] for key, value in controls.items()})
    operations.update(DONE="Every requirement is visibly satisfied.", BLOCKED="No supported operation can progress.")
    questions = {
        "operation": {"type": "choice", "criteria": operations, "instructions": {"goal": goal, "rules": NEXT_ACTION}}
    }
    for operation, candidates in targets.items():
        questions[operation.lower() + "_target"] = {
            "type": "choice",
            "criteria": {
                index: {
                    "element": f"[{index}] {a['label']}",
                    "current_value": a.get("current_value", a.get("value", "")),
                    **{k: a[k] for k in ("role", "checked", "selected", "expanded") if k in a},
                }
                for index, a in candidates.items()
            },
            "instructions": {"goal": goal, "operation": operation, "rules": [NEXT_ACTION, TARGET]},
        }
    body = {
        "model": os.environ.get("TYPESAFE_MODEL", "jev-latest"),
        "state": {
            "page": {k: state[k] for k in ("url", "title", "text")},
            "elements": elements,
            "recent_actions": [
                {k: h.get(k) for k in ("action", "kind", "text", "page_changed")} for h in history[-10:]
            ],
        },
        "questions": questions,
    }
    started = time.perf_counter()
    result = post_json("https://api.typesafe.ai/v1/systemone", os.environ["TYPESAFE_API_KEY"], body)
    operation_answer = validate_choice(result["answers"].get("operation", {}), operations)
    operation = operation_answer["choice"]
    target = None
    target_answer = None
    probabilities = {}
    if operation in targets:
        # Unused target heads cannot cause an action. Validate the head selected by the operation.
        target_answer = validate_choice(result["answers"].get(operation.lower() + "_target", {}), targets[operation])
        target = target_answer["choice"]
        choice = targets[operation][target]["id"]
        probabilities = {a["id"]: target_answer["probabilities"][index] for index, a in targets[operation].items()}
    else:
        choice = controls[operation]["id"] if operation in controls else operation
        probabilities[choice] = operation_answer["probabilities"][operation]
    return {
        "choice": choice,
        "operation": operation,
        "target": target,
        "confidence": operation_answer["confidence"],
        "probabilities": probabilities,
        "operation_probabilities": operation_answer["probabilities"],
        "target_probabilities": target_answer["probabilities"] if target_answer else {},
        "target_confidence": target_answer["confidence"] if target_answer else None,
        "raw_answers": result["answers"],
        "model": result["model"],
        "usage": result.get("usage", {}),
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "request": body,
    }


def field_context(goal, action, page, history):
    return {
        "goal": goal,
        "field": {k: action.get(k) for k in ("label", "role", "value")},
        "page": {"title": page["title"], "text": page["text"][:6000]},
        "recent_actions": [{k: h.get(k) for k in ("action", "text")} for h in history[-6:]],
    }


def local_endpoint(base):
    return urlparse(base).hostname in {"localhost", "127.0.0.1", "::1"}


def chat_json(system, context, max_tokens=1024, timeout=None):
    """One JSON-mode call to the OpenAI-compatible text model. Local servers need no key."""
    base = os.environ.get("TEXT_MODEL_BASE_URL", "https://api.deepseek.com/v1").rstrip("/")
    key = os.environ.get("TEXT_MODEL_API_KEY")
    if not key and not local_endpoint(base):
        raise ValueError("The text model needs TEXT_MODEL_API_KEY; no text is hardcoded or guessed by the executor.")
    model = os.environ.get("TEXT_MODEL", "deepseek-chat")
    no_reasoning = os.environ.get("TEXT_MODEL_REASONING") == "none"
    if local_endpoint(base):
        # Ollama, LM Studio and mlx_lm speak the plain OpenAI dialect.
        reasoning = {"reasoning_effort": "none"} if no_reasoning else {}
    elif "api.deepseek.com/" in (base + "/"):
        reasoning = {"thinking": {"type": "disabled"}}
    else:
        reasoning = {"reasoning": {"enabled": False}} if no_reasoning else {"reasoning": {"effort": "low"}}
    started = time.perf_counter()
    result = post_json(
        base + "/chat/completions",
        key or "local",
        {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": 0,
            "response_format": {"type": "json_object"},
            **reasoning,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": json.dumps(context)}],
        },
        timeout,
    )
    output = json.loads(result["choices"][0]["message"]["content"])
    return output, {
        "model": model,
        "latency_ms": round((time.perf_counter() - started) * 1000),
        "usage": result.get("usage", {}),
    }


def field_text(context):
    try:
        output, meta = chat_json(TEXT_VALUE, context)
        value = output["text"]
        if set(output) != {"text"} or not isinstance(value, str) or not value.strip() or len(value) > 2000:
            raise ValueError()
    except (ValueError, KeyError, TypeError) as error:
        if "TEXT_MODEL_API_KEY" in str(error):
            raise
        raise ValueError("Text helper returned no valid field value; nothing typed.") from None
    return value, meta


def plan_goal(goal, fields=(), attempts=3):
    """Once per task: the values the goal states, the item to open, and the visible finish condition.
    `fields` are the observed field labels, so requirements can name the field that sets them. No site plan."""
    context = {"goal": goal, "fields_on_page": list(fields)[:40]}
    for attempt in range(attempts):
        try:
            plan, meta = parse_plan(*chat_json(GOAL_PLAN, context))
            break
        except ValueError as error:
            if "TEXT_MODEL_API_KEY" in str(error) or attempt == attempts - 1:
                raise
    if plan["requirements"] or plan["open"]:
        return plan, meta
    # Nothing to fill and nothing to open: the goal is about using controls ("open the chat, start a call").
    # One more call lists them as ordered steps. Any failure keeps the plain plan.
    try:
        output, step_meta = chat_json(STEP_PLAN, context)
        plan["steps"] = parse_steps(output.get("steps"))
        meta = {**meta, "latency_ms": meta["latency_ms"] + step_meta["latency_ms"], "calls": 2}
    except (ValueError, RuntimeError, KeyError, TypeError, AttributeError):
        pass
    return plan, meta


def parse_plan(output, meta):
    try:
        requirements = [
            {"what": r["what"].strip(), "value": r["value"].strip()}
            for r in output["requirements"]
            if isinstance(r.get("what"), str) and isinstance(r.get("value"), str) and r["value"].strip()
        ]
        finish, item = output["finish"], output.get("open")
        if not isinstance(finish, str) or not finish.strip() or len(requirements) > 12:
            raise ValueError()
        item = item.strip() if isinstance(item, str) and item.strip() else None
        steps = parse_steps(output.get("steps"))
    except (ValueError, KeyError, TypeError, AttributeError):
        raise ValueError("Goal planner returned no valid plan; no action executed.") from None
    return {"requirements": requirements, "open": item, "finish": finish.strip(), "steps": steps}, meta


def parse_steps(steps, tabs=None, limit=12):
    """Ordered steps on page controls: what to do, the labels its control likely shows, and any text to type.
    With `tabs`, each step also names its tab (one of `tabs`, else None: the policy then decides), the text it waits
    to see, and whether the goal made it optional."""
    def text(value):
        return value if isinstance(value, str) and value.strip() else None

    parsed = []
    for st in steps or []:
        if not isinstance(st, dict) or not isinstance(st.get("do"), str) or not st["do"].strip():
            continue
        # A label written as alternatives ("Online/Offline") is each of them.
        labels = [part.strip() for label in st.get("labels") or [] if isinstance(label, str)
                  for part in (label.split("/") if " " not in label.strip() else [label])]
        step = {"do": st["do"].strip(), "labels": [label for label in labels if label][:4],
                "text": text(st.get("text"))}
        if tabs is not None:
            step.update(tab=st.get("tab") if st.get("tab") in tabs else None, see=text(st.get("see")),
                        optional=st.get("optional") is True)
        parsed.append(step)
    if len(parsed) > limit:
        raise ValueError("Too many steps")
    return parsed


def plan_tabs(goal, tabs, attempts=3):
    """Once per task with several tabs: ordered steps, each on a named tab. `tabs` maps each tab's name to the labels
    it shows now. One call splits the goal into parts by tab, then one call per part lists its steps: a small local
    model plans one tab's part far better than the whole goal. Credential references ({{NAME}}) stay references;
    their values never reach the text model."""
    def ask(system, context, key, parse):
        for attempt in range(attempts):
            try:
                output, meta = chat_json(system, context, timeout=90)
                result = parse(output.get(key) if isinstance(output, dict) else None)
                if not result:
                    raise ValueError("Goal planner returned no steps; no action executed.")
                return result, meta
            except ValueError as error:
                if "TEXT_MODEL_API_KEY" in str(error) or attempt == attempts - 1:
                    raise

    def parts(value):
        return [{"tab": p["tab"], "goal": p["goal"].strip()} for p in value or [] if isinstance(p, dict)
                and p.get("tab") in tabs and isinstance(p.get("goal"), str) and p["goal"].strip()]

    split, meta = ask(TAB_SPLIT, {"goal": goal, "tabs": list(tabs)}, "parts", parts)
    steps, latency, calls = [], meta["latency_ms"], 1
    for part in split:
        context = {"goal": part["goal"], "fields_on_page": list(tabs[part["tab"]])[:30]}
        found, step_meta = ask(TAB_STEPS, context, "steps", lambda value: parse_steps(value, list(tabs)))
        steps += [{**step, "tab": part["tab"]} for step in found]
        latency, calls = latency + step_meta["latency_ms"], calls + 1
    if len(steps) > 40:
        raise ValueError("Too many steps")
    return {"requirements": [], "open": None, "finish": "Every step is done.", "steps": steps}, {
        **meta, "latency_ms": latency, "calls": calls, "parts": split}


def resolve_step(goal, step, controls):
    """When no control's label names a step, the text model picks one observed control by index, or none.
    `controls` maps index -> description. It returns an index from `controls` or None, never anything else."""
    context = {"goal": goal, "step": step["do"], "text_to_type": step["text"], "controls": controls}
    output, meta = chat_json(STEP_TARGET, context)
    index = output.get("index") if isinstance(output, dict) else None
    index = str(index) if isinstance(index, (int, str)) and not isinstance(index, bool) else None
    return (index if index in controls else None), meta
