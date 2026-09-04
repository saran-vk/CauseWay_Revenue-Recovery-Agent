"""
Thin wrapper around Google's Gemini API -- chosen specifically because it
has a genuine, ongoing free tier (unlike Anthropic's API, which only
offers a one-time trial credit). No credit card required, no expiration.

Get a free key at https://aistudio.google.com/apikey, add it to .env as
GEMINI_API_KEY, and this activates automatically.

Uses the stable `generateContent` REST endpoint via stdlib urllib only --
no `google-genai` package required. Google also offers a newer
"Interactions API" (GA since June 2026), but it's explicitly still
beta-status with schemas that "may change" per Google's own docs
(ai.google.dev/gemini-api/docs/interactions) -- generateContent is
described as "legacy but remains fully supported," which is the more
reliable choice for a project that needs to keep working without
maintenance. Verified against Google's official API reference
(ai.google.dev/api, fetched 2026-09-04) before writing this.

Model: gemini-flash-lite-latest -- an officially documented alias
(ai.google.dev/gemini-api/docs/models#latest) that always points at
Google's current cheapest/fastest Flash-Lite model, so this doesn't
silently go stale as Google ships new versions.

Free tier limits (subject to change -- check https://ai.google.dev/gemini-api/docs/rate-limits):
15 requests/minute, 1,500 requests/day, 1M input tokens/day on
Flash-Lite as of this writing -- vastly more than a hackathon demo needs.
"""
import os
import json
import urllib.request
import urllib.error

_API_KEY = os.environ.get("GEMINI_API_KEY")
_MODEL = "gemini-flash-lite-latest"
_ENDPOINT = f"https://generativelanguage.googleapis.com/v1beta/models/{_MODEL}:generateContent"


def is_available() -> bool:
    return bool(_API_KEY)


def generate_text(prompt: str, max_output_tokens: int = 150, timeout_seconds: float = 5.0) -> str:
    """
    Returns the model's text response, or None on ANY failure (no key,
    network error, malformed response, empty output, rate limit hit,
    safety block) -- NEVER raises. Every caller in this codebase must
    have a non-AI fallback ready regardless; this function is not
    allowed to be a single point of failure for the pipeline.
    """
    if not is_available() or not prompt or not prompt.strip():
        return None

    body = json.dumps({
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"maxOutputTokens": max_output_tokens},
    }).encode()

    req = urllib.request.Request(
        _ENDPOINT,
        data=body,
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": _API_KEY,
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_seconds) as resp:
            data = json.loads(resp.read())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        # Covers network errors AND 4xx/5xx responses raised as
        # HTTPError (a subclass of URLError, includes 429 rate-limit
        # and 404) -- fail safe either way, caller falls back.
        return None

    # Response shape: {"candidates": [{"content": {"parts": [{"text": "..."}]}}]}
    # A missing/empty "candidates" list means a safety block or an
    # otherwise-empty response -- treated as failure, not an exception.
    try:
        text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except (KeyError, IndexError, TypeError):
        return None

    return text or None
