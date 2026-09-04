"""
Diagnosis accuracy report for the LLM fallback specifically.

The rule engine doesn't need this kind of testing -- it's a deterministic
lookup, always 100% "correct" relative to its own table by construction.
This script tests the one part that genuinely needs empirical validation:
does the LLM fallback correctly classify AMBIGUOUS FREE-TEXT reasons the
rule table doesn't recognize?

Requires GEMINI_API_KEY set (in .env or the environment) -- without
it, every call returns None and this reports 0/N by design, which is
correct behavior to show (the fallback safely does nothing rather than
guessing), not a bug in this test.

Usage:
    python pipeline/test_llm_diagnosis.py
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from pipeline.llm_diagnosis import classify, is_available

# Each entry: (raw free-text reason a gateway might actually send,
# the correct root_cause a human reviewer would assign).
LABELED_SAMPLES = [
    ("card declined due to lack of available balance", "insufficient_funds"),
    ("the customer's account did not have sufficient funds", "insufficient_funds"),
    ("the card on file is no longer valid", "invalid_card"),
    ("card number failed Luhn validation", "invalid_card"),
    ("customer's bank could not be reached in time", "bank_timeout"),
    ("gateway request timed out waiting for issuer response", "bank_timeout"),
    ("payment blocked by issuing bank's risk system", "issuer_declined"),
    ("issuer declined the transaction for risk reasons", "issuer_declined"),
    ("customer said this charge was not authorized by them", "customer_disputed"),
    ("cardholder is disputing this transaction", "customer_disputed"),
    ("recurring payment authorization was revoked by the customer", "mandate_cancelled"),
    ("customer cancelled their standing instruction", "mandate_cancelled"),
    ("the weather in Mumbai today", "unknown"),  # deliberately nonsensical -- should NOT match anything
]


def main():
    if not is_available():
        print("GEMINI_API_KEY not set -- LLM fallback is disabled.")
        print("Every classification below will correctly return 'no match' (fail-safe behavior).")
        print("Get a free key at https://aistudio.google.com/apikey and set GEMINI_API_KEY in your .env file to actually run this accuracy test.\n")

    correct = 0
    total = len(LABELED_SAMPLES)

    for raw_text, expected in LABELED_SAMPLES:
        result = classify(raw_text)
        predicted = result["root_cause"] if result else "unknown"
        is_correct = (predicted == expected)
        correct += is_correct
        status = "PASS" if is_correct else "FAIL"
        print(f"[{status}] \"{raw_text[:50]}\"")
        print(f"       expected={expected}  predicted={predicted}")

    print(f"\n=== Accuracy: {correct}/{total} ({correct/total*100:.0f}%) ===")


if __name__ == "__main__":
    main()
