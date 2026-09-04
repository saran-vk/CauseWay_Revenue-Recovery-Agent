"""
Maps Razorpay's REAL error_code / error_reason strings (verified against
Razorpay's official test-card documentation, github.com/razorpay/
markdown-docs, checked 2026-08-29) to our internal root_cause taxonomy
used throughout pipeline/config.py.

Razorpay's exact BAD_REQUEST_ERROR / GATEWAY_ERROR reasons, and the test
card that triggers each one (any random CVV, any future expiry date;
select "Failure" on the mock bank page after entering the card):

  error_reason                      | test card (Visa)      | maps to
  -----------------------------------|------------------------|------------------
  payment_timed_out                  | 4100 2800 0009 0000    | bank_timeout
  insufficient_fund                  | 4100 2800 0008 0001    | insufficient_funds
  payment_cancelled                  | 4100 2800 0007 0002    | customer_disputed
  card_declined                      | 4100 2800 0006 0003    | issuer_declined
  card_disabled_for_online_payments  | 4100 2800 0003 0006    | issuer_declined
  card_number_invalid                | 4100 2800 0001 0008    | invalid_card
  gateway_technical_error            | 4100 2800 0002 0007    | bank_timeout
  authentication_failed              | 4100 2800 0000 0009    | issuer_declined

Note: Razorpay's official test cards don't include a dedicated "expired
card" scenario -- card_expired stays in our internal taxonomy for real
production decline data (issuers do send this in live mode), it's just
not reachable via test-mode test cards. card_number_invalid is the
closest test-mode equivalent for exercising the "send_update_link" action.

If Razorpay updates their error taxonomy, re-check the source above and
update this map -- everything downstream (diagnose(), intervene()) reads
from ROOT_CAUSE_RULES via this map's output, so a one-place fix here is
all that's ever needed.

Any error_reason NOT in this map falls through to diagnose()'s existing
fail-safe: treated as "unknown" -> non-recoverable. That's intentional --
we'd rather under-act on an unrecognized code than guess.
"""

RAZORPAY_ERROR_REASON_MAP = {
    "payment_timed_out": "bank_timeout",
    "insufficient_fund": "insufficient_funds",
    "payment_cancelled": "customer_disputed",
    "card_declined": "issuer_declined",
    "card_disabled_for_online_payments": "issuer_declined",
    "card_number_invalid": "invalid_card",
    "gateway_technical_error": "bank_timeout",
    "authentication_failed": "issuer_declined",
}


def map_error_reason(raw_reason: str) -> str:
    if not raw_reason:
        return "unknown"
    return RAZORPAY_ERROR_REASON_MAP.get(raw_reason.lower(), "unknown")
