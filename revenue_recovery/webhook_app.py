"""
Live webhook receiver -- sits in FRONT of the same pipeline used by
main.py. Every event that arrives here (a REAL HTTP POST from Razorpay,
not a line read from a JSONL file) flows through the exact same
diagnose() -> intervene() -> audit.log_event() functions as the batch
mode. Only the event SOURCE changes.

--- SETUP ---
1. pip install flask razorpay python-dotenv
2. Get TEST MODE API keys: Razorpay Dashboard -> Settings -> API Keys
3. Copy .env.example to a new file named .env in this same folder, and
   fill in RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET. This file is loaded
   automatically below -- no $env: commands needed, and it persists
   across terminal sessions (unlike $env:, which resets every time you
   open a new window).
4. Run: python webhook_app.py
5. In a second terminal, expose it publicly (ngrok or cloudflared, no
   account needed for cloudflared): cloudflared tunnel --url http://localhost:5000
     - Copy the https://xxxx.trycloudflare.com URL it prints
     - Razorpay REJECTS localhost URLs, so this step must happen
       BEFORE you create the webhook in the dashboard (step 6)
6. Create a webhook: Dashboard -> Settings -> Webhooks -> Add New Webhook
     - Webhook URL: https://xxxx.trycloudflare.com/webhook/razorpay
     - Active Events: payment.failed, payment.captured, order.paid,
       payment_link.expired, payment_link.cancelled
       (subscription.halted belongs to the separate Subscriptions
       product and won't appear here unless that's enabled on your
       account -- payment.failed alone is enough, since diagnose()
       treats both event types identically)
     - For the receivables workflow, also select invoice.expired if it
       appears -- like Subscriptions, Razorpay's Invoices product may
       need separate activation on your account before this event shows
       up as selectable. If it's not there, the payment-failure and
       checkout-abandonment paths still work fully without it.
     - Copy the webhook secret it gives you, add it to your .env file
       as RAZORPAY_WEBHOOK_SECRET=..., then restart webhook_app.py so
       it picks up the new value
7. Trigger a real TEST MODE payment failure using Razorpay's published
   test card numbers (e.g. a card that simulates a decline), and watch
   it appear at GET /dashboard within seconds.

--- ESCALATION / STOPPING RULES ---
Every event gets AT MOST 2 intervention attempts, ever -- this is
enforced in code (pipeline/config.py's MAX_ESCALATION_ATTEMPTS,
pipeline/audit.py's escalation queries), not just documented. In live
mode: attempt 1 fires immediately when the event arrives. If it's still
"pending" after COOL_OFF_SECONDS_LIVE (90s by default -- short
deliberately, for demo purposes), a background thread automatically
fires a DIFFERENT attempt 2 action. If it's still pending after another
90s cool-off, the event is permanently marked "terminated_unrecovered" --
no 3rd attempt is ever fired. Watch this play out live: trigger a
failure, then just leave webhook_app.py running and refresh /dashboard
every 30s or so -- you'll see the same event's row update in place as it
moves through the FSM.

Note: free tunnel URLs (ngrok or cloudflared) change every time you
restart the tunnel, so keep the same session running for your whole
demo -- if it restarts, update the webhook URL in the Razorpay
dashboard to match.

Without .env values set, signature verification and payment-link
creation both fall back gracefully (see pipeline/razorpay_client.py),
so you can still run and test this locally with simulated requests --
see the "__main__ self-test" block at the bottom of this file for
exactly that.
"""
import os
import sys
import hmac
import hashlib
import random
import sqlite3
import threading
import time as time_module

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Load variables from .env into the real environment BEFORE anything
# else reads os.environ -- this must run before pipeline.razorpay_client
# (imported lazily elsewhere) or the WEBHOOK_SECRET line just below.
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    print("NOTE: python-dotenv isn't installed, so .env won't be read "
          "automatically. Run: pip install python-dotenv "
          "(or set variables manually with $env: instead).")

from flask import Flask, request, jsonify, redirect

from pipeline.diagnosis import diagnose
from pipeline.intervention import (
    fire_first_intervention, format_message, format_message_ai_or_static,
    get_payment_link, get_payment_link_and_id,
)
from pipeline import audit
from pipeline.metrics import compute_metrics, compute_cause_breakdown
from pipeline.paths import AUDIT_DB_PATH
from pipeline.razorpay_mapping import map_error_reason
from pipeline.config import ESCALATION_ACTION_MAP, ROOT_CAUSE_RULES, COOL_OFF_SECONDS_LIVE
from dashboard.build_dashboard import _metric_card, _row, _cause_breakdown_table

app = Flask(__name__)

WEBHOOK_SECRET = os.environ.get("RAZORPAY_WEBHOOK_SECRET")

# Live outcomes are genuinely live -- NOT seeded, unlike the batch pipeline
# which is deliberately reproducible for demo stability.
_rng = random.Random()


def _verify_signature(payload_body: bytes, received_signature: str) -> bool:
    if not WEBHOOK_SECRET:
        # No secret configured -- local/dev mode, verification skipped.
        # Never do this with a real production webhook endpoint.
        return True
    expected = hmac.new(WEBHOOK_SECRET.encode(), payload_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_signature or "")


def _get_conn() -> sqlite3.Connection:
    # Shares the SAME database file the batch pipeline (main.py) writes to,
    # so live events and any previously-generated synthetic batch sit side
    # by side in one audit trail.
    conn = sqlite3.connect(AUDIT_DB_PATH)
    audit.ensure_schema(conn)
    return conn


def _to_internal_event(razorpay_event: dict) -> dict:
    """
    Reshapes Razorpay's real webhook payload into the internal event shape
    diagnose()/intervene() already expect. This is the ONLY place that
    needs to change if Razorpay's real payload differs from what's
    assumed here -- verify field names against a real received payload
    and adjust this function only.
    """
    payload = razorpay_event.get("payload", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    raw_reason = payment_entity.get("error_reason") or payment_entity.get("error_code", "")

    event_id = payment_entity.get("id") or f"evt_live_{int(_rng.random() * 1e9)}"

    return {
        "id": event_id,
        "event": razorpay_event.get("event"),
        "created_at": razorpay_event.get("created_at", 0),
        "created_at_readable": "",
        "payload": {
            "payment": {
                "id": payment_entity.get("id", ""),
                "amount": payment_entity.get("amount", 0),
                "currency": payment_entity.get("currency", "INR"),
                "error_code": payment_entity.get("error_code", ""),
                "error_reason": map_error_reason(raw_reason),
                "raw_error_reason": raw_reason,
                "customer_id": payment_entity.get("customer_id", ""),
            }
        },
    }


def _to_internal_event_from_link(razorpay_event: dict) -> dict:
    """
    Reshapes a payment_link.expired / payment_link.cancelled webhook into
    the internal event shape. Payment links have a totally different
    payload structure from payment events (payload.payment_link.entity,
    not payload.payment.entity) -- and critically, there's no error_reason
    at all, since no payment attempt was ever made. root_cause is set
    directly to "checkout_abandoned" rather than going through
    map_error_reason(), which only handles actual payment decline codes.
    """
    payload = razorpay_event.get("payload", {})
    link_entity = payload.get("payment_link", {}).get("entity", {})
    customer = link_entity.get("customer", {}) or {}

    event_id = link_entity.get("id") or f"evt_live_{int(_rng.random() * 1e9)}"

    return {
        "id": event_id,
        "event": razorpay_event.get("event"),
        "created_at": razorpay_event.get("created_at", 0),
        "created_at_readable": "",
        "payload": {
            "payment": {
                "id": link_entity.get("id", ""),
                "amount": link_entity.get("amount", 0),
                "currency": link_entity.get("currency", "INR"),
                "error_code": "",
                "error_reason": "checkout_abandoned",
                "customer_id": customer.get("contact") or customer.get("email", ""),
            }
        },
    }


def _to_internal_event_from_invoice(razorpay_event: dict) -> dict:
    """
    Reshapes an invoice.expired webhook (a B2B overdue receivable) into
    the internal event shape. Invoices have their own payload structure
    (payload.invoice.entity), and like payment_link events, there's no
    error_reason -- root_cause is set directly to "receivable_overdue".
    Note: Razorpay's Invoices product may need separate activation on
    your account, similar to Subscriptions -- see webhook_app.py's setup
    docstring.
    """
    payload = razorpay_event.get("payload", {})
    invoice_entity = payload.get("invoice", {}).get("entity", {})
    customer = invoice_entity.get("customer_details", {}) or {}

    event_id = invoice_entity.get("id") or f"evt_live_{int(_rng.random() * 1e9)}"

    return {
        "id": event_id,
        "event": razorpay_event.get("event"),
        "created_at": razorpay_event.get("created_at", 0),
        "created_at_readable": "",
        "payload": {
            "payment": {
                "id": invoice_entity.get("id", ""),
                "amount": invoice_entity.get("amount", 0),
                "currency": invoice_entity.get("currency", "INR"),
                "error_code": "",
                "error_reason": "receivable_overdue",
                "customer_id": customer.get("contact") or customer.get("email", ""),
            }
        },
    }


def _handle_failure_event(conn: sqlite3.Connection, razorpay_event: dict, base_url: str = None):
    internal_event = _to_internal_event(razorpay_event)
    if audit.event_exists(conn, internal_event["id"]):
        return  # redelivered webhook for an event we already processed -- no-op, no new payment link
    diagnosis = diagnose(internal_event)
    result = fire_first_intervention(internal_event, diagnosis, base_url=base_url)
    audit.log_event(conn, internal_event, diagnosis, result)


def _handle_abandonment_event(conn: sqlite3.Connection, razorpay_event: dict, base_url: str = None):
    """Same treatment as _handle_failure_event, just starting from a
    payment_link event instead of a payment event -- see
    _to_internal_event_from_link()'s docstring for why these need
    separate mapping functions.

    CHAIN-PREVENTION CHECK (runs first, before anything else): every
    recovery link we ever create (send_update_link, send_reminder,
    send_checkout_reminder, send_receivables_chaser, send_final_notice --
    all of them, for every root cause, since they all go through
    create_payment_link) is itself a Razorpay payment_link. If a customer
    ignores THAT link too, Razorpay fires this exact same
    payment_link.expired / payment_link.cancelled webhook for it -- but
    carrying the RECOVERY link's own id, not the original event's id. Left
    unchecked, that looks indistinguishable from a brand-new checkout
    abandonment, so the code below would create yet another recovery
    link for it. That new link can itself expire too, and so on --
    payment_link_1 -> _2 -> _3 -> ... forever, with no stopping rule.

    The fix: every real recovery link is registered (audit.log_event /
    audit.escalate_live_event both call register_child_link) against the
    event_id it was created for. So the very first thing this handler
    does is check whether the incoming link id is already a KNOWN
    recovery link. If it is, this webhook isn't a new revenue-at-risk
    event at all -- it's just confirmation that an already-tracked
    event's attempt went unpaid. We note that on the existing row and
    stop; no new audit row, no new payment link. What happens next for
    that event (escalate to attempt 2, or terminate) is still decided
    exactly as before, by the existing cool-off-based escalation
    scheduler and the same MAX_ESCALATION_ATTEMPTS cap -- this check only
    stops a duplicate, unbounded chain from starting; it doesn't add or
    skip any of the real attempts.
    """
    link_entity = razorpay_event.get("payload", {}).get("payment_link", {}).get("entity", {})
    incoming_link_id = link_entity.get("id")

    parent_event_id = audit.get_parent_event_id(conn, incoming_link_id)
    if parent_event_id:
        conn.execute(
            "UPDATE audit_trail SET escalation_log = escalation_log || ? WHERE event_id = ?",
            (f" Recovery link ({incoming_link_id}) itself expired/was cancelled, unpaid -- "
             f"no new recovery link created (chain prevented).", parent_event_id),
        )
        conn.commit()
        print(f"[chain-prevention] {incoming_link_id}: recovery link for {parent_event_id} "
              f"expired/cancelled unpaid -- not treated as a new event.")
        return

    internal_event = _to_internal_event_from_link(razorpay_event)
    if audit.event_exists(conn, internal_event["id"]):
        return
    diagnosis = diagnose(internal_event)
    result = fire_first_intervention(internal_event, diagnosis, base_url=base_url)
    audit.log_event(conn, internal_event, diagnosis, result)


def _handle_receivable_event(conn: sqlite3.Connection, razorpay_event: dict, base_url: str = None):
    """Same treatment again, for invoice.expired -- the third loss type
    named explicitly in the track brief (payment failures, checkout
    abandonment, overdue receivables)."""
    internal_event = _to_internal_event_from_invoice(razorpay_event)
    if audit.event_exists(conn, internal_event["id"]):
        return
    diagnosis = diagnose(internal_event)
    result = fire_first_intervention(internal_event, diagnosis, base_url=base_url)
    audit.log_event(conn, internal_event, diagnosis, result)


def _handle_recovery_event(conn: sqlite3.Connection, razorpay_event: dict):
    payload = razorpay_event.get("payload", {})
    payment_entity = payload.get("payment", {}).get("entity", {})
    notes = payment_entity.get("notes", {}) or {}
    reference_id = notes.get("reference_id")  # stamped when we created the payment link
    if not reference_id:
        return  # a capture we don't have a matching pending record for -- ignore, not ours

    amount_inr = payment_entity.get("amount", 0) / 100
    conn.execute(
        "UPDATE audit_trail SET outcome = 'recovered', amount_recovered_inr = ? "
        "WHERE event_id = ? AND outcome = 'pending'",
        (amount_inr, reference_id),
    )
    conn.commit()


@app.route("/webhook/razorpay", methods=["POST"])
def razorpay_webhook():
    raw_body = request.get_data()
    signature = request.headers.get("X-Razorpay-Signature")

    if not _verify_signature(raw_body, signature):
        return jsonify({"error": "invalid signature"}), 400

    event = request.get_json(force=True, silent=True) or {}
    event_type = event.get("event")

    conn = _get_conn()
    try:
        base_url = request.host_url  # e.g. https://xxxx.trycloudflare.com/
        if event_type in ("payment.failed", "subscription.halted"):
            _handle_failure_event(conn, event, base_url=base_url)
        elif event_type in ("payment_link.expired", "payment_link.cancelled"):
            _handle_abandonment_event(conn, event, base_url=base_url)
        elif event_type == "invoice.expired":
            _handle_receivable_event(conn, event, base_url=base_url)
        elif event_type in ("payment.captured", "order.paid"):
            _handle_recovery_event(conn, event)
        # Any other subscribed event type: acknowledged, not acted on --
        # bounded scope, only react to event types we've explicitly handled.
    finally:
        conn.close()

    return jsonify({"status": "ok"}), 200


@app.route("/track/<event_id>")
def track_click(event_id):
    """
    Promise-to-pay tracker. Reminder messages point HERE instead of
    directly at the real Razorpay payment link -- a real click updates
    the event's outcome to "promised" (visible on the dashboard and in
    the audit log) BEFORE redirecting on to the actual payment page.
    This is what lets you distinguish "customer never even looked" from
    "customer engaged but didn't finish" in the audit trail, which is
    the whole point of a promise-to-pay tracker.

    Escalation timing is unaffected by a click -- a promise that never
    converts to payment still escalates/terminates on the same schedule
    as plain silence (see pipeline/audit.py's queries, which treat
    'pending' and 'promised' identically for scheduling purposes).
    """
    conn = _get_conn()
    link = audit.record_click(conn, event_id)
    conn.close()

    if link:
        return redirect(link)
    return jsonify({"error": "unknown or expired tracking link"}), 404


@app.route("/dashboard")
def live_dashboard():
    conn = _get_conn()
    rows = audit.fetch_all(conn)
    conn.close()

    if not rows:
        return ("<body style='font-family:sans-serif;background:#0f1115;color:#eee;padding:32px;'>"
                "<h2>No events yet.</h2><p>Trigger a real test-mode payment failure, checkout "
                "abandonment, or overdue invoice and it'll appear here within seconds.</p></body>")

    metrics = compute_metrics(rows)
    breakdown = compute_cause_breakdown(rows)
    pending_count = sum(1 for r in rows if r["outcome"] == "pending")
    promised_count = sum(1 for r in rows if r["outcome"] == "promised")

    from dashboard.build_dashboard import _SHARED_CSS, _FILTER_SCRIPT, _filter_controls, _flow_funnel_svg

    cards = "".join([
        _metric_card("Total at risk", f"Rs.{metrics['total_at_risk_inr']:,.0f}"),
        _metric_card("Gross recovered", f"Rs.{metrics['gross_recovered_inr']:,.0f}"),
        _metric_card("Net recovered", f"Rs.{metrics['net_recovered_inr']:,.0f}"),
        _metric_card("Recovery rate", f"{metrics['recovery_rate_pct']}%"),
        _metric_card("Pending", str(pending_count)),
        _metric_card("Promised (clicked, unpaid)", str(promised_count)),
        _metric_card("Escalated", str(metrics.get('escalated_count', 0))),
        _metric_card("Terminated (stopped)", str(metrics.get('terminated_unrecovered_count', 0))),
        _metric_card("AI-personalized messages", str(sum(1 for r in rows if (r.get('message_source') or 'template') == 'ai'))),
    ])
    table_rows = "".join(_row(r) for r in rows)

    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><meta http-equiv="refresh" content="5">
<title>Live Revenue Recovery</title>
<style>{_SHARED_CSS}</style>
{_FILTER_SCRIPT}
</head>
<body>
  <h2>Live Revenue Recovery <span style="font-size:13px;color:#9aa0a6;">(auto-refreshes every 5s)</span></h2>
  <div class="cards">{cards}</div>

  <h2>Pipeline Flow</h2>
  {_flow_funnel_svg(metrics)}

  <h2>Per-Cause Breakdown</h2>
  {_cause_breakdown_table(breakdown)}

  <h2>Audit Trail</h2>
  {_filter_controls(rows)}
  <table id="auditTable"><thead><tr><th>Event ID</th><th>Time</th><th>Type</th><th>Amount</th>
  <th>Root Cause</th><th>Action</th><th>Attempt</th><th>Cost</th><th>Outcome</th><th>Recovered</th></tr></thead>
  <tbody>{table_rows}</tbody></table>
</body></html>"""


@app.route("/health")
def health():
    from pipeline.razorpay_client import is_live
    return jsonify({
        "status": "ok",
        "live_mode": is_live(),
        "signature_verification": WEBHOOK_SECRET is not None,
    })


def _run_escalation_scheduler(poll_interval: float = 15.0):
    """
    Background loop enforcing the live-mode escalation FSM in real time.
    After COOL_OFF_SECONDS_LIVE with no recovery confirmation, fires
    attempt 2 (a DIFFERENT action/channel than attempt 1, per
    ESCALATION_ACTION_MAP). After a second cool-off with still no
    confirmation, permanently terminates the event.

    This is the actual enforcement of the "stopping rule" -- there is no
    code path anywhere, in this thread or in intervention.py, that fires
    a 3rd attempt for an event that reaches escalation_step 2.
    """
    while True:
        try:
            conn = _get_conn()

            for row in audit.get_events_needing_escalation(conn, COOL_OFF_SECONDS_LIVE):
                action_1 = row["action"]
                action_2 = ESCALATION_ACTION_MAP.get(action_1, "send_final_notice")
                rule = ROOT_CAUSE_RULES.get(row["root_cause"], {})
                # Minimal fake event, just enough for get_payment_link()'s
                # real-payment-link creation path to work off of.
                fake_event = {
                    "id": row["event_id"],
                    "payload": {"payment": {"amount": int(round(row["amount_inr"] * 100)), "customer_id": ""}},
                }
                raw_link_2, link_id_2 = get_payment_link_and_id(action_2, fake_event)
                public_base_url = os.environ.get("PUBLIC_BASE_URL")  # optional -- see .env.example
                link_2 = f"{public_base_url.rstrip('/')}/track/{row['event_id']}" if public_base_url and raw_link_2 else raw_link_2
                message_2, message_source_2 = format_message_ai_or_static(
                    action_2, link_2, row["root_cause"], row["amount_inr"], escalation_step=2
                )
                added_cost = rule.get("cost_inr", 0.30)
                # new_link_id registers THIS attempt-2 link as a child of
                # row['event_id'] too -- so if it also expires/gets
                # cancelled unpaid, _handle_abandonment_event's
                # chain-prevention check catches it and no attempt 3 is
                # ever spawned. See that function's docstring.
                audit.escalate_live_event(
                    conn, row["event_id"], action_2, message_2, added_cost,
                    f"Cool-off ({COOL_OFF_SECONDS_LIVE:.0f}s). Attempt 2 (escalated): {action_2} -> pending. Message: {message_source_2}.",
                    message_source=message_source_2, new_link_id=link_id_2,
                )
                conn.execute("UPDATE audit_trail SET payment_link_url = ? WHERE event_id = ?",
                             (raw_link_2, row["event_id"]))
                conn.commit()
                print(f"[escalation] {row['event_id']}: escalated {action_1} -> {action_2}")

            for row in audit.get_events_needing_termination(conn, COOL_OFF_SECONDS_LIVE):
                audit.terminate_live_event(conn, row["event_id"])
                print(f"[escalation] {row['event_id']}: STOPPED (max attempts reached, still unrecovered)")

            conn.close()
        except Exception as e:
            print(f"[escalation scheduler] error: {e}")
        time_module.sleep(poll_interval)


if __name__ == "__main__":
    print("Starting live webhook receiver on http://localhost:5000")
    print("  Webhook endpoint : POST /webhook/razorpay")
    print("  Live dashboard   : GET  /dashboard")
    print("  Health check     : GET  /health")
    if not WEBHOOK_SECRET:
        print("\nWARNING: RAZORPAY_WEBHOOK_SECRET not set -- signature verification is DISABLED.")
        print("Fine for local testing, but never expose this endpoint publicly without it.")

    scheduler_thread = threading.Thread(target=_run_escalation_scheduler, daemon=True)
    scheduler_thread.start()
    print(f"Escalation scheduler started (cool-off: {COOL_OFF_SECONDS_LIVE:.0f}s, checks every 15s)")

    app.run(port=5000, debug=True, use_reloader=False)
