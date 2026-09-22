"""Local decisions with Laya: open weights on Apple Silicon (MLX), no decision API.

A 421M typed-decision encoder answers narrow questions well: which field sets X, does this value
satisfy Y, which suggestion matches Z. It does not reliably answer "what should a browser do next?".
So each step asks narrow questions and composes them with rules that hold on any site:
fill the values the goal states, pick what a typed query or an opened control offers,
submit, then check the goal's finish condition. Every target is an observed element.
"""

import os
import re
import time
import unicodedata
from urllib.parse import urlparse

from . import credentials
from .model import plan_goal, plan_tabs, resolve_step, validate_choice

DEFAULT_MODEL = "aac6fef/laya-typed-decisions-mlx"
FIELD_ROLES = {"combobox", "textbox", "searchbox", "spinbutton", "checkbox", "radio", "switch"}
TOGGLES = {"checkbox", "radio", "switch"}
NEGATIVE = {"no", "off", "false", "unchecked", "disabled", "without", "none"}
SUBMIT_WORDS = {"search", "submit", "find", "go", "apply", "done", "continue", "next", "confirm", "show"}
STOP_WORDS = {"the", "and", "for", "with", "from", "find", "open", "stop", "when", "visib", "page", "are", "this"}
MONTH_DAY = re.compile(
    r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)(?:uary|ruary|ch|il|e|y|ust|t|tember|ober|ember)?"
    r"\s+(\d{1,2})\b|\b(\d{1,2})\s+(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)"
)
YES_NO = {"yes": "the current value satisfies the requirement", "no": "the current value does not satisfy it"}
# Contrasting page kinds separated finished from unfinished pages far better than a yes/no question.
UNFINISHED = {
    "form": "a search form that still has to be submitted",
    "results": "a list of search results, without the requested item opened",
    "other": "some other page",
}

MAX_RESULT_WAITS = 12  # about 2 s of observation while submitted results load
# Goals that are a sequence of steps on controls ("open the chat, start a call, end it, close it").
CONFIRM_WORDS = {"yes", "confirm", "ok", "okay", "continue", "proceed", "accept", "allow", "agree", "send", "submit"}
GUESS_AFTER = 3  # waits before the text model is asked which control performs a step no label names
RESOLVE_CALLS = 3  # text-model calls per step at most; each new set of visible controls may need one
MAX_STEP_WAITS = 24  # about 8 s for a step's control to appear; then the step is skipped
# With several tabs, a control can wait on another tab through a backend (an agent accepts a customer's chat).
TAB_STEP_WAITS = 90  # about 30 s for such a control to appear
TAB_GUESS_AFTER = 15  # and about 5 s before the text model is asked, so it is not asked while the control is on its way
SEE_WAITS = 150  # about 60 s for text a step waits to see (a reply sent from another tab)
OPTIONAL_WAITS = 6  # an optional step ("log in if needed") whose control does not show is not needed
SETTLE_WAITS = 10  # about 1.5 s for the page to react to a step before the step counts as having done nothing
STEP_TRIES = 3  # a step whose click visibly did nothing is decided again (controls can show before they work)

_MODEL = None


def laya():
    """Load once per process. The first forward pass also compiles Metal kernels, so warm it here."""
    global _MODEL
    if _MODEL is None:
        import laya_mlx

        _MODEL = laya_mlx.load(os.environ.get("LAYA_MODEL", DEFAULT_MODEL))
        _MODEL.system_one("Warm up.", {"q": {"type": "choice", "instructions": "Warm up.", "criteria": ["a", "b"]}})
    return _MODEL


def fold(text):
    text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


def words(text):
    return {w[:5] for w in fold(text).split() if (len(w) > 2 or w.isdigit()) and w[:5] not in STOP_WORDS}


def month_day(text):
    m = MONTH_DAY.search(fold(text))
    return (m[1] or m[4], int(m[2] or m[3])) if m else None


def observed(page):
    """One entry per DOM node, indexed in the same order as model.action_space (the inspector's indices)."""
    elements, nodes = [], {}
    for action in page["actions"]:
        if action["kind"] not in {"click", "fill", "select"}:
            continue
        e = nodes.get(action["node"])
        if e is None:
            e = nodes[action["node"]] = {
                "node": action["node"],
                "index": str(len(elements) + 1),
                "role": action.get("role", ""),
                "label": action["label"].split(" → ")[0].strip(),
                "value": action.get("current_value", action.get("value", "")) or "",
                "checked": action.get("checked"),
                "expanded": action.get("expanded"),
                "hint": action.get("hint", ""),
                "tab": action.get("tab"),
                "secret": bool(action.get("secret")),
                "actions": {},
                "options": [],
            }
            elements.append(e)
        if action["kind"] == "select":
            e["options"].append(action)
        else:
            e["actions"].setdefault(action["kind"], action)
    for e in elements:
        if e["role"] in TOGGLES:
            e["current"] = "checked" if e["checked"] in {"true", True} else "unchecked"
        elif e["value"] or (is_field(e) and e["role"] != "button"):
            e["current"] = e["value"]
        else:
            e["current"] = e["label"]
    return elements


def is_field(e):
    # Buttons that display a count ("1 passenger") act as fields; dated buttons are calendar choices.
    counter = e["role"] == "button" and re.search(r"\d", e["label"]) and not month_day(e["label"])
    return e["role"] in FIELD_ROLES or bool(e["options"]) or bool(counter)


def display(e):
    """The name the planner sees and may copy as a requirement's "what"."""
    return f"{e['label'][:80]} ({e['hint'][:60]})" if e.get("hint") else e["label"][:80]


def named(e, step):
    """The control's label carries every word of one of the step's likely labels ("Call" in "Start a call"). A step
    planned without labels, or a typing step, is also named by its own words ("type a message")."""
    label = words(e["label"])
    texts = step["labels"] + ([step["do"]] if step.get("do") and (step.get("text") or not step["labels"]) else [])
    return any(w and w <= label for w in (words(text) for text in texts))


def plannable(e):
    """Elements a requirement can name: fields, and buttons, which often open pickers for dates or counts."""
    return is_field(e) or e["role"] == "button"


def mark(e):
    """A control's identity across observations: its node and label. A running clock ("3 minutes 12 seconds") in a
    label is not a new control."""
    return e["node"], re.sub(r"\d+", "#", e["label"])


def tab_text(page, tab):
    """The visible text of one tab (several tabs observed as one page), or of the whole page."""
    return next((t["text"] for t in page.get("tabs") or [] if t["name"] == tab), page["text"])


def describe(e):
    text = f"[{e['tab']}] " if e.get("tab") else ""
    text += f"{e['role']} {e['label'][:70]}"
    if e.get("hint"):
        text += f" ({e['hint'][:50]})"
    if is_field(e) and e["current"] != e["label"]:
        text += f" = {e['current'][:40] or '(empty)'}"
    if e["options"]:
        text += " (options: " + ", ".join(option_label(o) for o in e["options"][:6]) + ")"
    return text


def option_label(option):
    return option["label"].split(" → ")[-1]


def settled(requirement, e):
    """True or False when plain code can tell; None asks Laya."""
    value = requirement["value"]
    if e["role"] in TOGGLES:
        return (e["current"] == "checked") != bool(set(fold(value).split()) & NEGATIVE)
    current = e["current"]
    if not fold(current):
        return False
    if any(fold(option_label(o)) == fold(value) for o in e["options"]):
        return False  # The requested value is still offered as an unselected option.
    wanted, shown = month_day(value), month_day(current)
    if wanted and shown:
        return wanted == shown
    fv, fc = fold(value), fold(current)
    if fv and (fv in fc or (len(fc) >= 3 and fc in fv)):
        return True
    return None


def summary(text, about, limit=200):
    """The page lines sharing the most words with `about`, in page order. Page chrome comes first on most
    sites, so the opening characters rarely describe the page."""
    lines = [line for line in text.splitlines() if line.strip()]
    target = words(about)
    ranked = sorted(range(len(lines)), key=lambda i: -len(words(lines[i]) & target))
    kept, size = set(), 0
    for i in ranked:
        if size >= limit:
            break
        kept.add(i)
        size += len(lines[i])
    return " | ".join(lines[i] for i in sorted(kept))[: limit * 2]


def location(url):
    """Sites often rewrite the query on every edit; a new host or path means a new page."""
    parsed = urlparse(url)
    return parsed.netloc, parsed.path


def titled(title, name):
    """The page title carries most of the name's words, and they make up most of the title. A search results
    page ("X - Search results - Site") names the item too, but is not its page."""
    wanted, shown = words(name), words(title)
    shared = len(wanted & shown)
    return bool(wanted) and shared >= max(1, round(0.6 * len(wanted))) and shared >= 0.6 * len(shown)


def relevance(e, text):
    s = len(words(f"{e['label']} {e.get('hint', '')} {e['current']}") & words(text))
    date = month_day(text)
    return s + 5 if date and month_day(e["label"]) == date else s


def shortlist(candidates, text, limit, bonus=None, top_tier=False, margin=1):
    """Keep the likeliest candidates, in document order, so options fit Laya's 256-token question budget.
    With top_tier, only candidates scoring within `margin` points of the best remain."""
    scores = {e["node"]: relevance(e, text) + (bonus(e) if bonus else 0) for e in candidates}
    if top_tier and candidates:
        best = max(scores.values())
        candidates = [e for e in candidates if scores[e["node"]] >= best - margin]
    kept = {e["node"] for e in sorted(candidates, key=lambda e: -scores[e["node"]])[:limit]}
    return [e for e in candidates if e["node"] in kept]


class LayaPolicy:
    def __init__(self, goal, tabs=None):
        self.goal = goal
        self.tabs = list(tabs) if tabs else None  # names of the tabs observed as one page
        self.plan = None
        self.plan_meta = None
        self.fields = {}  # requirement index -> observed node
        self.met = set()
        self.skipped = set()
        self.attempts = {}
        self.pending = None  # the latest decision, until history shows it executed
        self.last = None  # the latest executed step
        self.edit_url = None
        self.submitted = False
        self.waits = 0
        self.seen = 0
        self.tried = {}  # label -> times clicked as the next step
        self.typed = False  # text entered since the last submit
        self.acted = None
        self.search_added = False
        self.frozen = set()  # requirements submitted to an earlier page
        self.failed = {}  # node -> decisions on it that could not execute
        self.step = 0  # the next step of a step-by-step goal
        self.used = set()  # marks of controls a step used; a later step never reuses them
        self.opened = None  # the latest executed step, until a later action: it may still need confirming
        self.base = set()  # marks of controls shown before the latest executed action
        self.tries = {}  # step -> executions
        self.resolved = {}  # (step, visible controls) -> the text model's answer
        self.typing = None  # the latest typing step, until the page shows its text outside the field
        self.sends = set()
        self.confirmed = {}  # step -> label of the control that confirmed it (the Send a typed message revealed)
        self.calls = []  # text-model calls made while choosing, for the run's model-call count
        self.view = None  # the previous observation's controls and text, to act only on a settled page
        self.proposed = None

    # Bookkeeping ---------------------------------------------------------------------------------------------

    def sync(self, history):
        step = self.pending
        if step and step["node"] is not None and len(history) == self.seen:
            # The decision never ran: the target was covered or the page changed first.
            self.failed[step["node"]] = self.failed.get(step["node"], 0) + 1
        if len(history) > self.seen and step and history[-1].get("choice") == step["choice"]:
            self.last = step
            if step["req"] is not None:
                self.attempts[step["req"]] = self.attempts.get(step["req"], 0) + 1
                self.edit_url, self.submitted = step["url"], False
            if step["kind"] == "fill":
                self.typed = True
            if step["kind"] in {"submit", "item"}:
                self.submitted = True
            if step["kind"] in {"submit", "item", "next"}:
                self.tried[step["label"]] = self.tried.get(step["label"], 0) + 1
            self.waits = self.waits + 1 if step["kind"] == "wait" else 0
            if step["kind"] in {"step", "confirm"}:
                self.used.add(mark(step))
            if step["kind"] == "step":
                self.step = step["seq"] + 1
                self.tries[step["seq"]] = self.tries.get(step["seq"], 0) + 1
                # A credential cannot be read back from the page (a password never is), so it is not tracked.
                text = self.plan["steps"][step["seq"]]["text"]
                if text and not credentials.references(text):
                    self.typing, self.sends = step, set()
            if step["kind"] == "confirm" and self.typing:
                self.sends.add(step["node"])  # a retry may press the same Send button again
            if step["kind"] == "confirm" and self.opened:
                self.confirmed[self.opened["seq"]] = step["label"]
            if step["kind"] != "wait":
                self.opened = step if step["kind"] == "step" else None
                self.base = step["shown"]
            if step["kind"] != "wait":
                self.acted = step["kind"]  # the latest non-wait step
        self.seen, self.pending = len(history), None

    def ask(self, state, questions):
        result = laya().system_one(state, questions)
        for key, answer in result["answers"].items():
            if questions[key]["type"] == "choice":
                try:
                    validate_choice(answer, questions[key]["criteria"])
                except ValueError:
                    raise ValueError("Invalid Laya response; no action executed.") from None
        self.answers.update(result["answers"])
        self.questions.update(questions)
        self.tokens += result["usage"]["input_tokens"]
        return result["answers"]

    def pick(self, qid, candidates, state, instructions, text, limit=20, bonus=None, allow_none=False,
             top_tier=False, margin=1):
        date = month_day(text)
        dated = [e for e in candidates if date and month_day(e["label"]) == date]
        if len(dated) == 1:
            return dated[0], None  # An exact calendar match needs no model call; Laya confused adjacent days.
        ranked = shortlist(candidates, text, limit, bonus, top_tier, margin)
        if not ranked:
            return None
        criteria = {str(e["node"]): describe(e) for e in ranked}
        if allow_none:
            criteria["none"] = "none of these"
        answer = self.ask(state, {qid: {"type": "choice", "instructions": instructions, "criteria": criteria}})[qid]
        if answer["choice"] == "none":
            return None
        return next(e for e in ranked if str(e["node"]) == answer["choice"]), answer

    # Decisions ----------------------------------------------------------------------------------------------

    def choose(self, page, history):
        started = time.perf_counter()
        self.sync(history)
        if self.edit_url and location(page["url"]) != location(self.edit_url):
            # The form was submitted to a new page. Its values were used; a new page's empty copy of the
            # form (a site-wide search box, say) does not undo them.
            self.typed = False
            self.frozen |= self.met
        elements = observed(page)
        if self.plan is None:
            labels = list(dict.fromkeys(display(e) for e in elements if plannable(e)))
            if self.tabs:
                self.plan, self.plan_meta = plan_tabs(self.goal, {tab: list(dict.fromkeys(
                    display(e) for e in elements if plannable(e) and e["tab"] == tab)) for tab in self.tabs})
            else:
                self.plan, self.plan_meta = plan_goal(self.goal, labels)
            # A step label copied from the page's own fields names that field only if the goal does too.
            copied = {fold(label) for label in labels}
            for step in self.plan.get("steps", []):
                step["labels"] = [t for t in step["labels"]
                                  if fold(t) not in copied or words(t) <= words(self.goal)]
        self.answers, self.questions, self.tokens = {}, {}, 0
        self.proposed = None
        steps = self.plan.get("steps")
        op, element, action, picked, kind, req, text = (
            self.sequence(page, elements, steps) if steps else self.decide(page, elements))
        answer = picked[1] if picked else None
        indices = {str(e["node"]): e["index"] for e in elements}
        choice = action["id"] if action else {"DONE": "DONE", "BLOCKED": "BLOCKED"}.get(op, "wait")
        probability = answer["probabilities"][answer["choice"]] if answer else 1.0
        target = element["index"] if element else None
        if action and action["kind"] == "select":
            target = f"{element['index']}:{element['options'].index(action) + 1}"
        self.pending = {
            "choice": choice, "kind": kind, "req": req, "node": element["node"] if element else None,
            "url": page["url"], "before": {e["node"] for e in elements},
            "label": element["label"] if element else None, "seq": self.proposed,
            # Sites relabel a control in place ("Start a call" becomes "Yes, Start a call"): node and label.
            "shown": {mark(e) for e in elements}, "tab": element["tab"] if element else None,
            "tab_shown": {mark(e) for e in elements if element and e["tab"] == element["tab"]},
        }
        return {
            "choice": choice,
            "operation": op,
            "target": target,
            "text": text,
            "confidence": answer["confidence"] if answer else 1.0,
            "probabilities": {choice: probability},
            "operation_probabilities": {op: probability},
            "target_probabilities": {
                indices.get(k, k): p for k, p in (answer or {}).get("probabilities", {}).items() if k in indices
            },
            "target_confidence": answer["confidence"] if answer and element else None,
            "raw_answers": self.answers,
            "model": os.environ.get("LAYA_MODEL", DEFAULT_MODEL),
            "usage": {"input_tokens": self.tokens, "output_tokens": 0},
            "latency_ms": round((time.perf_counter() - started) * 1000),
            "request": {"plan": self.plan, "questions": self.questions},
        }

    def decide(self, page, elements):
        """Return (operation, element, action, (element, answer) or None, step kind, requirement, text)."""
        reqs = self.plan["requirements"]
        by_node = {e["node"]: e for e in elements}
        clickable = [e for e in elements if "click" in e["actions"] and self.failed.get(e["node"], 0) < 2]
        last = self.last

        # 1. A typed query or an opened control offers new choices: take the one its requirement asks for.
        if last and last["kind"] in {"fill", "open"} and last["req"] is not None:
            r = reqs[last["req"]]
            new = [e for e in clickable if e["node"] not in last["before"]]
            # Suggestions name the value; controls that appear beside them ("Clear", "Swap") do not.
            new = [e for e in new if relevance(e, r["value"])]
            state = f"Requirement: {r['what']} = {r['value']}"
            picked = self.pick("option", new, state, f"Which option sets {r['what']} to {r['value']}?",
                               r["value"], allow_none=True)
            if picked:
                return self.click(picked, "pick", last["req"])

        # An item to open that nothing on the page names has to be searched for first.
        item = self.plan.get("open")
        if item and not self.search_added and not titled(page["title"], item):
            search = [e for e in elements if "fill" in e["actions"] and
                      (e["role"] == "searchbox" or "searc" in words(e["label"]))]
            if search and not any(relevance(e, item) for e in clickable):
                reqs.append({"what": "search", "value": item})
                self.search_added = True

        # 2. Requirements in the goal's order: map each to an observed element, then check its value.
        if reqs and not any(plannable(e) for e in elements):
            # Nothing to fill yet: the page is still loading or showing an interstitial. This costs no attempt.
            return "WAIT", None, None, None, "wait", None, None
        self.refresh(page, elements, by_node)
        for i, r in enumerate(reqs):
            if i in self.met or i in self.skipped:
                continue
            if self.attempts.get(i, 0) >= 3:
                self.skipped.add(i)
                continue
            e = by_node.get(self.fields.get(i))
            state = f"Requirement: {r['what']} = {r['value']}"
            if e is None:
                # A control about this requirement that already displays its value ("Travellers and cabin
                # class: 1 Adult, Economy") settles it. Both the name and the value must appear.
                # Calendar days name a date too, but they are choices, not displays.
                shown = [c for c in elements if plannable(c) and c["role"] not in TOGGLES and not month_day(c["label"])
                         and words(c["label"]) & words(r["what"]) and words(c["label"]) & words(r["value"])
                         and settled(r, {**c, "current": c["label"], "options": []})]
                if shown:
                    self.met.add(i)
                    continue
                about = f"{r['what']} {r['value']}"
                options = [c for c in clickable if c["role"] not in TOGGLES or words(c["label"]) & words(about)]
                options = [c for c in options if relevance(c, about)] or options
                picked = self.pick(f"set_{i}", options, state, f"Which element sets {r['what']} to {r['value']}?",
                                   about, allow_none=True)
                if not picked:
                    # Often the page is mid-render. Wait (one attempt); the attempt limit skips it for good.
                    return "WAIT", None, None, None, "wait", i, None
                # It may open a picker (a trip-type menu, a calendar); step 1 then chooses from what appears.
                return self.click(picked, "open", i)
            if "fill" in e["actions"]:
                return "TYPE_TEXT", e, e["actions"]["fill"], None, "fill", i, r["value"]
            if e["options"]:
                exact = [o for o in e["options"] if fold(option_label(o)) == fold(r["value"])]
                similar = [o for o in e["options"] if words(option_label(o)) & words(r["value"])]
                if exact or len(similar) == 1:
                    return "SELECT", e, (exact or similar)[0], None, "select", i, None
                answer = self.ask(state, {f"select_{i}": {
                    "type": "choice",
                    "instructions": f"Which option sets {r['what']} to {r['value']}?",
                    "criteria": {str(n): option_label(o) for n, o in enumerate(e["options"])},
                }})[f"select_{i}"]
                option = e["options"][int(answer["choice"])]
                return "SELECT", e, option, (e, answer), "select", i, None
            return "CLICK", e, e["actions"]["click"], None, "toggle" if e["role"] in TOGGLES else "open", i, None

        # 3. Everything stated is set. Check the finish condition once something was submitted or navigated.
        finish = self.plan["finish"]
        navigated = self.edit_url is not None and location(page["url"]) != location(self.edit_url)
        if item:
            # An opened item names its page. Accept its title when it carries the item's words, or the words
            # of the element Laya chose to open it.
            chosen = (last and last["kind"] == "item" and relevance({"label": last["label"], "current": ""}, item)
                      and titled(page["title"], last["label"]))
            if chosen or titled(page["title"], item):
                return "DONE", None, None, None, "done", None, None
        elif self.submitted or navigated or not reqs:
            # Laya's yes/no finish check was unreliable; contrasting page kinds separated real outcomes.
            state = f"Page title: {page['title']}\nPage: {summary(page['text'], finish)}"
            done = self.ask(state, {"done": {
                "type": "choice", "instructions": "Which best describes the current page?",
                "criteria": {"finish": finish, **UNFINISHED},
            }})["done"]
            # A submit that led to a new page while every stated value still holds is a search outcome:
            # accept it once Laya sees results, and wait while they load.
            searched = self.acted == "submit" and navigated and self.met | self.skipped >= set(range(len(reqs)))
            # Laya rarely labels a real results page as one, so also count results that name the requested
            # values: two or more elements mentioning at least three of them (route, date, ...).
            wanted = set().union(*(words(r["value"]) for r in reqs)) if reqs else set()
            matching = [e for e in clickable if len(words(e["label"]) & wanted) >= min(3, len(wanted))]
            # Laya called a loading results skeleton "finish", so with stated values it needs rows naming them,
            # or its verdict after the full wait for results.
            laya_done = done["choice"] in {"finish", "results"}
            if (not reqs and done["choice"] == "finish") or (
                searched and (len(matching) >= 2 or (laya_done and self.waits >= MAX_RESULT_WAITS))
            ):
                return "DONE", None, None, None, "done", None, None
            if searched and self.waits < MAX_RESULT_WAITS:
                return "WAIT", None, None, None, "wait", None, None
        if last and last["kind"] in {"submit", "item", "next"} and self.waits < 2 and (self.submitted or navigated):
            return "WAIT", None, None, None, "wait", None, None
        mapped = {self.fields.get(i) for i in range(len(reqs))}
        fresh = {e["node"] for e in clickable if last and e["node"] not in last["before"]}
        # A next-step target clicked twice already has shown it does not advance the goal.
        candidates = [e for e in clickable if e["node"] not in mapped and self.tried.get(e["label"], 0) < 2]

        def submits(e):
            return 2 * bool(set(fold(e["label"]).split()) & SUBMIT_WORDS) + (e["node"] in fresh)

        state = f"Goal: {self.goal}\nFinish condition: {finish}\nPage title: {page['title']}\nPage: {page['text']}"
        if self.typed:
            # Typed search text is not applied until its form is submitted, so submit before opening results.
            buttons = [e for e in candidates if e["role"] == "button"]
            picked = self.pick("submit", buttons, state, "Which button submits the filled form?", "",
                               bonus=submits, top_tier=True)
            if picked:
                return self.click(picked, "submit", None)
        if item:
            named = [e for e in candidates if relevance(e, item) and not is_field(e)]
            # Near-duplicates ("completeness" vs "incompleteness") fooled Laya, so only the elements naming
            # the most of the item's words stay; Laya breaks exact ties.
            picked = self.pick("item", named, state, f"Which element opens {item}?", item, top_tier=True, margin=0)
            if picked:
                return self.click(picked, "item", None)
        # Otherwise click what the goal names: the elements sharing the most words with it; Laya breaks ties.
        picked = self.pick("next", candidates, state, "Which element should be clicked next to reach the goal?",
                           f"{self.goal} {finish}", bonus=lambda e: e["node"] in fresh, top_tier=True, margin=0)
        if not picked:
            return "BLOCKED", None, None, None, "blocked", None, None
        return self.click(picked, "next", None)

    def sequence(self, page, elements, steps):
        """A goal that is a sequence of steps: do each in order on the control whose label names it, finish what
        a step opens (a confirmation, a Send button), wait for controls that are still appearing, and skip a step
        whose control never appears. With several tabs, each step acts only on its own tab. Every target is an
        observed element."""
        def here(tab):  # the controls of one tab (every control when the page is one tab)
            return [e for e in elements if tab is None or e["tab"] == tab]

        current = steps[self.step].get("tab") if self.step < len(steps) else None
        view, self.view = self.view, ({mark(e) for e in here(current)},
                                      re.sub(r"\d+", "#", tab_text(page, current)))
        opened = self.opened
        # Typed text is delivered once the page shows it outside its field (a sent message in a transcript). If it
        # is in neither place, a reset or a send before the page was ready lost it: type it again.
        if typing := self.typing:
            text = fold(steps[typing["seq"]]["text"])
            field = next((e for e in elements if e["node"] == typing["node"]), None)
            in_field = bool(field) and text in fold(field["current"])
            if (text in fold(tab_text(page, typing["tab"])) and not in_field) or (
                    field is None and self.last is not typing):
                self.typing = None  # shown, or a later step moved the form on and its field is gone
            elif not in_field:
                if self.waits < SETTLE_WAITS:  # it may still be rendering; later steps wait for it
                    return "WAIT", None, None, None, "wait", None, None
                self.typing = None
                if self.tries.get(typing["seq"], 0) < STEP_TRIES:
                    self.used = {u for u in self.used if u[0] not in {typing["node"], *self.sends}}
                    self.step, self.opened, opened = typing["seq"], None, None
        # Act on the page a step produced, not on the one it is still replacing ("End call" before the call
        # screen closes).
        if opened and {mark(e) for e in here(opened["tab"])} == opened["tab_shown"]:
            if self.waits < SETTLE_WAITS:
                return "WAIT", None, None, None, "wait", None, None
            if self.tries.get(opened["seq"], 0) < STEP_TRIES:
                # Nothing visibly happened: the control may have shown before it worked. Decide the step again.
                self.used.discard(mark(opened))
                self.step, self.opened, opened = opened["seq"], None, None
        # Only a settled page: two observations in a row with the same controls and text on the step's tab.
        if view != self.view:
            return "WAIT", None, None, None, "wait", None, None
        free = [e for e in elements if mark(e) not in self.used and self.failed.get(e["node"], 0) < 2]
        clickable = [e for e in free if "click" in e["actions"]]
        if opened:
            done = steps[opened["seq"]]
            # A confirmation repeats the label that named the clicked control ("End chat" asks "End chat?");
            # another control sharing one of the step's looser labels ("Chat" in "Minimize Chat") does not.
            # A control no label named (the text model chose it) opened choices: the most specific label names one.
            naming = [t for t in done["labels"] if named({"label": opened["label"]}, {"labels": [t]})]
            same = {"labels": [max(naming, key=lambda t: len(words(t)))] if naming else done["labels"][:1]}
            # A confirmation is a button, never a text field ("Type a message and press enter to send").
            new = [e for e in clickable if mark(e) not in opened["shown"] and e["tab"] == opened["tab"]
                   and "fill" not in e["actions"] and (set(fold(e["label"]).split()) & CONFIRM_WORDS or named(e, same))]
            # A choice the next step names belongs to that step (a menu opened to choose "Available" also lists the
            # current "Offline", which repeats the label that opened it).
            following = steps[opened["seq"] + 1] if opened["seq"] + 1 < len(steps) else None
            if following and any(named(e, following) for e in clickable if mark(e) not in opened["shown"]):
                new = []
            picked = self.pick("confirm", new, f"Step: {done['do']}",
                               f"Which option completes this step: {done['do']}?", " ".join(done["labels"]))
            if picked:
                return self.click(picked, "confirm", None)

        def pool(s):
            """The step's own tab. Text goes into fields; a password credential only into a password field."""
            mine = [e for e in free if s.get("tab") is None or e["tab"] == s["tab"]]
            if s["text"]:
                return [e for e in mine if "fill" in e["actions"] and e["secret"] == credentials.secret(s["text"])]
            return [e for e in mine if "click" in e["actions"]]

        patience, guess = (TAB_STEP_WAITS, TAB_GUESS_AFTER) if self.tabs else (MAX_STEP_WAITS, GUESS_AFTER)
        while self.step < len(steps):
            i, s = self.step, steps[self.step]
            if s.get("see"):
                # A step that waits for text to appear on its tab (a reply sent from another tab).
                if fold(s["see"]) in fold(tab_text(page, s.get("tab"))):
                    self.step, self.waits = i + 1, 0
                    continue
                if self.waits < SEE_WAITS:
                    return "WAIT", None, None, None, "wait", None, None
                self.step, self.waits = i + 1, 0  # it never appeared; the run's own checks report it
                continue
            about = f"{s['do']} {' '.join(s['labels'])}"
            question = f"Which element should be used to {s['do']}?"
            picked = None
            later = [t for t in steps[i + 1:] if t.get("tab") == s.get("tab") and not t.get("see")]
            if i - 1 in self.confirmed and named({"label": self.confirmed[i - 1]}, s):
                # The previous step's confirmation was this step's control ("send it" after the Send that typing
                # revealed was pressed): already done.
                self.step, self.waits = i + 1, 0
                continue
            if exact := [e for e in pool(s) if named(e, s)]:
                picked = self.pick(f"step_{i}", exact, f"Step: {s['do']}", question, about)
            elif s.get("optional"):
                # Only needed when its own control shows ("log in if needed"). A later step's control on the same
                # tab, or none after a short wait, means it is not needed. The text model never guesses one.
                if self.waits >= OPTIONAL_WAITS or any(named(e, t) for t in later for e in pool(t)):
                    self.step, self.waits = i + 1, 0
                    continue
            elif not s["text"] and (ahead := self.ahead(i, steps, pool)) is not None:
                # A later step's control appeared since the last action, so this step has none of its own
                # ("start a conversation" can begin by itself once a chat opens). A control that was already
                # there (a Minimize button) says nothing: the current step's control may still be loading.
                # A step with text to type is never skipped this way: the text is part of the goal.
                self.step, self.waits = ahead, 0
                continue
            elif self.waits >= guess:
                # No label names it: the text model reads the step and the visible controls. It never takes a
                # control that another step names.
                others = [t for t in steps if t is not s]
                picked = self.resolve(i, s, [e for e in pool(s) if not any(named(e, t) for t in others)])
            if picked:
                self.proposed, e = i, picked[0]
                if s["text"]:
                    return "TYPE_TEXT", e, e["actions"]["fill"], picked, "step", None, s["text"]
                return self.click(picked, "step", None)
            if self.waits < patience:
                return "WAIT", None, None, None, "wait", None, None
            self.step, self.waits = i + 1, 0  # its control never appeared
        return "DONE", None, None, None, "done", None, None

    def ahead(self, i, steps, pool):
        """The later step whose control appeared since the last action, if any. On one page only the next step
        counts. With several tabs, a backend can make the goal move on by itself (a chat offer arrives while the
        agent is already available), so any later step on the same tab counts, up to one that types or waits."""
        for j in range(i + 1, len(steps)):
            t = steps[j]
            if t.get("see") or t.get("tab") != steps[i].get("tab"):
                return None
            if any(named(e, t) and mark(e) not in self.base for e in pool(t)):
                return j
            if not self.tabs or t["text"]:
                return None
        return None

    def resolve(self, i, step, candidates):
        """Ask the text model once per step and set of visible controls (at most RESOLVE_CALLS per step). Any
        failure is an answer of none: the step keeps waiting and is skipped if its control never appears."""
        key = (i, frozenset(mark(e) for e in candidates))
        if key not in self.resolved:
            if not candidates or sum(k[0] == i for k in self.resolved) >= RESOLVE_CALLS:
                return None
            # Only controls sharing a word with the step: a guess with nothing in common ("Start a call" to send a
            # reply, a code editor to type a message) did the wrong thing. Pickers can offer hundreds: keep 60.
            about = f"{step['do']} {' '.join(step['labels'])}"
            likely = shortlist([e for e in candidates if relevance(e, about)], about, 60)
            if not likely:
                self.resolved[key] = None
                return None
            controls = {e["index"]: describe(e) for e in likely}
            try:
                index, meta = resolve_step(self.goal, step, controls)
                self.calls.append({**meta, "field": f"step: {step['do']}", "value": index})
            except (ValueError, RuntimeError, KeyError, TypeError):
                index = None
            # Remember the control itself: indices shift when the page changes during a slow model call.
            self.resolved[key] = next((mark(e) for e in likely if e["index"] == index), None)
        chosen = self.resolved[key] or next((v for k, v in self.resolved.items() if k[0] == i and v), None)
        return next(((e, None) for e in candidates if mark(e) == chosen), None)

    def click(self, picked, kind, req):
        e = picked[0]
        return "CLICK", e, e["actions"]["click"], picked, kind, req, None

    def assign(self, todo, free):
        """Match requirements to fields. Laya is asked both ways (which field sets this requirement, which
        requirement does this field hold); the product matched Flights fields better than either direction.
        The most confident pairs are assigned first, so two requirements never share one field.
        There is no "none" option: in tests it outvoted the right field. Requirements no field can hold
        reach the attempt limit instead. Laya shares one state per request, so each question is one call."""
        reqs = self.plan["requirements"]
        free = shortlist(free, " ".join(f"{reqs[i]['what']} {reqs[i]['value']}" for i in todo), 20)
        fields = {str(e["node"]): describe(e) for e in free}
        options = {str(i): f"{reqs[i]['what']} = {reqs[i]['value']}" for i in todo}
        by_requirement = {
            i: self.ask(f"Requirement: {options[str(i)]}", {f"field_{i}": {
                "type": "choice", "instructions": "Which element shows or sets this requirement?", "criteria": fields,
            }})[f"field_{i}"]["probabilities"]
            for i in todo
        }
        by_field = {
            node: self.ask(f"Form field: {text}", {f"holds_{node}": {
                "type": "choice", "instructions": "Which requirement does this form field hold?",
                "criteria": {**options, "none": "none of these"},
            }})[f"holds_{node}"]["probabilities"]
            for node, text in fields.items()
        }
        # A shared word between the field's label and the requirement's name ("Departure", "departure date")
        # doubles the pair's score.
        label = {str(e["node"]): words(e["label"]) for e in free}
        pairs = sorted(
            (
                (by_requirement[i][node] * by_field[node][str(i)] * (1 + bool(label[node] & words(reqs[i]["what"]))),
                 i, node)
                for i in todo for node in fields
            ),
            reverse=True,
        )
        done, used = set(), set()
        for _p, i, node in pairs:
            if i not in done and node not in used:
                self.fields[i] = int(node)
                done.add(i)
                used.add(node)

    def refresh(self, page, elements, by_node):
        """Map unmapped requirements to observed elements, then check their current values."""
        reqs = self.plan["requirements"]
        open_reqs = [i for i in range(len(reqs)) if i not in self.skipped and i not in self.frozen]
        fields = [e for e in elements if is_field(e)]
        # One field holds one requirement. Fields kept by other requirements are not offered again.
        taken = {self.fields.get(i) for i in open_reqs if self.fields.get(i) in by_node}
        todo = []
        for i in open_reqs:
            if i in self.met or self.fields.get(i) in by_node:
                continue
            r = reqs[i]
            free = [e for e in fields if e["node"] not in taken]
            # A dropdown offering exactly the requested value needs no model call.
            exact = [e for e in free if any(fold(option_label(o)) == fold(r["value"]) for o in e["options"])]
            # The planner names requirements by the observed field label when one sets them.
            named = [e for e in elements if plannable(e) and e["node"] not in taken
                     and fold(r["what"]) in {fold(e["label"]), fold(display(e))}]
            if len(exact) == 1 or len(named) == 1:
                self.fields[i] = (exact or named)[0]["node"]
                taken.add(self.fields[i])
            else:
                todo.append(i)
        free = [e for e in fields if e["node"] not in taken]
        if todo and free:
            self.assign(todo, free)
        for i in todo:
            # A checkbox is named by what it sets; one sharing no words with the requirement cannot hold it.
            e = by_node.get(self.fields.get(i))
            r = reqs[i]
            if e and e["role"] in TOGGLES and not words(e["label"]) & words(f"{r['what']} {r['value']}"):
                del self.fields[i]
        checks = {}
        for i in open_reqs:
            e = by_node.get(self.fields.get(i))
            if e is None:
                continue
            verdict = settled(reqs[i], e)
            if verdict is None and i not in self.met:
                checks[i] = e
            elif verdict:
                self.met.add(i)
            else:
                self.met.discard(i)
        for i, e in checks.items():
            r = reqs[i]
            answer = self.ask(f"Requirement: {r['what']} = {r['value']}\nCurrent value: {e['current']}", {
                f"met_{i}": {"type": "choice", "instructions": "Does the current value satisfy the requirement?",
                             "criteria": YES_NO},
            })[f"met_{i}"]
            if answer["choice"] == "yes":
                self.met.add(i)
