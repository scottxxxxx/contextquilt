"""Jev against what CQ ships today, on three real CQ judgments.

ARM "today" uses CQ's OWN prompt builders, imported from the repo, and the
same model the code defaults to, over raw HTTP because that is how
llm_client_anthropic calls the API. It is a faithful reproduction of the
prompt and model, not a call through the worker's code path: no database,
no sanitizer chain, no batching cap. Every number below is proved on that
reproduction and nothing more.

ARM "jev" sends the same text to api.typesafe.ai as typed questions.

Usage: BENCH_KEY_FILE=... python3 tests/benchmark/jev_vs_today.py
           <character|dedup|types|all> [reps]   (BENCH_MODEL picks the baseline)
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(Path(__file__).parent))

from jev_vs_today_data import CHARACTER, DEDUP, TYPES
from contextquilt.services.extraction_schema import sanitize_behavior_observations
from contextquilt.services import behavior_classifier
from contextquilt.services.behavior_classifier import build_classifier_system, classifier_types
from contextquilt.services import semantic_dedup

MANIFEST = json.load(open(REPO / "init-db/11_shouldersurf_schema.json"))

# Anthropic list prices. Sonnet 4.6 is what behavior_classifier.DEFAULT_MODEL
# names today; Haiku is the cheap arm, worth running because the 2026-08-13
# person-analyzer experiment found Haiku failing invisibly on a neighbouring
# judgment, and invisibly is the part that matters.
PRICES = {"claude-sonnet-4-6": (3.00, 15.00), "claude-haiku-4-5": (1.00, 5.00)}
TODAY_MODEL = os.environ.get("BENCH_MODEL", "claude-sonnet-4-6")
TODAY_IN, TODAY_OUT = PRICES[TODAY_MODEL]
JEV_MODEL = "jev-latest"
JEV_IN = 0.042                              # $ per 1M input tokens, output free


def env(name):
    """Read one value from .env without ever printing it."""
    dotenv = REPO / ".env"
    if not dotenv.exists():
        return os.environ.get(name)
    for line in dotenv.read_text().splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name)


def anthropic_key():
    """A key path in BENCH_KEY_FILE, else the environment, else .env.

    Checked for length because the committed .env.example and a working
    dev .env both carry PLACEHOLDERS (18 and 14 characters on 2026-09-22),
    and reading one produced a silent HTTP 401 that looked like a model
    failure until the raw response was printed.
    """
    key_file = os.environ.get("BENCH_KEY_FILE")
    if key_file and Path(key_file).exists():
        candidate = Path(key_file).read_text().strip()
        if candidate:
            return candidate
    for name in ("CQ_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"):
        candidate = os.environ.get(name) or env(name)
        if candidate and len(candidate) > 40:
            return candidate
    return None


def anthropic_call(system, user, max_tokens=2000):
    key = anthropic_key()
    if not key:
        sys.exit("no usable anthropic key: set BENCH_KEY_FILE or CQ_ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": TODAY_MODEL, "max_tokens": max_tokens,
        "system": system, "messages": [{"role": "user", "content": user}],
    }).encode()
    request = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                 "content-type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=120) as response:
        payload = json.loads(response.read().decode())
    seconds = time.time() - started
    text = "".join(b.get("text", "") for b in payload.get("content", []))
    usage = payload.get("usage") or {}
    cost = (usage.get("input_tokens", 0) * TODAY_IN
            + usage.get("output_tokens", 0) * TODAY_OUT) / 1_000_000
    return text, seconds, cost


def jev_call(state, questions):
    key = env("TYPESAFE_API_KEY")
    if not key:
        sys.exit("no TYPESAFE_API_KEY in .env")
    body = json.dumps({"state": state, "model": JEV_MODEL, "questions": questions}).encode()
    request = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    started = time.time()
    with urllib.request.urlopen(request, timeout=60) as response:
        payload = json.loads(response.read().decode())
    seconds = time.time() - started
    tokens = (payload.get("usage") or {}).get("input_tokens") or 0
    return payload.get("answers") or {}, seconds, tokens * JEV_IN / 1_000_000


def loads(text):
    match = re.search(r"\{.*\}", text, re.DOTALL)
    return json.loads(match.group()) if match else {}


# ---------------------------------------------------------------- task 1
def character_today():
    """The SHIPPED guardrail: an English denylist, no model, no network."""
    patches = [{"type": "moment", "value": {"text": t, "owner": o}}
               for t, o, _, _ in CHARACTER]
    started = time.time()
    out = sanitize_behavior_observations({"patches": patches}, None)
    seconds = time.time() - started
    kept = {(p["value"]["text"]) for p in out.get("patches") or []}
    got = [t in kept for t, _, _, _ in CHARACTER]
    return got, seconds, 0.0


CHARACTER_Q = {
    "is_character": {
        "type": "noul",
        "instructions": ("Does this text say what KIND of person someone is, "
                         "rather than reporting one specific action they were "
                         "observed taking on one occasion? Answer for the text "
                         "in whatever language it is written."),
        "criteria": {
            "true": ("The text characterizes the person as a type: defensive, "
                     "thorough, impatient, dismissive, meticulous."),
            "false": ("The text reports a specific observable action taken at a "
                      "particular moment, such as asking, declining, reading "
                      "aloud, or postponing."),
        },
    }
}


def character_jev():
    got, seconds, cost = [], 0.0, 0.0
    for text, owner, _, _ in CHARACTER:
        answers, s, c = jev_call({"observation": text, "owner": owner}, CHARACTER_Q)
        seconds += s
        cost += c
        noul = (answers.get("is_character") or {}).get("noul")
        got.append(noul is not None and noul <= 0.5)   # keep when not character
    return got, seconds, cost


# ---------------------------------------------------------------- task 2
def dedup_today():
    pairs = [(a, b) for a, b, _ in DEDUP]
    text, seconds, cost = anthropic_call(
        semantic_dedup.DEDUP_JUDGE_SYSTEM,
        semantic_dedup.build_dedup_judge_content(pairs))
    # `parse_dedup_verdicts` takes the ALREADY PARSED dict, because the
    # production client hands it `response.content` after its own json load.
    # Passing the raw string made it fail open to all False, which reads on
    # a scoreboard as the judge calling every pair a different fact. The
    # model's answer was perfect; the harness was wrong.
    return semantic_dedup.parse_dedup_verdicts(loads(text), len(pairs)), seconds, cost


DEDUP_Q = {
    "same_fact": {
        "type": "noul",
        "instructions": ("Do statement_a and statement_b record the SAME "
                         "underlying fact, so that a reader who knows the first "
                         "learns nothing new from the second? Different "
                         "wording, or a different language, does not make two "
                         "facts. A different object, person, date, amount or "
                         "scope does."),
        "criteria": {
            "true": ("Both name the same action on the same object, or one "
                     "merely adds detail to the other."),
            "false": ("They name different objects, people, dates, amounts, "
                      "rules or scopes, however similar the wording."),
        },
    }
}


def dedup_jev():
    got, seconds, cost = [], 0.0, 0.0
    for a, b, _ in DEDUP:
        answers, s, c = jev_call({"statement_a": a, "statement_b": b}, DEDUP_Q)
        seconds += s
        cost += c
        noul = (answers.get("same_fact") or {}).get("noul")
        got.append(noul is not None and noul > 0.5)
    return got, seconds, cost


# ---------------------------------------------------------------- task 3
def types_today():
    system = build_classifier_system(MANIFEST)
    items = "\n".join(f"{i}. [{o}] {t}" for i, (t, o, _) in enumerate(TYPES))
    text, seconds, cost = anthropic_call(system, items)
    # CQ's own parser, on the parsed dict, exactly as the worker calls it.
    # None means the judge said nothing usable, and in production None means
    # KEEP, so the row stays a moment. Score it as such rather than as a
    # third outcome that flatters neither arm.
    verdicts = behavior_classifier.parse_classifier_verdicts(
        loads(text), len(TYPES), classifier_types(MANIFEST))
    return [v or "moment" for v in verdicts], seconds, cost


def types_question():
    names = classifier_types(MANIFEST)
    by = {t["domain_type"]: t for t in MANIFEST["patch_types"]
          if isinstance(t, dict) and t.get("domain_type")}
    criteria = {}
    for n in names:
        desc = " ".join(str((by.get(n) or {}).get("description") or n).split())
        criteria[n] = desc[:300]
    return {"which_type": {"type": "choice", "instructions": (
        "This line came out of a stage that records how named people conducted "
        "themselves in one meeting, and that stage drifts: it also records "
        "promises, decisions, events and positions dressed up as conduct. "
        "Which type is this line ACTUALLY? A moment is a choice the owner made "
        "when something else was available to them. If the owner agreed to do "
        "something later it is a commitment. If it states what the owner "
        "prefers or leans toward it is a preference. If it holds true with the "
        "owner's name removed it is a takeaway or an event."),
        "criteria": criteria}}


def types_jev():
    questions = types_question()
    got, seconds, cost = [], 0.0, 0.0
    for text, owner, _ in TYPES:
        answers, s, c = jev_call({"line": text, "owner": owner}, questions)
        seconds += s
        cost += c
        got.append((answers.get("which_type") or {}).get("choice"))
    return got, seconds, cost


# ---------------------------------------------------------------- runner
TASKS = {
    "character": (CHARACTER, [k for _, _, k, _ in CHARACTER], character_today, character_jev),
    "dedup": (DEDUP, [s for _, _, s in DEDUP], dedup_today, dedup_jev),
    "types": (TYPES, [t for _, _, t in TYPES], types_today, types_jev),
}


# "routine" is not a type. `parse_classifier_verdicts` rewrites a KEPT moment
# to that string when the stakes gate fires, so the judge said "moment" and
# then said "drop it as mechanics". Jev was never asked the stakes question,
# so scoring routine as a type miss would penalise the baseline for answering
# a question the other arm never saw. Normalise it on both arms and report
# the stakes calls separately.
NORMALIZE = {"routine": "moment"}


def score(got, truth):
    got = [NORMALIZE.get(g, g) for g in got]
    return sum(1 for g, t in zip(got, truth) if g == t), len(truth)


def run(name, reps):
    _, truth, today, jev = TASKS[name]
    print(f"\n{'=' * 64}\nTASK {name}   n={len(truth)}   reps={reps}")
    results = {}
    for arm, fn in (("today", today), ("jev", jev)):
        accs, secs, costs, wrong = [], [], [], []
        for rep in range(reps):
            got, s, c = fn()
            right, n = score(got, truth)
            accs.append(right / n)
            secs.append(s)
            costs.append(c)
            if rep == 0:
                wrong = [(i, truth[i], got[i]) for i in range(n) if got[i] != truth[i]]
        results[arm] = (accs, secs, costs, wrong)
        print(f"\n  {arm}: accuracy {' '.join(f'{a:.0%}' for a in accs)}"
              f"   wall {sum(secs)/len(secs):.2f}s   cost ${sum(costs)/len(costs):.6f}")
        for i, want, got_v in wrong[:6]:
            item = TASKS[name][0][i]
            print(f"      MISS [{i}] want={want} got={got_v}: {str(item[0])[:70]}")
    return results


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "all"
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    for name in (list(TASKS) if which == "all" else [which]):
        run(name, reps)
