"""
Stage 2: Diagnosis engine.

Reads one Razorpay-shaped webhook event and decides:
  - what actually went wrong (root_cause)
  - whether it's worth acting on at all (recoverable)

Rule engine first, ALWAYS -- every known Razorpay error code is handled
deterministically via ROOT_CAUSE_RULES, no LLM involved. The LLM fallback
(pipeline/llm_diagnosis.py) only activates for the minority case: a raw
free-text reason the rule table doesn't recognize at all. Every
diagnosis carries a "diagnosis_source" field ("rule", "llm_fallback", or
"rule_failsafe") so you can always tell which path produced it.

Run standalone in VS Code (Run Python File, or `python pipeline/diagnosis.py`):
auto-loads data/events.jsonl (generating it first if missing), diagnoses
every event, writes data/diagnoses.jsonl, and prints a breakdown.
"""
import sys
import os
import json
from collections import Counter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.config import ROOT_CAUSE_RULES
from pipeline.paths import EVENTS_PATH, DIAGNOSES_PATH


def diagnose(event: dict) -> dict:
    payment = event["payload"]["payment"]
    root_cause = payment.get("error_reason", "unknown")
    rule = ROOT_CAUSE_RULES.get(root_cause)

    if rule is not None:
        return {
            "event_id": event["id"],
            "root_cause": root_cause,
            "recoverable": rule["recoverable"],
            "confidence": 0.95 if rule["recoverable"] else 1.0,
            "diagnosis_source": "rule",
        }

    # Rule table doesn't recognize this reason. Before failing safe, try
    # the LLM fallback IF we have genuine raw free text to classify (only
    # live webhook events carry this -- see webhook_app.py's
    # _to_internal_event(), which preserves the pre-normalization text
    # specifically for this). Batch/synthetic events never reach this
    # branch with raw text, since their error_reason values are always
    # already-known keys by construction.
    raw_text = payment.get("raw_error_reason", "")
    if raw_text:
        from pipeline.llm_diagnosis import classify
        llm_result = classify(raw_text)
        if llm_result is not None:
            llm_rule = ROOT_CAUSE_RULES.get(llm_result["root_cause"])
            if llm_rule is not None:  # defensive -- classify() already validates this
                return {
                    "event_id": event["id"],
                    "root_cause": llm_result["root_cause"],
                    "recoverable": llm_rule["recoverable"],
                    "confidence": llm_result["confidence"],
                    "diagnosis_source": "llm_fallback",
                }

    # No rule match, and no LLM fallback available or confident -- fail
    # safe: treat as non-recoverable rather than guessing. This is the
    # SAME safety behavior whether the LLM fallback is disabled, errored,
    # or was genuinely uncertain -- uncertainty never defaults to acting.
    return {
        "event_id": event["id"],
        "root_cause": "unknown",
        "recoverable": False,
        "confidence": 0.0,
        "diagnosis_source": "rule_failsafe",
    }


def _load_events(in_path: str = EVENTS_PATH) -> list:
    if not os.path.exists(in_path):
        # No upstream data yet -- generate it ourselves so this stage
        # can run in complete isolation, e.g. straight after cloning the repo.
        from data.generate_events import generate
        return generate()
    events = []
    with open(in_path) as f:
        for line in f:
            events.append(json.loads(line))
    return events


def run_diagnosis_stage(events: list = None, in_path: str = EVENTS_PATH,
                         out_path: str = DIAGNOSES_PATH) -> list:
    if events is None:
        events = _load_events(in_path)
    diagnoses = [diagnose(e) for e in events]
    with open(out_path, "w") as f:
        for d in diagnoses:
            f.write(json.dumps(d) + "\n")
    return diagnoses


if __name__ == "__main__":
    diagnoses = run_diagnosis_stage()
    counts = Counter(d["root_cause"] for d in diagnoses)
    recoverable = sum(1 for d in diagnoses if d["recoverable"])

    print(f"Diagnosed {len(diagnoses)} events -> data/diagnoses.jsonl")
    print(f"Recoverable: {recoverable} / {len(diagnoses)}")
    print("\nRoot cause breakdown:")
    for cause, n in counts.most_common():
        print(f"  {cause:20s}: {n}")
