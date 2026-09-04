"""
Stage 1: Webhook generator.

Produces a batch of synthetic events shaped like REAL Razorpay webhook
payloads (payment.failed / subscription.halted), so the rest of the
pipeline can be demoed as if it were reading live webhook traffic.

Run directly to (re)generate data/events.jsonl:
    python data/generate_events.py
"""
import json
import random
import sys
import os
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.config import ROOT_CAUSE_RULES, RANDOM_SEED
from pipeline.paths import EVENTS_PATH

EVENT_TYPES = ["payment.failed", "subscription.halted"]
ROOT_CAUSES = list(ROOT_CAUSE_RULES.keys())

# Weights calibrated against Churnkey's "State of Retention 2025" decline-
# reason breakdown: ~50% insufficient-funds (soft), ~25-33% risk-management
# hard flags, ~10-15% card issues. Order matches ROOT_CAUSE_RULES above:
# [insufficient_funds, bank_timeout, card_expired, invalid_card,
#  issuer_declined, mandate_cancelled, customer_disputed, checkout_abandoned,
#  receivable_overdue]
# checkout_abandoned and receivable_overdue only appear via live
# payment_link.expired / invoice.expired events in webhook_app.py, never
# in the synthetic batch generator -- weight 0 for both here.
WEIGHTS = [50, 4, 8, 5, 20, 5, 8, 0, 0]  # must match len(ROOT_CAUSES), sums to 100

AMOUNTS_INR = [199, 299, 499, 999, 1499, 2499, 4999]  # typical subscription/order sizes


def make_event(idx: int, rng: random.Random) -> dict:
    root_cause = rng.choices(ROOT_CAUSES, weights=WEIGHTS, k=1)[0]
    event_type = rng.choice(EVENT_TYPES)
    amount_inr = rng.choice(AMOUNTS_INR)
    customer_id = f"cust_{rng.randrange(10**6):06x}"
    created_at = 1755800000 + idx * 3600  # spaced an hour apart for a believable timeline

    base = {
        "id": f"evt_{idx:04d}",
        "event": event_type,
        "created_at": created_at,
        "created_at_readable": datetime.fromtimestamp(created_at, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "payload": {
            "payment": {
                "id": f"pay_{rng.randrange(10**8):08x}",
                "amount": amount_inr * 100,  # Razorpay amounts are in paise
                "currency": "INR",
                "error_code": "BAD_REQUEST_ERROR" if root_cause != "bank_timeout" else "GATEWAY_ERROR",
                "error_reason": root_cause,
            }
        },
    }

    if event_type == "subscription.halted":
        base["payload"]["subscription"] = {
            "id": f"sub_{rng.randrange(10**8):08x}",
            "customer_id": customer_id,
            "status": "halted",
        }
    else:
        base["payload"]["payment"]["customer_id"] = customer_id

    return base


def generate(n: int = 80, seed: int = RANDOM_SEED, out_path: str = None) -> list:
    rng = random.Random(seed)
    events = [make_event(i, rng) for i in range(1, n + 1)]
    out_path = out_path or EVENTS_PATH
    with open(out_path, "w") as f:
        for e in events:
            f.write(json.dumps(e) + "\n")
    return events


if __name__ == "__main__":
    events = generate(80)
    print(f"Generated {len(events)} synthetic webhook events -> data/events.jsonl")
