# CauseWay (AI Revenue Recovery) — Working Prototype

A bounded, auditable agent that detects at-risk revenue across all three
loss types named in the brief — **payment failures**, **checkout
abandonment**, and **overdue B2B receivables** — diagnoses the root
cause, runs a capped 2-attempt escalation sequence, and reports gross
recovered, cost, and net recovered across a batch, with a full audit
trail and honest reporting of what it correctly chose NOT to touch.

## Quick start

No external dependencies for batch mode — pure Python 3 standard library.

```bash
python main.py
```

This will:
1. Generate 80 synthetic, Razorpay-webhook-shaped events (if `data/events.jsonl` doesn't already exist)
2. Diagnose each event's root cause
3. Run the bounded escalation sequence per recoverable event (attempt 1 → cool-off → attempt 2 if still unrecovered → hard stop), tagging cost at each step
4. Log every event's full journey, including its escalation history, to `audit_trail.db` (SQLite)
5. Compute batch metrics (gross recovered, cost, **net recovered**, recovery rate, escalated count, terminated-unrecovered count)
6. Write `dashboard/dashboard.html` — a pipeline flow diagram, a per-cause breakdown, and a filterable audit trail, all in one self-contained file, no server needed

To regenerate a fresh random batch, delete `data/events.jsonl` before re-running.

## Two modes: batch (synthetic) vs live (real Razorpay test-mode webhooks)

**Batch mode** (`python main.py`) resolves both possible attempts
synchronously in one pass — good for reproducible metrics and an
offline demo backup. Validated across 50 random seeds
(`run_sensitivity.py`): mean recovery rate 57.7% (± 5.9 stdev), which
sits exactly where a capped two-touch system should land relative to
published single-touch (47.6%) and best-in-class multi-touch (70-85%)
industry benchmarks — see `pipeline/config.py`'s docstring for full
sourcing.

**Live mode** (`python webhook_app.py`) is a real webhook receiver.
Register it with Razorpay's TEST MODE dashboard and it processes REAL
webhook events as they arrive. Unlike batch mode, live mode never
coin-flips an outcome: attempt 1 fires immediately and logs "pending";
a background scheduler thread enforces the SAME escalation FSM in real
time — after `COOL_OFF_SECONDS_LIVE` (90s by default, short
deliberately for demo purposes) with no recovery confirmation, it fires
a genuinely different attempt 2; after a second cool-off with still no
confirmation, it permanently terminates the event. No code path
anywhere fires a 3rd attempt. See the big docstring at the top of
`webhook_app.py` for full setup steps (API keys, webhook secret,
tunnel). Requires `pip install flask razorpay python-dotenv`.

An event only becomes "recovered" once a genuine `payment.captured`
webhook confirms it — live mode doesn't simulate success. Open
`http://localhost:5000/dashboard` for a live, auto-refreshing view with
the same flow diagram, breakdown, and filters as the batch dashboard.

Both modes write to the SAME `audit_trail.db`, so live events and any
previously-generated batch sit side by side.

## The three loss types, and their live triggers

| Loss type | Live trigger event | root_cause | Notes |
|---|---|---|---|
| Payment failure | `payment.failed` | insufficient_funds, card_expired, etc. | See `pipeline/razorpay_mapping.py` for exact Razorpay error-string mapping |
| Checkout abandonment | `payment_link.expired` | checkout_abandoned | Reliable to trigger on demand — just let a short-expiry Payment Link time out |
| Overdue receivable | `invoice.expired` | receivable_overdue | Requires Razorpay's Invoices product enabled on your account, similar to Subscriptions — see `webhook_app.py` setup docstring |

## Promise-to-pay tracker

Reminder messages don't link directly to Razorpay's payment page — they
link to `/track/<event_id>` on your own app first. A real click there
flips the event's outcome from `"pending"` to `"promised"` (distinct
color on the dashboard, its own audit narrative) and only THEN redirects
to the real payment link. This distinguishes "customer never even
looked" from "customer engaged but didn't finish" — the actual point of
a promise-to-pay tracker. A promise that never converts still escalates
and terminates on the exact same schedule as silence; clicking doesn't
buy extra time, it just gets logged. Batch mode simulates the same
distinction narratively (in `escalation_log`) using `CLICK_THROUGH_PROB`,
calibrated against real WhatsApp reminder CTR benchmarks (45-60%, per
ChatArchitect/AiSensy) — using an independent RNG stream so it never
disturbs the core, already-validated recovery-rate numbers.

## AI-personalized messages and diagnosis fallback

Two features actually put "AI" into "AI Revenue Recovery," using Google's
Gemini API — chosen specifically because it has a genuine, ongoing free
tier (no credit card, no expiration), unlike Anthropic's API which only
offers a one-time trial credit:

- **`pipeline/llm_diagnosis.py`** — classifies free-text failure reasons
  that don't match a known Razorpay error code. Only fires for the
  minority unmapped case; the rule engine handles everything else.
- **`pipeline/llm_messaging.py`** — writes a personalized customer
  message per event (root cause, amount, and a visibly more urgent tone
  on attempt 2 vs attempt 1), live mode only. Batch mode always uses
  static templates, so the validated recovery-rate numbers stay fast,
  offline, and untouched by this.

Safety note: the model never writes the actual payment link — only a
literal `{link}` placeholder, substituted in afterward — and any URL it
writes anyway despite instructions gets stripped by a regex safety net
before a message can ever reach a customer.

## Project structure

```
revenue_recovery/
  data/
    generate_events.py     # Stage 1: synthetic Razorpay-shaped webhook events
  pipeline/
    config.py              # business rules: root cause -> action -> cost -> recovery_prob,
                            # PLUS escalation config (MAX_ESCALATION_ATTEMPTS, cool-off, action map)
    razorpay_mapping.py     # maps Razorpay's real error strings -> internal root_cause
    razorpay_client.py       # real Razorpay Payment Links API wrapper (falls back to mock link)
    diagnosis.py              # Stage 2: root cause + recoverability
    intervention.py            # Stage 3: bounded 2-attempt escalation (batch: intervene(),
                                # live: fire_first_intervention() + webhook_app.py's scheduler)
    audit.py                    # Stage 4: SQLite audit trail + escalation/termination queries
    metrics.py                   # Stage 5: aggregation, INCLUDING per-cause breakdown
  dashboard/
    build_dashboard.py       # renders the static HTML dashboard: flow funnel, cause
                              # breakdown, filterable audit trail -- all self-contained
    dashboard.html            # generated output — open this in a browser
  webhook_app.py                # live webhook receiver + escalation scheduler thread
  main.py                        # orchestrates the full batch pipeline end to end
  run_sensitivity.py              # multi-seed validation (don't report one lucky run)
```

## Why this design

- **Every root cause maps to a specific action, and escalation changes
  the channel, not just repeats it** (`pipeline/config.py`'s
  `ESCALATION_ACTION_MAP`) — attempt 2 is a genuinely different
  intervention than attempt 1, which is what real escalation means.
- **The stopping rule is enforced in code, not documentation**: there is
  no path in `intervention.py`, `audit.py`, or `webhook_app.py` that
  fires a 3rd attempt. `MAX_ESCALATION_ATTEMPTS` is checked, not assumed.
- **Cost is tagged the instant each action fires**, across both
  attempts — so "net recovered = gross recovered − total cost" is
  always accurate even after escalation.
- **Non-recoverable events are explicitly logged as skipped**, not
  silently ignored — your demo moment for "one failure handled gracefully."
- **The dashboard visually proves the pipeline's depth**: the flow
  funnel shows exactly how many events got escalated vs terminated vs
  recovered, and the per-cause table shows which loss types are worth
  the most attention — not just a flat table of rows.
- **The dashboard is a single static HTML file** with no CDN dependency
  — it will render even with no wifi on stage.

## Recovery-rate honesty,

The `recovery_prob` values in `pipeline/config.py` are calibrated
against real published benchmarks (Churnkey, Baremetrics, AgentCollect,
Chaser), and the resulting simulation's aggregate behavior matches those
benchmarks closely across 50 runs. That validates that the simulation
behaves like real-world aggregate data behaves — it does NOT prove this
specific agent's exact wording/timing achieves that rate in production.
Be ready to state that distinction plainly if asked.

## Extending the data

`pipeline/config.py`'s `ROOT_CAUSE_RULES` dict is the single source of
truth for what counts as recoverable, what action to take, what it
costs, and how likely it is to succeed on attempt 1 (attempt 2's
probability is derived automatically via `ESCALATION_RECOVERY_MULTIPLIER`).
Add a new root cause there and both the generator and the pipeline pick
it up automatically.
