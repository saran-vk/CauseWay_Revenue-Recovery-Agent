"""
Stage 4: Audit trail.

One row per event, capturing its full journey: what happened, what we
diagnosed, what we did about it, what it cost, and what came of it.
Every other metric in the dashboard is just an aggregation over this table.

Uses sqlite3 (Python standard library) so there's zero setup required.

Run standalone: auto-runs Stages 1-3 for any input file that doesn't
exist yet, logs everything to audit_trail.db, and prints a sample row.
"""
import sys
import os
import sqlite3
import json
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from pipeline.paths import EVENTS_PATH, DIAGNOSES_PATH, INTERVENTIONS_PATH, AUDIT_DB_PATH

DB_PATH = AUDIT_DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_trail (
    event_id TEXT PRIMARY KEY,
    event_type TEXT,
    amount_inr REAL,
    root_cause TEXT,
    recoverable INTEGER,
    action TEXT,
    message TEXT,
    cost_inr REAL,
    outcome TEXT,
    amount_recovered_inr REAL,
    created_at INTEGER,
    created_at_readable TEXT,
    escalation_step INTEGER DEFAULT 1,
    escalation_log TEXT,
    last_action_epoch REAL,
    payment_link_url TEXT,
    clicked_at_epoch REAL,
    diagnosis_source TEXT,
    message_source TEXT DEFAULT 'template',
    payment_link_id TEXT
);
"""

# Link registry: maps a Razorpay payment-link ID that WE created (as a
# recovery action) back to the event_id it was created for. This is what
# stops the runaway payment_link_1 -> _2 -> _3 -> ... chain: a recovery
# link's own payment_link.expired/payment_link.cancelled webhook carries
# THAT link's id, not the parent event's id, so without this table it
# looks indistinguishable from a brand-new checkout abandonment and the
# handler would create yet another recovery link for it, forever. With
# this table, webhook_app.py checks the incoming link id here first: a
# hit means "this is an existing recovery attempt concluding, not a new
# at-risk event" and no new link is created.
LINK_REGISTRY_SCHEMA = """
CREATE TABLE IF NOT EXISTS link_registry (
    link_id TEXT PRIMARY KEY,
    parent_event_id TEXT NOT NULL,
    created_at REAL
);
"""


def ensure_schema(conn):
    """Creates both tables if missing, AND migrates an existing
    audit_trail.db from before the payment_link_id column existed (e.g.
    a DB from a previous webhook_app.py run, which persists across
    restarts unlike the batch pipeline's init_db()). ALTER TABLE ADD
    COLUMN is wrapped in try/except since SQLite has no
    'ADD COLUMN IF NOT EXISTS' -- the OperationalError it raises when the
    column already exists is the expected, ignorable case."""
    conn.execute(SCHEMA)
    conn.execute(LINK_REGISTRY_SCHEMA)
    try:
        conn.execute("ALTER TABLE audit_trail ADD COLUMN payment_link_id TEXT")
    except sqlite3.OperationalError:
        pass  # column already exists
    conn.commit()


def init_db(db_path: str = DB_PATH):
    if os.path.exists(db_path):
        os.remove(db_path)  # fresh run each time -- keeps demo runs reproducible
    conn = sqlite3.connect(db_path)
    ensure_schema(conn)
    return conn


def register_child_link(conn, link_id, parent_event_id: str):
    """Records that `link_id` (a real Razorpay payment link WE just
    created) is a recovery action taken FOR `parent_event_id`. No-op if
    link_id is None (mock mode, or an action that never touched a real
    link) -- there's nothing to register. Called right after any real
    create_payment_link() call, both for attempt 1 (webhook_app.py's
    _handle_*_event functions) and attempt 2 (the escalation scheduler)."""
    if not link_id:
        return
    conn.execute(
        "INSERT OR IGNORE INTO link_registry (link_id, parent_event_id, created_at) VALUES (?, ?, ?)",
        (link_id, parent_event_id, time.time()),
    )
    conn.commit()


def get_parent_event_id(conn, link_id: str):
    """Returns the event_id this link was created as a recovery action
    for, or None if link_id isn't one of ours (i.e. it's a genuine
    customer-facing checkout link, not something we generated). This is
    the chain-prevention check: called on every incoming
    payment_link.expired/payment_link.cancelled webhook BEFORE deciding
    whether to treat it as a new event."""
    if not link_id:
        return None
    cur = conn.execute("SELECT parent_event_id FROM link_registry WHERE link_id = ?", (link_id,))
    row = cur.fetchone()
    return row[0] if row else None


def log_event(conn, event: dict, diagnosis: dict, result: dict):
    payment = event["payload"]["payment"]
    conn.execute(
        """INSERT OR IGNORE INTO audit_trail
           (event_id, event_type, amount_inr, root_cause, recoverable, action,
            message, cost_inr, outcome, amount_recovered_inr, created_at, created_at_readable,
            escalation_step, escalation_log, last_action_epoch, payment_link_url, clicked_at_epoch,
            diagnosis_source, message_source, payment_link_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            event["id"],
            event["event"],
            payment["amount"] / 100,
            diagnosis["root_cause"],
            int(diagnosis["recoverable"]),
            result["action"],
            result["message"],
            result["cost_inr"],
            result["outcome"],
            result["amount_recovered_inr"],
            event["created_at"],
            event.get("created_at_readable", ""),
            result.get("escalation_step", 1),
            result.get("escalation_log", ""),
            time.time(),
            result.get("payment_link_url", ""),
            None,
            diagnosis.get("diagnosis_source", "rule"),
            result.get("message_source", "template"),
            result.get("payment_link_id"),
        ),
    )
    conn.commit()
    # Register this event's own recovery link (if a real one was created)
    # as a child of itself, so that IF this exact link later expires or
    # gets cancelled unpaid, that webhook is recognized as this same
    # event concluding attempt 1 -- not a brand-new one. See
    # register_child_link()'s docstring.
    register_child_link(conn, result.get("payment_link_id"), event["id"])


def record_click(conn, event_id: str):
    """
    Promise-to-pay tracker: called when a customer actually clicks the
    reminder link (see webhook_app.py's /track/<event_id> route). Only
    transitions "pending" -> "promised" -- if the event already recovered,
    escalated, or terminated, a late click doesn't change its state.
    Returns the row's payment_link_url so the caller can redirect there,
    or None if the event_id doesn't exist.
    """
    cur = conn.execute("SELECT payment_link_url, outcome FROM audit_trail WHERE event_id = ?", (event_id,))
    row = cur.fetchone()
    if row is None:
        return None
    link, outcome = row
    if outcome == "pending":
        conn.execute(
            "UPDATE audit_trail SET outcome = 'promised', clicked_at_epoch = ?, "
            "escalation_log = escalation_log || ' Customer clicked the link (promise-to-pay) at step ' || escalation_step || '.' "
            "WHERE event_id = ?",
            (time.time(), event_id),
        )
        conn.commit()
    return link


def get_events_needing_escalation(conn, cool_off_seconds: float) -> list:
    """Rows still pending OR promised (clicked but didn't pay) after
    attempt 1, past their cool-off window -- candidates for the LIVE
    scheduler to fire attempt 2 on."""
    cutoff = time.time() - cool_off_seconds
    cur = conn.execute(
        "SELECT * FROM audit_trail WHERE outcome IN ('pending', 'promised') AND escalation_step = 1 "
        "AND recoverable = 1 AND last_action_epoch < ?",
        (cutoff,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def escalate_live_event(conn, event_id: str, new_action: str, new_message: str, added_cost: float,
                         log_append: str, message_source: str = "template", new_link_id: str = None):
    """Applies attempt 2 to a live event that's still pending/promised after cool-off.
    If attempt 2 created a real payment link (new_link_id), it's registered
    as a child of event_id too -- so if THAT link also expires/gets
    cancelled unpaid, it's recognized as this same event's attempt 2
    concluding, not a fresh event. Registration happens regardless of the
    UPDATE's WHERE clause matching, since the link was already created
    against the real Razorpay API either way -- it must be tracked."""
    conn.execute(
        "UPDATE audit_trail SET action = ?, message = ?, message_source = ?, cost_inr = cost_inr + ?, "
        "escalation_step = 2, escalation_log = escalation_log || ' ' || ?, last_action_epoch = ?, "
        "outcome = 'pending', payment_link_id = ? "
        "WHERE event_id = ? AND outcome IN ('pending', 'promised') AND escalation_step = 1",
        (new_action, new_message, message_source, added_cost, log_append, time.time(), new_link_id, event_id),
    )
    conn.commit()
    register_child_link(conn, new_link_id, event_id)


def get_events_needing_termination(conn, cool_off_seconds: float) -> list:
    """Rows still pending OR promised after attempt 2, past a second
    cool-off window -- candidates for the LIVE scheduler to permanently
    stop on. This is the hard cap enforced in real time, not just
    documented."""
    cutoff = time.time() - cool_off_seconds
    cur = conn.execute(
        "SELECT * FROM audit_trail WHERE outcome IN ('pending', 'promised') AND escalation_step = 2 "
        "AND last_action_epoch < ?",
        (cutoff,),
    )
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def terminate_live_event(conn, event_id: str):
    """Permanently stops an event that exhausted both attempts -- no 3rd
    attempt is ever fired for it, by anything, from this point on."""
    conn.execute(
        "UPDATE audit_trail SET outcome = 'terminated_unrecovered', "
        "escalation_log = escalation_log || ' STOPPED: max attempts (2) reached.' "
        "WHERE event_id = ? AND outcome IN ('pending', 'promised')",
        (event_id,),
    )
    conn.commit()


def event_exists(conn: sqlite3.Connection, event_id: str) -> bool:
    """Cheap existence check with no side effects -- callers use this
    BEFORE calling fire_first_intervention()/create_payment_link(), so a
    redelivered webhook (Razorpay's own docs confirm this can happen)
    never triggers a second real payment link for an event we've already
    processed. INSERT OR IGNORE in log_event() only prevented a duplicate
    DB ROW; it ran too late to prevent the duplicate API call that
    already happened before it."""
    cur = conn.execute("SELECT 1 FROM audit_trail WHERE event_id = ? LIMIT 1", (event_id,))
    return cur.fetchone() is not None


def fetch_all(conn):
    cur = conn.execute("SELECT * FROM audit_trail ORDER BY created_at")
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _load_jsonl(path: str) -> list:
    items = []
    with open(path) as f:
        for line in f:
            items.append(json.loads(line))
    return items


def run_audit_stage(events: list = None, diagnoses: list = None, interventions: list = None,
                     events_path: str = EVENTS_PATH, diagnoses_path: str = DIAGNOSES_PATH,
                     interventions_path: str = INTERVENTIONS_PATH, db_path: str = DB_PATH) -> list:
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

    if interventions is None:
        if not os.path.exists(interventions_path):
            from pipeline.intervention import run_intervention_stage
            interventions = run_intervention_stage(events=events, diagnoses=diagnoses, out_path=interventions_path)
        else:
            interventions = _load_jsonl(interventions_path)

    diag_by_id = {d["event_id"]: d for d in diagnoses}
    result_by_id = {r["event_id"]: r for r in interventions}

    conn = init_db(db_path)
    for event in events:
        log_event(conn, event, diag_by_id[event["id"]], result_by_id[event["id"]])
    rows = fetch_all(conn)
    conn.close()
    return rows


if __name__ == "__main__":
    rows = run_audit_stage()
    print(f"Logged {len(rows)} events to audit trail -> {DB_PATH}")
    print("\nSample row:")
    for k, v in rows[0].items():
        print(f"  {k:24s}: {v}")
