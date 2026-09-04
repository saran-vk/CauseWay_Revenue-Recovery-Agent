"""
Stage 3c: AI-personalized recovery messages -- LIVE mode only.

This is where "AI" actually touches the core intervention, not just a
rare diagnosis edge case: instead of a fixed template, the customer
message is generated per event, tailored to root cause, amount, and
escalation step (attempt 2 gets a visibly more urgent tone than
attempt 1). Batch/synthetic mode deliberately does NOT use this -- it
keeps static templates so the validated 57.7% recovery number stays
fast, offline, and reproducible with zero external dependency. AI
personalization only applies to the real live pipeline.

Same defensive pattern as llm_diagnosis.py: uses the shared Gemini
client (pipeline/gemini_client.py), and every possible failure (no API
key, network error, malformed response, empty output) falls back to the
existing static ACTION_MESSAGES template -- this module can only ever
improve the message, never break the pipeline if it's unavailable.

SAFETY RULE: the model is NEVER shown the real payment link and is
NEVER allowed to write a URL into its output. It only writes the
surrounding text; the {link} placeholder is substituted in
programmatically afterward, identically to the static-template path.
This prevents a hallucinated or malformed URL from ever reaching a
customer.
"""
import re

from pipeline.config import ACTION_MESSAGES
from pipeline.gemini_client import is_available, generate_text


def _tone_for_step(escalation_step: int) -> str:
    return "friendly and low-pressure -- this is the first reminder" if escalation_step <= 1 \
        else "a bit more urgent, but still polite -- this is a second and final reminder before we stop following up"


def generate_message(action: str, root_cause: str, amount_inr: float, escalation_step: int,
                      timeout_seconds: float = 5.0) -> str:
    """
    Returns a personalized message with a literal "{link}" placeholder
    still inside it (caller substitutes the real link afterward, same as
    the static-template path) -- or None on ANY failure, in which case
    the caller must fall back to ACTION_MESSAGES[action].
    """
    if not is_available():
        return None

    style = ("Hinglish (mix of Hindi and English, casual and warm)" if action in
             ("send_update_link", "send_checkout_reminder")
             else "professional, formal English suitable for a B2B invoice reminder" if action == "send_receivables_chaser"
             else "brief, clear English")

    prompt = (
        f"Write a single short payment-recovery message (1-2 sentences max) for a customer "
        f"whose payment failed. Context: reason='{root_cause}', amount=Rs.{amount_inr:.0f}, "
        f"tone should be {_tone_for_step(escalation_step)}. Style: {style}.\n\n"
        f"CRITICAL: include the exact literal placeholder text {{link}} somewhere in your message "
        f"where a payment link should go -- do NOT invent, write out, or guess any actual URL "
        f"yourself. Respond with ONLY the message text, nothing else -- no preamble, no quotes."
    )

    text = generate_text(prompt, max_output_tokens=120, timeout_seconds=timeout_seconds)

    if not text or "{link}" not in text:
        # No response, or model didn't include the placeholder --
        # treat as a failure rather than send a message with no payment link.
        return None

    # Extra safety net: strip out anything that looks like a URL the model
    # might have written despite the instruction not to, so a hallucinated
    # link can never reach a customer even if the prompt was ignored.
    text = re.sub(r"https?://\S+", "{link}", text)

    return text
