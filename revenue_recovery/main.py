"""
Runs the full Revenue Recovery pipeline end to end by calling each
stage's own run_*_stage() function in order:

  webhook events -> diagnosis -> intervention -> audit trail -> metrics -> dashboard

This is functionally identical to running each stage script one at a
time in VS Code (each stage auto-chains to the one before it if its
input file is missing) -- main.py just makes the full sequence explicit
and produces the dashboard at the end.

Usage:
    python main.py
"""
from pipeline.audit import run_audit_stage
from pipeline.metrics import compute_metrics
from pipeline.paths import DASHBOARD_HTML_PATH


def main():
    rows = run_audit_stage()
    metrics = compute_metrics(rows)

    print("\n=== Revenue Recovery Batch Results ===")
    for k, v in metrics.items():
        print(f"{k:28s}: {v}")

    from dashboard.build_dashboard import build_dashboard
    build_dashboard(rows, metrics, DASHBOARD_HTML_PATH)
    print(f"\nDashboard written to: {DASHBOARD_HTML_PATH}")
    print("Open that file in a browser to see the full audit trail + metrics.")


if __name__ == "__main__":
    main()
