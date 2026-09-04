"""
Stage 3b: LLM fallback for the diagnosis engine.

The rule engine (pipeline/diagnosis.py) handles every KNOWN Razorpay
error code correctly and deterministically -- that's the majority case
and doesn't need an LLM. This module only activates for the minority
case: a free-text failure description that doesn't match anything in
ROOT_CAUSE_RULES (e.g. a gateway partner's raw text reason, not a clean
Razorpay error code).

Uses Google's Gemini API (pipeline/gemini_client.py), chosen over
Anthropic specifically because it has a genuine ongoing free tier.

Activation: set GEMINI_API_KEY in your .env file (free at
https://aistudio.google.com/apikey, no credit card). Without it,
classify() always returns None, and diagnose() in diagnosis.py falls
back to its existing fail-safe (non-recoverable) behavior -- exactly the
"if uncertain, default to safest category" rule the track's diagnosis
engine should follow regardless of whether an LLM is involved at all.
"""
from pipeline.config import ROOT_CAUSE_RULES
from pipeline.gemini_client import is_available, generate_text

_VALID_CATEGORIES = [k for k in ROOT_CAUSE_RULES.keys()]  # only ever classify into KNOWN categories


def classify(raw_reason_text: str, timeout_seconds: float = 5.0) -> dict:
    """
    Classifies an ambiguous free-text failure reason into one of the
    known root causes, or returns None if unavailable/uncertain/failed.

    Returns a dict {"root_cause": str, "confidence": float} on a
    confident match, or None -- NEVER raises. Any failure (no API key,
    network error, malformed response, model says "unknown") results in
    None, and the caller falls back to the existing rule-based fail-safe.
    This function is not allowed to be a single point of failure for the
    pipeline -- it can only ever help, never break diagnosis.
    """
    if not is_available() or not raw_reason_text or not raw_reason_text.strip():
        return None

    categories_str = ", ".join(_VALID_CATEGORIES)
    prompt = (
        f"A payment gateway reported this failure reason: \"{raw_reason_text}\"\n\n"
        f"Classify it into EXACTLY ONE of these categories: {categories_str}, or \"unknown\" "
        f"if it genuinely doesn't fit any of them.\n\n"
        f"Respond with ONLY the category name, nothing else -- no punctuation, no explanation."
    )

    text = generate_text(prompt, max_output_tokens=20, timeout_seconds=timeout_seconds)
    if text is None:
        return None

    text = text.strip().lower().strip(".\"'")

    if text not in _VALID_CATEGORIES:
        # Includes the model saying "unknown", or any hallucinated
        # category outside our known set -- both treated the same: no
        # confident classification, caller falls back to non-recoverable.
        return None

    return {"root_cause": text, "confidence": 0.6}  # capped below rule-engine confidence (0.95) -- an LLM guess should never outrank a known code
