"""
Runs the pipeline across many random seeds and reports the RANGE of
outcomes, not just one run. This directly answers "how do I trust a
single random result?" -- you don't report one number, you report a
distribution and show it's stable.

Usage:
    python run_sensitivity.py [n_runs] [batch_size]
"""
import random
import statistics
import sys
import os
import csv
import tempfile

from data.generate_events import generate
from pipeline.diagnosis import diagnose
from pipeline.intervention import intervene
from pipeline import audit
from pipeline.metrics import compute_metrics

# Cross-platform temp dir (Windows has no /tmp -- tempfile.gettempdir()
# resolves correctly on Windows, macOS, and Linux alike).
TMP_DIR = tempfile.gettempdir()
EVENTS_TMP_PATH = os.path.join(TMP_DIR, "_sensitivity_events.jsonl")
AUDIT_TMP_PATH = os.path.join(TMP_DIR, "_sensitivity_audit.db")

# This IS kept -- one row per seed's aggregated metrics, so you can chart
# the distribution afterward instead of only seeing the printed summary.
RESULTS_CSV_PATH = os.path.join(os.path.dirname(__file__), "sensitivity_results.csv")


def run_once(seed: int, batch_size: int) -> dict:
    events = generate(batch_size, seed=seed, out_path=EVENTS_TMP_PATH)
    rng = random.Random(seed)
    conn = audit.init_db(db_path=AUDIT_TMP_PATH)
    for event in events:
        diagnosis = diagnose(event)
        result = intervene(event, diagnosis, rng)
        audit.log_event(conn, event, diagnosis, result)
    rows = audit.fetch_all(conn)
    conn.close()
    return compute_metrics(rows)


def main():
    n_runs = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    batch_size = int(sys.argv[2]) if len(sys.argv) > 2 else 80

    all_metrics = []  # one full metrics dict per seed, kept for CSV export

    for seed in range(n_runs):
        m = run_once(seed, batch_size)
        m["seed"] = seed
        all_metrics.append(m)

    recovery_rates = [m["recovery_rate_pct"] for m in all_metrics]
    net_recovered = [m["net_recovered_inr"] for m in all_metrics]

    print(f"\n=== Sensitivity across {n_runs} runs (batch size {batch_size}) ===")
    print(f"Recovery rate:  mean={statistics.mean(recovery_rates):.1f}%  "
          f"stdev={statistics.stdev(recovery_rates):.1f}  "
          f"min={min(recovery_rates):.1f}%  max={max(recovery_rates):.1f}%")
    print(f"Net recovered:  mean=₹{statistics.mean(net_recovered):,.0f}  "
          f"stdev=₹{statistics.stdev(net_recovered):,.0f}  "
          f"min=₹{min(net_recovered):,.0f}  max=₹{max(net_recovered):,.0f}")
    print("\nUse the mean +/- stdev in your pitch instead of a single run's number.")

    # Persist every run's metrics -- this is what "generated data" now
    # means here: not raw events (those are genuinely disposable), but
    # the one number per seed that your pitch deck / chart actually needs.
    fieldnames = list(all_metrics[0].keys())
    with open(RESULTS_CSV_PATH, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(all_metrics)
    print(f"Per-run metrics saved to: {RESULTS_CSV_PATH}")

    for path in (EVENTS_TMP_PATH, AUDIT_TMP_PATH):
        if os.path.exists(path):
            os.remove(path)


if __name__ == "__main__":
    main()
