"""
Stage 5 (part 1): Metrics.

Pure aggregation over the audit trail rows -- no new logic, just sums.
This is where "net recovered" (suggestion 4) lives.

Run standalone: auto-runs Stages 1-4 (via run_audit_stage) if their
outputs don't exist yet, then prints the full batch metrics.
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def compute_metrics(rows: list) -> dict:
    total_at_risk = sum(r["amount_inr"] for r in rows)
    gross_recovered = sum(r["amount_recovered_inr"] for r in rows)
    total_cost = sum(r["cost_inr"] for r in rows)
    net_recovered = gross_recovered - total_cost

    recoverable_rows = [r for r in rows if r["recoverable"]]
    recovered_rows = [r for r in rows if r["outcome"] == "recovered"]
    skipped_rows = [r for r in rows if r["outcome"] == "skipped_not_recoverable"]
    terminated_rows = [r for r in rows if r["outcome"] == "terminated_unrecovered"]
    escalated_rows = [r for r in rows if r.get("escalation_step", 1) == 2]

    recovery_rate = (
        len(recovered_rows) / len(recoverable_rows) * 100 if recoverable_rows else 0.0
    )

    return {
        "total_events": len(rows),
        "total_at_risk_inr": round(total_at_risk, 2),
        "gross_recovered_inr": round(gross_recovered, 2),
        "total_cost_inr": round(total_cost, 2),
        "net_recovered_inr": round(net_recovered, 2),
        "recovery_rate_pct": round(recovery_rate, 1),
        "recoverable_count": len(recoverable_rows),
        "recovered_count": len(recovered_rows),
        "correctly_skipped_count": len(skipped_rows),
        "escalated_count": len(escalated_rows),
        "terminated_unrecovered_count": len(terminated_rows),
    }


def compute_cause_breakdown(rows: list) -> list:
    """
    Groups the audit trail by root_cause and returns per-cause stats:
    count, recovered count, recovery rate, gross/cost/net. This is the
    direct answer to "which causes are most/least recoverable, and at
    what cost" -- previously derivable by hand from the raw audit trail,
    now a first-class function feeding the dashboard breakdown table.
    Sorted by net recovered, highest first, so the most valuable cause
    to focus on is immediately visible.
    """
    by_cause = {}
    for r in rows:
        cause = r["root_cause"]
        bucket = by_cause.setdefault(cause, {
            "root_cause": cause, "count": 0, "recovered_count": 0,
            "gross_recovered_inr": 0.0, "cost_inr": 0.0,
        })
        bucket["count"] += 1
        bucket["gross_recovered_inr"] += r["amount_recovered_inr"]
        bucket["cost_inr"] += r["cost_inr"]
        if r["outcome"] == "recovered":
            bucket["recovered_count"] += 1

    breakdown = []
    for bucket in by_cause.values():
        bucket["net_recovered_inr"] = round(bucket["gross_recovered_inr"] - bucket["cost_inr"], 2)
        bucket["gross_recovered_inr"] = round(bucket["gross_recovered_inr"], 2)
        bucket["cost_inr"] = round(bucket["cost_inr"], 2)
        bucket["recovery_rate_pct"] = round(
            bucket["recovered_count"] / bucket["count"] * 100 if bucket["count"] else 0.0, 1
        )
        breakdown.append(bucket)

    breakdown.sort(key=lambda b: b["net_recovered_inr"], reverse=True)
    return breakdown


if __name__ == "__main__":
    from pipeline.audit import run_audit_stage
    rows = run_audit_stage()
    metrics = compute_metrics(rows)
    print("=== Batch Metrics ===")
    for k, v in metrics.items():
        print(f"{k:28s}: {v}")
