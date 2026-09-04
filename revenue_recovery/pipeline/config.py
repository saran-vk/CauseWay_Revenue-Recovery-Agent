"""
Central config for the revenue recovery pipeline.
Keeping every rule in one file makes it easy to explain to judges
and easy to tweak without touching pipeline logic.

CALIBRATION SOURCES (cite these in your pitch deck):
- Industry-wide median SINGLE-ATTEMPT failed-payment recovery rate is
  ~47.6% (Baremetrics / Slicker 2025-26 benchmarks) -- this is what a
  ONE-touch system would land near, and is what per-cause recovery_prob
  values below are calibrated against.
- Optimized multi-touch retry strategies recover 45-70% of failures,
  best-in-class SaaS businesses reach 70-85% (Kaplan Group 2025 SaaS
  payment stats) -- multi-touch sequences genuinely outperform single-touch.
- With this codebase's bounded 2-attempt escalation FSM (see
  MAX_ESCALATION_ATTEMPTS below), 50-run sensitivity testing
  (run_sensitivity.py) shows a mean recovery rate of 57.7% (+/- 5.9
  stdev) -- sitting exactly where a capped, non-aggressive two-touch
  system should land: above the 47.6% single-touch median, below the
  70-85% best-in-class ceiling (which typically uses more than 2 touches
  and more channels than we do). This progression -- single-touch
  baseline -> our bounded two-touch result -> best-in-class ceiling --
  is a coherent, explainable story, not three unrelated numbers.
- Decline-reason population breakdown: ~50% insufficient-funds soft
  declines, ~25-33% risk-management hard flags, ~10-15% card issues
  (expiry/loss/theft) -- Churnkey "State of Retention 2025".

These numbers set the WEIGHTS in generate_events.py (how common each
root cause is) and the recovery_prob below (how often each recovers on
ATTEMPT 1 specifically -- attempt 2's probability is derived from this
via ESCALATION_RECOVERY_MULTIPLIER, not set independently).
"""

# Root cause -> (recoverable?, action_name, cost_in_inr, base_recovery_probability)
# cost_in_inr is a rough estimate of what firing this action costs you
# (LLM tokens for message generation + WhatsApp/SMS send cost).
#
# recovery_prob rationale:
#   - insufficient_funds / bank_timeout are "soft" / temporary failures ->
#     retry-friendly, higher recovery odds (per Churnkey's soft-decline bucket).
#   - card_expired / invalid_card need active customer action (update card) ->
#     moderate recovery, in line with Stripe's pre-dunning card-update uplift data.
#   - issuer_declined / mandate_cancelled are risk-management "hard" flags ->
#     lower recovery odds, per Churnkey's hard-flag bucket.
#   - customer_disputed is a genuine dispute, not a technical failure ->
#     explicitly non-recoverable, should never be actioned.
ROOT_CAUSE_RULES = {
    "insufficient_funds": {"recoverable": True,  "action": "retry_later",      "cost_inr": 0.10, "recovery_prob": 0.55},
    "bank_timeout":       {"recoverable": True,  "action": "retry_now",        "cost_inr": 0.10, "recovery_prob": 0.75},
    "card_expired":       {"recoverable": True,  "action": "send_update_link", "cost_inr": 0.35, "recovery_prob": 0.50},
    "invalid_card":       {"recoverable": True,  "action": "send_update_link", "cost_inr": 0.35, "recovery_prob": 0.45},
    "issuer_declined":    {"recoverable": True,  "action": "send_reminder",    "cost_inr": 0.30, "recovery_prob": 0.25},
    "mandate_cancelled":  {"recoverable": True,  "action": "send_reminder",    "cost_inr": 0.30, "recovery_prob": 0.30},
    "customer_disputed":  {"recoverable": False, "action": None,               "cost_inr": 0.0,  "recovery_prob": 0.0},
    # Checkout/payment-link abandonment -- NOT a payment failure, a timeout
    # with no attempt made at all. recovery_prob calibrated against
    # published reminder-recovery benchmarks: Klaviyo's baseline generic
    # cart-recovery email converts ~3.33%, while well-targeted/personalized
    # sequences (which a payment-link-specific reminder is, since the
    # customer already showed real purchase intent) report 5-18%, and
    # top AI-driven tools report up to 38% (multiple 2026 industry
    # benchmark aggregators). 25% is a defensible mid-range estimate for
    # a personalized, single-touch reminder -- not a generic email blast.
    "checkout_abandoned": {"recoverable": True,  "action": "send_checkout_reminder", "cost_inr": 0.30, "recovery_prob": 0.25},
    # B2B overdue receivable (Razorpay invoice.expired). recovery_prob
    # calibrated against a spread of published AR benchmarks: consistent
    # follow-up makes an invoice 76% more likely to be paid within a week
    # (Chaser 2026 AR report), pre-due reminders lift on-time payment ~18%
    # (Fusion CX 2026), while un-chased invoices average 20-43% overdue
    # rates depending on region/segment (Atradius, AgentCollect 2026). 45%
    # is a conservative mid-estimate for an IMMEDIATE first-touch reminder
    # fired the moment an invoice crosses its due date, before the account
    # has had time to age -- real B2B benchmarks vary hugely by industry
    # and invoice size, so treat this as directionally reasonable, not precise.
    "receivable_overdue": {"recoverable": True,  "action": "send_receivables_chaser", "cost_inr": 0.40, "recovery_prob": 0.45},
}

# --- Escalation / stopping-rule config ---
# Every recoverable event gets AT MOST this many intervention attempts,
# ever. This cap is enforced in code (see intervention.py / webhook_app.py),
# not just documented here -- there is no code path that fires a 3rd attempt.
MAX_ESCALATION_ATTEMPTS = 2

# A customer who didn't respond to attempt 1 is less likely to respond to
# attempt 2 -- diminishing returns is a well-established pattern in dunning
# sequences generally (later reminders in a sequence consistently convert
# at a fraction of the first). 0.4 is a directional estimate, not a
# per-channel measured figure.
ESCALATION_RECOVERY_MULTIPLIER = 0.4

# What attempt 2 looks like, keyed by attempt 1's action. Deliberately a
# DIFFERENT action/channel than attempt 1, not a repeat of the same thing --
# real escalation should change the approach, not just retry it.
ESCALATION_ACTION_MAP = {
    "retry_later": "send_reminder",
    "retry_now": "send_reminder",
    "send_update_link": "send_reminder",
    "send_reminder": "send_final_notice",
    "send_checkout_reminder": "send_final_notice",
    "send_receivables_chaser": "send_final_notice",
}

# Live-mode cool-off window between attempt 1 and attempt 2 (and between
# attempt 2 and automatic termination), in seconds. Kept short here
# specifically so it's demo-friendly -- a real deployment would use hours
# or days, not seconds.
COOL_OFF_SECONDS_LIVE = 90

# Promise-to-pay tracker: probability a customer clicks a reminder link
# without immediately paying (used for batch-mode narrative simulation
# in intervention.py; live mode detects this for real via the
# /track/<event_id> redirect in webhook_app.py). Calibrated against
# WhatsApp Business API click-through-rate benchmarks -- ChatArchitect
# and AiSensy both report 45-60% CTR for WhatsApp reminder campaigns
# (vs 2-6% for email), consistent with our messages being WhatsApp/
# Hinglish-styled. 0.5 sits at the midpoint of that range.
CLICK_THROUGH_PROB = 0.50

# Human-readable message templates per action (Hinglish nudge included per your track notes)
ACTION_MESSAGES = {
    "send_update_link": "Aapka card expire/invalid hai — payment update karein: {link}",
    "retry_later": "We'll automatically retry your payment during your usual pay window.",
    "retry_now": "Retrying your payment now, bank issue looked temporary.",
    "send_reminder": "Please re-authorize your payment mandate to continue your subscription: {link}",
    "send_checkout_reminder": "Aapne payment complete nahi kiya — abhi complete karein: {link}",
    "send_receivables_chaser": "Your invoice is now overdue. Please settle it here: {link}",
    "send_final_notice": "Final reminder — please complete this payment as soon as possible: {link}",
}

# Placeholder for a real Razorpay test-mode payment link, swapped in at the
# "add mock Razorpay payment links" phase without touching any other code.
MOCK_PAYMENT_LINK = "https://rzp.io/i/PLACEHOLDER"

RANDOM_SEED = 42  # fixed seed so your metrics are reproducible across demo runs
