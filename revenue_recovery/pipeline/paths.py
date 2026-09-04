"""
Single source of truth for every file path used across the pipeline.

Every stage imports its input/output paths from here instead of hardcoding
them locally. That's what makes the stages interchangeable: whichever
stage you run -- standalone or via main.py -- they all agree on where
each intermediate file lives.
"""
import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # revenue_recovery/

EVENTS_PATH = os.path.join(BASE_DIR, "data", "events.jsonl")            # Stage 1 output
DIAGNOSES_PATH = os.path.join(BASE_DIR, "data", "diagnoses.jsonl")      # Stage 2 output
INTERVENTIONS_PATH = os.path.join(BASE_DIR, "data", "interventions.jsonl")  # Stage 3 output
AUDIT_DB_PATH = os.path.join(BASE_DIR, "audit_trail.db")                # Stage 4 output
DASHBOARD_HTML_PATH = os.path.join(BASE_DIR, "dashboard", "dashboard.html")  # Stage 5 output
