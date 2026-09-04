"""
Thin wrapper around the official Razorpay Python SDK (`pip install razorpay`).

Needs two environment variables from your Razorpay TEST MODE dashboard
(Settings -> API Keys -> Generate Test Key):
    RAZORPAY_KEY_ID
    RAZORPAY_KEY_SECRET

If these aren't set, every function here falls back to the mock link
from pipeline/config.py -- so the whole app (including the batch/demo
mode) keeps working with zero setup, and only switches to real API
calls once you've actually configured test credentials.
"""
import os
from pipeline.config import MOCK_PAYMENT_LINK

_KEY_ID = os.environ.get("RAZORPAY_KEY_ID")
_KEY_SECRET = os.environ.get("RAZORPAY_KEY_SECRET")

_client = None
if _KEY_ID and _KEY_SECRET:
    try:
        import razorpay
        _client = razorpay.Client(auth=(_KEY_ID, _KEY_SECRET))
    except ImportError:
        print("WARNING: RAZORPAY_KEY_ID/SECRET are set but the 'razorpay' "
              "package isn't installed. Run: pip install razorpay")
        _client = None


def is_live() -> bool:
    """True once real test-mode credentials are configured and the SDK is installed."""
    return _client is not None


def create_payment_link(amount_inr: float, customer_name: str, reference_id: str, description: str) -> dict:
    """
    Creates a REAL Razorpay test-mode payment link if credentials are
    configured; otherwise returns a mock placeholder.

    Returns {"id": <razorpay payment link id, or None if mocked>,
             "short_url": <the link customers actually click>}.

    reference_id is stamped into the link's `notes` field, which Razorpay
    echoes back on the eventual payment.captured webhook -- that's how
    webhook_app.py matches a recovery back to the original failure event.

    The returned `id` matters just as much as `short_url`: it's what lets
    the caller register THIS link as a child/recovery link of
    `reference_id` (see pipeline/audit.py's link registry). Without that
    registration, a recovery link that itself later expires or gets
    cancelled would look, on its own webhook, exactly like a brand-new
    checkout abandonment -- and the handler would create ANOTHER recovery
    link for it, which could create another, forever
    (payment_link_1 -> _2 -> _3 -> ...). Returning the id is what closes
    that loop.
    """
    if _client is None:
        return {"id": None, "short_url": MOCK_PAYMENT_LINK}

    link = _client.payment_link.create({
        "amount": int(round(amount_inr * 100)),  # Razorpay amounts are in paise
        "currency": "INR",
        "accept_partial": False,
        "description": description,
        "customer": {"name": customer_name or "Customer"},
        "notify": {"sms": False, "email": False},
        "reminder_enable": False,
        "notes": {"reference_id": reference_id},
        "callback_method": "get",
    })
    return {"id": link.get("id"), "short_url": link["short_url"]}
