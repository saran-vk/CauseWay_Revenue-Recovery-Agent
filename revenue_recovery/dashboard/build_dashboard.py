"""
Builds a single, self-contained dashboard.html (no CDN, no server needed)
showing batch metrics, a per-cause breakdown, a pipeline flow diagram,
filterable audit trail, all up top and the full per-event audit trail below.

Being fully self-contained matters for demo day: it will render even with
no wifi in the room. Filtering is done with plain vanilla JS (data
attributes + a tiny script) -- no framework, no build step, works by
just opening the HTML file.

Run standalone: auto-runs Stages 1-5 (via run_audit_stage + compute_metrics)
if their outputs don't exist yet, then writes dashboard/dashboard.html.
"""
import sys
import os
import html

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _metric_card(label: str, value: str) -> str:
    return f"""
    <div class="card">
      <div class="card-label">{html.escape(label)}</div>
      <div class="card-value">{html.escape(value)}</div>
    </div>"""


def _row(r: dict) -> str:
    outcome_class = {
        "recovered": "ok",
        "not_recovered": "bad",
        "skipped_not_recoverable": "neutral",
        "pending": "pending",
        "promised": "promised",
        "terminated_unrecovered": "bad",
    }.get(r["outcome"], "")
    escalation_step = r.get("escalation_step", 1)
    escalation_label = "2nd attempt" if escalation_step == 2 else "1st attempt"
    msg_source = r.get("message_source") or "template"
    msg_badge = (f'<span class="badge-ai">AI</span>' if msg_source == "ai"
                 else '<span class="badge-template">Template</span>')
    message_text = r.get("message") or "—"
    return f"""
    <tr class="{outcome_class}" data-cause="{html.escape(r['root_cause'])}" data-outcome="{html.escape(r['outcome'])}">
      <td>{html.escape(r['event_id'])}</td>
      <td>{html.escape(r['created_at_readable'] or '')}</td>
      <td>{html.escape(r['event_type'])}</td>
      <td>₹{r['amount_inr']:.0f}</td>
      <td>{html.escape(r['root_cause'])}</td>
      <td>{html.escape(r['action'] or '—')}</td>
      <td>{escalation_label}</td>
      <td>{msg_badge}<span class="msg-text" title="{html.escape(message_text)}">{html.escape(message_text)}</span></td>
      <td>₹{r['cost_inr']:.2f}</td>
      <td>{html.escape(r['outcome'].replace('_', ' '))}</td>
      <td>₹{r['amount_recovered_inr']:.0f}</td>
    </tr>"""


def _cause_breakdown_table(breakdown: list) -> str:
    """Renders the per-root-cause breakdown -- which causes are most/least
    recoverable, and at what cost. Directly answers Phase 6's requirement,
    and doubles as the fastest way to spot which cause needs attention
    (sorted worst/best by net recovered)."""
    rows = "".join(f"""
    <tr>
      <td>{html.escape(b['root_cause'])}</td>
      <td>{b['count']}</td>
      <td>{b['recovery_rate_pct']}%</td>
      <td>₹{b['gross_recovered_inr']:,.0f}</td>
      <td>₹{b['cost_inr']:.2f}</td>
      <td>₹{b['net_recovered_inr']:,.0f}</td>
    </tr>""" for b in breakdown)
    return f"""
    <table class="breakdown-table">
      <thead><tr><th>Root Cause</th><th>Count</th><th>Recovery Rate</th>
      <th>Gross Recovered</th><th>Cost</th><th>Net Recovered</th></tr></thead>
      <tbody>{rows}</tbody>
    </table>"""


def _flow_funnel_svg(metrics: dict) -> str:
    """A simple funnel showing the pipeline's actual depth: total events
    in, how many were judged recoverable, how many needed escalation to
    a 2nd attempt, and the final recovered/terminated split. This is the
    visual proof that the pipeline does more than 'show reason + percent'."""
    total = metrics["total_events"]
    recoverable = metrics["recoverable_count"]
    escalated = metrics["escalated_count"]
    recovered = metrics["recovered_count"]
    terminated = metrics["terminated_unrecovered_count"]
    skipped = metrics["correctly_skipped_count"]

    def bar(label, value, denom, color):
        pct = (value / denom * 100) if denom else 0
        width = max(pct, 2)
        return f"""
        <div class="funnel-row">
          <div class="funnel-label">{html.escape(label)}</div>
          <div class="funnel-track">
            <div class="funnel-fill" style="width:{width:.1f}%;background:{color};"></div>
          </div>
          <div class="funnel-value">{value}</div>
        </div>"""

    return f"""
    <div class="funnel">
      {bar("Total events", total, total, "#4b5563")}
      {bar("Diagnosed recoverable", recoverable, total, "#3b82f6")}
      {bar("Skipped (non-recoverable)", skipped, total, "#6b7280")}
      {bar("Escalated to 2nd attempt", escalated, total, "#f59e0b")}
      {bar("Recovered", recovered, total, "#22c55e")}
      {bar("Terminated (stopped, unrecovered)", terminated, total, "#ef4444")}
    </div>"""


_FILTER_SCRIPT = """
<script>
function applyFilters() {
  var cause = document.getElementById('causeFilter').value;
  var outcome = document.getElementById('outcomeFilter').value;
  var rows = document.querySelectorAll('#auditTable tbody tr');
  rows.forEach(function(r) {
    var causeMatch = (cause === 'all' || r.dataset.cause === cause);
    var outcomeMatch = (outcome === 'all' || r.dataset.outcome === outcome);
    r.style.display = (causeMatch && outcomeMatch) ? '' : 'none';
  });
}
</script>
"""


def _filter_controls(rows: list) -> str:
    causes = sorted(set(r["root_cause"] for r in rows))
    outcomes = sorted(set(r["outcome"] for r in rows))
    cause_options = "".join(f'<option value="{html.escape(c)}">{html.escape(c)}</option>' for c in causes)
    outcome_options = "".join(f'<option value="{html.escape(o)}">{html.escape(o.replace("_"," "))}</option>' for o in outcomes)
    return f"""
    <div class="filters">
      <label>Root cause:
        <select id="causeFilter" onchange="applyFilters()">
          <option value="all">All</option>
          {cause_options}
        </select>
      </label>
      <label>Outcome:
        <select id="outcomeFilter" onchange="applyFilters()">
          <option value="all">All</option>
          {outcome_options}
        </select>
      </label>
    </div>"""


_SHARED_CSS = """
  body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; background: #0f1115; color: #e6e6e6; margin: 0; padding: 32px; }
  h1, h2 { margin-bottom: 4px; }
  h1 { font-size: 22px; } h2 { font-size: 15px; margin-top: 32px; color: #cfd3d8; }
  .subtitle { color: #9aa0a6; margin-bottom: 24px; font-size: 14px; }
  .cards { display: flex; flex-wrap: wrap; gap: 16px; margin-bottom: 8px; }
  .card { background: #1a1d24; border: 1px solid #2a2e37; border-radius: 10px; padding: 16px 20px; min-width: 150px; }
  .card-label { font-size: 12px; color: #9aa0a6; text-transform: uppercase; letter-spacing: 0.04em; }
  .card-value { font-size: 24px; font-weight: 600; margin-top: 6px; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid #2a2e37; }
  th { color: #9aa0a6; font-weight: 500; text-transform: uppercase; font-size: 11px; letter-spacing: 0.03em; }
  tr.ok { background: rgba(46, 160, 67, 0.08); }
  tr.bad { background: rgba(220, 70, 70, 0.06); }
  tr.neutral { background: rgba(150, 150, 150, 0.06); }
  tr.pending { background: rgba(245, 158, 11, 0.08); }
  tr.promised { background: rgba(245, 158, 11, 0.20); border-left: 2px solid #f59e0b; }
  .badge-ai { background: rgba(139, 92, 246, 0.2); color: #a78bfa; border: 1px solid #7c3aed; border-radius: 6px; padding: 2px 8px; font-size: 11px; font-weight: 600; }
  .badge-template { background: rgba(150, 150, 150, 0.15); color: #9aa0a6; border: 1px solid #3a3e47; border-radius: 6px; padding: 2px 8px; font-size: 11px; }
  .msg-text { display: inline-block; max-width: 260px; margin-left: 8px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle; color: #cfd3d8; cursor: help; }
  .breakdown-table { margin-top: 8px; }
  .filters { display: flex; gap: 20px; margin: 12px 0; font-size: 13px; color: #cfd3d8; }
  .filters select { background: #1a1d24; color: #e6e6e6; border: 1px solid #2a2e37; border-radius: 6px; padding: 4px 8px; margin-left: 6px; }
  .funnel { display: flex; flex-direction: column; gap: 8px; margin-top: 8px; }
  .funnel-row { display: flex; align-items: center; gap: 12px; font-size: 12px; }
  .funnel-label { width: 220px; color: #9aa0a6; flex-shrink: 0; }
  .funnel-track { flex: 1; background: #1a1d24; border-radius: 4px; height: 16px; overflow: hidden; }
  .funnel-fill { height: 100%; border-radius: 4px; }
  .funnel-value { width: 40px; text-align: right; color: #cfd3d8; }
"""


def build_dashboard(rows: list, metrics: dict, out_path: str):
    from pipeline.metrics import compute_cause_breakdown
    breakdown = compute_cause_breakdown(rows)

    cards = "".join([
        _metric_card("Total at risk", f"₹{metrics['total_at_risk_inr']:,.0f}"),
        _metric_card("Gross recovered", f"₹{metrics['gross_recovered_inr']:,.0f}"),
        _metric_card("Total cost", f"₹{metrics['total_cost_inr']:,.2f}"),
        _metric_card("Net recovered", f"₹{metrics['net_recovered_inr']:,.0f}"),
        _metric_card("Recovery rate", f"{metrics['recovery_rate_pct']}%"),
        _metric_card("Escalated to 2nd attempt", str(metrics.get('escalated_count', 0))),
        _metric_card("Terminated (stopped)", str(metrics.get('terminated_unrecovered_count', 0))),
        _metric_card("Correctly skipped", str(metrics['correctly_skipped_count'])),
        _metric_card("AI-personalized messages", str(sum(1 for r in rows if (r.get('message_source') or 'template') == 'ai'))),
    ])

    table_rows = "".join(_row(r) for r in rows)

    html_doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<title>Revenue Recovery Dashboard</title>
<style>{_SHARED_CSS}</style>
{_FILTER_SCRIPT}
</head>
<body>
  <h1>AI Revenue Recovery — Batch Results</h1>
  <div class="subtitle">{metrics['total_events']} synthetic Razorpay-shaped events processed, bounded 2-attempt escalation</div>

  <div class="cards">{cards}</div>

  <h2>Pipeline Flow — where every event ended up</h2>
  {_flow_funnel_svg(metrics)}

  <h2>Per-Cause Breakdown — which causes are most/least recoverable</h2>
  {_cause_breakdown_table(breakdown)}

  <h2>Audit Trail — every event's full journey</h2>
  {_filter_controls(rows)}
  <table id="auditTable">
    <thead>
      <tr>
        <th>Event ID</th><th>Time</th><th>Type</th><th>Amount</th><th>Root Cause</th>
        <th>Action</th><th>Attempt</th><th>Message</th><th>Cost</th><th>Outcome</th><th>Recovered</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>
</body>
</html>"""

    with open(out_path, "w") as f:
        f.write(html_doc)


if __name__ == "__main__":
    from pipeline.audit import run_audit_stage
    from pipeline.metrics import compute_metrics
    from pipeline.paths import DASHBOARD_HTML_PATH

    rows = run_audit_stage()
    metrics = compute_metrics(rows)
    build_dashboard(rows, metrics, DASHBOARD_HTML_PATH)
    print(f"Dashboard written to: {DASHBOARD_HTML_PATH}")
