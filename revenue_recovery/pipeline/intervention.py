"""
Stage 3: Intervention engine.

For a recoverable diagnosis, fires EXACTLY ONE bounded action, tags its
cost the moment it fires (so cost accounting is never an afterthought),
and simulates whether it worked.

Swapping the placeholder link for a real Razorpay test-mode payment link
later only touches build_message() below -- nothing else changes.

Run standalone: auto-loads events + diagnoses (running Stage 1 / Stage 2
first if either file doesn't exist yet), writes data/interventions.jsonl,
and prints a cost/recovery summary.
"""
import sys
import os
import json
import random

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.config import (
    ROOT_CAUSE_RULES, ACTION_MESSAGES, MOCK_PAYMENT_LINK, RANDOM_SEED,
    MAX_ESCALATION_ATTEMPTS, ESCALATION_RECOVERY_MULTIPLIER, ESCALATION_ACTION_MAP,
    CLICK_THROUGH_PROB,
)
from pipeline.paths import EVENTS_PATH, DIAGNOSES_PATH, INTERVENTIONS_PATH


LINK_ACTIONS = ("send_update_link", "send_reminder", "send_checkout_reminder",
                "send_receivables_chaser", "send_final_notice")


def get_payment_link_and_id(action: str, event: dict = None) -> tuple:
    """Returns (short_url, link_id) -- the REAL Razorpay payment link (if
    live credentials are configured and this action uses one) or the mock
    placeholder otherwise. link_id is None whenever no real link was
    created (mock mode, or an action that doesn't use a link at all).

    Kept separate from message formatting so the raw link can be stored
    in audit_trail.payment_link_url for the /track redirect, independent
    of whatever link text the customer actually sees.

    link_id is what live callers (fire_first_intervention, webhook_app's
    escalation scheduler) must register in the link registry
    (pipeline/audit.py: register_child_link) against event["id"] -- that
    registration is what lets a LATER payment_link.expired/cancelled
    webhook for THIS SAME link be recognized as an existing recovery
    attempt instead of a brand-new event, which is what prevents the
    payment_link_1 -> _2 -> _3 -> ... runaway chain."""
    if event is None or action not in LINK_ACTIONS:
        return "", None

    from pipeline.razorpay_client import is_live, create_payment_link
    if not is_live():
        return MOCK_PAYMENT_LINK, None

    payment = event["payload"]["payment"]
    amount_inr = payment["amount"] / 100
    customer_id = (
        payment.get("customer_id")
        or event["payload"].get("subscription", {}).get("customer_id", "customer")
    )
    link = create_payment_link(
        amount_inr=amount_inr,
        customer_name=customer_id,
        reference_id=event["id"],
        description=f"Payment recovery for {event['id']}",
    )
    return link["short_url"], link["id"]


def get_payment_link(action: str, event: dict = None) -> str:
    """Batch-mode convenience wrapper around get_payment_link_and_id() that
    drops the link_id. Fine for batch/synthetic mode, which never receives
    real webhooks back for the links it creates, so there's no chain to
    prevent there. LIVE-mode callers must use get_payment_link_and_id()
    directly and register the id -- see its docstring."""
    url, _link_id = get_payment_link_and_id(action, event)
    return url


def format_message(action: str, link: str) -> str:
    template = ACTION_MESSAGES[action]
    return template.format(link=link or MOCK_PAYMENT_LINK)


def format_message_ai_or_static(action: str, link: str, root_cause: str, amount_inr: float,
                                 escalation_step: int) -> tuple:
    """
    LIVE-mode message builder: tries AI personalization first, falls back
    to the static template on ANY failure. Returns (message, source) where
    source is "ai" or "template" -- surfaced on the dashboard so nothing
    about which path was used is hidden.

    Uses str.replace(), not str.format(), for the AI path deliberately --
    freeform LLM output could contain stray curly braces that would raise
    on .format(); a plain substring replace can't fail that way.
    """
    from pipeline.llm_messaging import generate_message

    ai_text = generate_message(action, root_cause, amount_inr, escalation_step)
    if ai_text:
        return ai_text.replace("{link}", link or MOCK_PAYMENT_LINK), "ai"

    return format_message(action, link), "template"


def build_message(action: str, event: dict = None) -> str:
    """Batch-mode convenience wrapper -- gets the link and formats the
    message in one call, since batch mode has no click-tracking redirect
    to worry about. Always uses the static template (see
    llm_messaging.py's module docstring for why batch mode stays AI-free)."""
    return format_message(action, get_payment_link(action, event))


def intervene(event: dict, diagnosis: dict, rng: random.Random) -> dict:
    """
    Runs the FULL bounded escalation sequence for BATCH/synthetic mode,
    resolving both possible attempts synchronously (no real cool-off wait
    needed here, since this is a one-shot simulation, not a live process).

    This is a real stopping rule, not a documentation-only claim: there is
    no code path in this function that fires more than MAX_ESCALATION_ATTEMPTS
    attempts, ever. escalation_log records exactly what happened, so the
    audit trail can show the FSM path taken for any given event.
    """
    root_cause = diagnosis["root_cause"]
    rule = ROOT_CAUSE_RULES.get(root_cause)

    if not diagnosis["recoverable"] or rule is None:
        return {
            "event_id": event["id"],
            "action": None,
            "message": None,
            "cost_inr": 0.0,
            "outcome": "skipped_not_recoverable",
            "amount_recovered_inr": 0.0,
            "escalation_step": 0,
            "escalation_log": "Diagnosed non-recoverable -- no action taken.",
        }

    amount_inr = event["payload"]["payment"]["amount"] / 100

    # --- Attempt 1 ---
    action_1 = rule["action"]
    cost_1 = rule["cost_inr"]
    link_1 = get_payment_link(action_1, event)
    recovered_1 = rng.random() < rule["recovery_prob"]
    log = f"Attempt 1: {action_1} -> {'recovered' if recovered_1 else 'not recovered'}."

    if recovered_1:
        return {
            "event_id": event["id"],
            "action": action_1,
            "message": format_message(action_1, link_1),
            "cost_inr": cost_1,
            "outcome": "recovered",
            "amount_recovered_inr": amount_inr,
            "escalation_step": 1,
            "escalation_log": log,
            "payment_link_url": link_1,
        }

    # Attempt 1 didn't convert to payment -- but for link-based actions,
    # simulate whether the customer at least clicked (promise-to-pay)
    # before giving up, purely for audit-narrative richness in batch mode.
    # CLICK_THROUGH_PROB is grounded in real WhatsApp/SMS reminder CTR
    # benchmarks -- see config.py. Uses its OWN independent RNG stream
    # (seeded off the event id, not drawn from the shared `rng`) so this
    # narrative-only addition can never shift the core recovery-outcome
    # sequence for this or any later event -- the validated 57.7%
    # headline number stays exactly reproducible regardless of this.
    if action_1 in LINK_ACTIONS:
        click_rng = random.Random(hash(event["id"]) & 0xFFFFFFFF)
        clicked = click_rng.random() < CLICK_THROUGH_PROB
        log += (" Customer clicked the link but did not complete payment (promise-to-pay)."
                if clicked else " No engagement detected.")

    # Escalate only if the cap allows a 2nd attempt --
    # MAX_ESCALATION_ATTEMPTS is checked here, not assumed.
    if MAX_ESCALATION_ATTEMPTS < 2:
        return {
            "event_id": event["id"],
            "action": action_1,
            "message": format_message(action_1, link_1),
            "cost_inr": cost_1,
            "outcome": "not_recovered",
            "amount_recovered_inr": 0.0,
            "escalation_step": 1,
            "escalation_log": log + " STOPPED: max attempts (1) reached.",
            "payment_link_url": link_1,
        }

    # --- Attempt 2 (escalation) ---
    action_2 = ESCALATION_ACTION_MAP.get(action_1, "send_final_notice")
    cost_2 = ROOT_CAUSE_RULES.get(root_cause, {}).get("cost_inr", 0.30)
    link_2 = get_payment_link(action_2, event)
    # Diminishing returns: a customer who ignored attempt 1 is less likely
    # to respond to attempt 2 -- see ESCALATION_RECOVERY_MULTIPLIER in config.py.
    recovery_prob_2 = rule["recovery_prob"] * ESCALATION_RECOVERY_MULTIPLIER
    recovered_2 = rng.random() < recovery_prob_2
    log += f" Cool-off. Attempt 2 (escalated): {action_2} -> {'recovered' if recovered_2 else 'not recovered'}."

    total_cost = cost_1 + cost_2

    if recovered_2:
        return {
            "event_id": event["id"],
            "action": action_2,
            "message": format_message(action_2, link_2),
            "cost_inr": total_cost,
            "outcome": "recovered",
            "amount_recovered_inr": amount_inr,
            "escalation_step": 2,
            "escalation_log": log,
            "payment_link_url": link_2,
        }

    # Both attempts exhausted -- HARD STOP. No 3rd attempt exists anywhere
    # in this codebase for this event; it terminates here permanently.
    log += f" STOPPED: max attempts ({MAX_ESCALATION_ATTEMPTS}) reached."
    return {
        "event_id": event["id"],
        "action": action_2,
        "message": format_message(action_2, link_2),
        "cost_inr": total_cost,
        "outcome": "terminated_unrecovered",
        "amount_recovered_inr": 0.0,
        "escalation_step": 2,
        "escalation_log": log,
        "payment_link_url": link_2,
    }


def fire_first_intervention(event: dict, diagnosis: dict, base_url: str = None) -> dict:
    """
    LIVE-mode counterpart to intervene() -- fires ONLY attempt 1, with no
    coin-flip outcome, since a real outcome can only come from a real
    payment.captured webhook later. Escalation to attempt 2 (and the
    eventual hard stop) is handled separately, after a real cool-off
    period has actually passed -- see webhook_app.py's escalation
    scheduler, which calls pipeline/audit.py's escalate/terminate helpers.

    base_url, when provided (webhook_app.py passes request.host_url),
    makes the customer-facing message point at OUR /track/<event_id>
    redirect instead of the raw Razorpay link directly -- that's the
    promise-to-pay tracker: a real click updates outcome to "promised"
    before redirecting to the actual payment link. The raw link itself is
    still stored in payment_link_url so the redirect has somewhere to go.
    """
    root_cause = diagnosis["root_cause"]
    rule = ROOT_CAUSE_RULES.get(root_cause)

    if not diagnosis["recoverable"] or rule is None:
        return {
            "event_id": event["id"],
            "action": None,
            "message": None,
            "cost_inr": 0.0,
            "outcome": "skipped_not_recoverable",
            "amount_recovered_inr": 0.0,
            "escalation_step": 0,
            "escalation_log": "Diagnosed non-recoverable -- no action taken.",
            "payment_link_url": "",
            "payment_link_id": None,
        }

    action = rule["action"]
    raw_link, link_id = get_payment_link_and_id(action, event)
    customer_facing_link = f"{base_url.rstrip('/')}/track/{event['id']}" if base_url and raw_link else raw_link
    amount_inr = event["payload"]["payment"]["amount"] / 100

    message, message_source = format_message_ai_or_static(
        action, customer_facing_link, root_cause, amount_inr, escalation_step=1
    )

    return {
        "event_id": event["id"],
        "action": action,
        "message": message,
        "message_source": message_source,
        "cost_inr": rule["cost_inr"],
        "outcome": "pending",
        "amount_recovered_inr": 0.0,
        "escalation_step": 1,
        "escalation_log": f"Attempt 1: {action} -> pending (awaiting real payment confirmation). Message: {message_source}.",
        "payment_link_url": raw_link,
        # Registered in the link registry by the caller (webhook_app.py)
        # against event["id"] -- see get_payment_link_and_id()'s docstring
        # for why this is what prevents the runaway recovery-link chain.
        "payment_link_id": link_id,
    }


def _load_jsonl(path: str) -> list:
    items = []
    with open(path) as f:
        for line in f:
            items.append(json.loads(line))
    return items


def run_intervention_stage(events: list = None, diagnoses: list = None, seed: int = RANDOM_SEED,
                            events_path: str = EVENTS_PATH, diagnoses_path: str = DIAGNOSES_PATH,
                            out_path: str = INTERVENTIONS_PATH) -> list:
    if events is None:
        if not os.path.exists(events_path):
            from data.generate_events import generate
            events = generate()
        else:
            events = _load_jsonl(events_path)

    if diagnoses is None:
        if not os.path.exists(diagnoses_path):
            from pipeline.diagnosis import run_diagnosis_stage
            diagnoses = run_diagnosis_stage(events=events, out_path=diagnoses_path)
        else:
            diagnoses = _load_jsonl(diagnoses_path)

    diag_by_id = {d["event_id"]: d for d in diagnoses}
    rng = random.Random(seed)
    results = [intervene(event, diag_by_id[event["id"]], rng) for event in events]

    with open(out_path, "w") as f:
        for r in results:
            f.write(json.dumps(r) + "\n")
    return results


if __name__ == "__main__":
    results = run_intervention_stage()
    fired = [r for r in results if r["action"]]
    recovered = [r for r in results if r["outcome"] == "recovered"]
    total_cost = sum(r["cost_inr"] for r in results)
    gross = sum(r["amount_recovered_inr"] for r in results)

    print(f"Ran interventions on {len(results)} events -> data/interventions.jsonl")
    print(f"Actions fired: {len(fired)}  |  Recovered: {len(recovered)}")
    print(f"Gross recovered: Rs.{gross:,.0f}  |  Total intervention cost: Rs.{total_cost:.2f}")
