# Demo Script — AI Revenue Recovery

Target length: 4-5 minutes live demo + 1-2 minutes Q&A buffer.
Have `main.py`'s batch dashboard open in one tab as backup the entire time.

---

## 0. Opening line (10 seconds)

> "Revenue loss rarely happens in one clean step — a payment fails, a
> checkout gets abandoned, an invoice goes overdue. Our agent detects
> all three, picks a different action for each, and never acts blindly —
> everything is bounded, escalated, and logged."

Don't over-explain yet. The demo should show this, not just say it.

## 1. Show the live system is real (45 seconds)

- Have `webhook_app.py` and your tunnel already running before you start talking.
- Open `/health` in a browser tab: point at `"live_mode": true` and `"signature_verification": true`.
- One line: "This isn't replaying a recording — it's a live webhook receiver registered with Razorpay's test-mode dashboard right now."

## 2. Trigger a real failure live (60 seconds)

- Open a pre-created Razorpay Payment Link (or trigger a real test-card decline).
- Switch to your `/dashboard` tab (already open, auto-refreshing).
- Point at the new row appearing: root cause, action taken, cost, outcome = "pending."
- One line: "It didn't just log 'payment failed' — it diagnosed *why* (insufficient funds vs expired card vs a genuine dispute) and picked a different response for each."

## 3. Show the promise-to-pay moment — your strongest beat (45 seconds)

- Click the reminder link yourself (from the message shown in the dashboard row, or your own test inbox if wired to a real number).
- Refresh `/dashboard` — the row flips to **"promised"**, visually distinct (amber highlight).
- One line: "We don't just log 'sent a reminder' — we know the difference between silence and a customer who engaged but hasn't paid yet. That's the promise-to-pay signal."

## 4. Complete the recovery live (30 seconds)

- Pay the link with a Razorpay success test card.
- Refresh `/dashboard` — row flips to **"recovered"**, gross/net recovered numbers update.
- One line: "This is a real `payment.captured` webhook confirming it — not a simulated success. If I hadn't paid it, it would still say pending."

## 5. Show the stopping rule — this is what separates you from a simple if/else (30 seconds)

- Don't wait for real timing live — instead, pull up the **Pipeline Flow funnel** on the dashboard.
- Point at "Escalated to 2nd attempt" and "Terminated (stopped, unrecovered)" bars.
- One line: "Every event gets at most two attempts, ever — enforced in code, not just documented. If attempt one fails, attempt two uses a genuinely different channel, not a repeat. If both fail, it's marked terminated and nothing acts on it again. That's the compliant escalation and stopping rule the brief asks for."

## 6. Zoom out to the batch numbers (45 seconds)

- Switch to the batch dashboard (`main.py`'s output).
- Point at: Total at risk, Net recovered, Recovery rate.
- One line, said carefully: "57.7% recovery rate, validated across 50 independent simulation runs, not a single lucky one — and calibrated against published industry benchmarks, not invented numbers."
- Point at the **Per-Cause Breakdown table**: "This tells us which failure type is worth the most attention — not just a flat list of events."

## 7. Close (20 seconds)

> "Payment failures, checkout abandonment, and overdue receivables — all
> three loss types in the brief — flow through the same bounded,
> auditable pipeline. Every dollar recovered, every dollar it cost to
> recover it, and every decision the agent made along the way is logged
> and explainable."

---

## Anticipated judge questions, and the honest answer for each

**"How do I know your 57.7% recovery rate is real?"**
> "It's not a claim about our specific agent's real-world performance —
> it's a simulation calibrated against published industry benchmarks
> (Baremetrics, Churnkey), validated by running it 50 times and checking
> the average lands where the literature says it should. We're
> transparent that this validates the simulation's realism, not a
> live-measured result."

**"Why does the live dashboard show fewer recovered events than the batch one?"**
> "Live mode never fakes a result — an event only becomes 'recovered'
> when a real payment.captured webhook confirms it. The batch dashboard
> demonstrates the pipeline's projected behavior at scale; the live one
> proves the mechanism is real, even if that means showing 'pending' or
> 'promised' during a short demo window."

**"What happens if both attempts fail?"**
> "It's marked terminated_unrecovered permanently — there's no third
> attempt anywhere in the codebase. We can show you the code path if
> you want to verify that."

**"Is this specific to Razorpay?"**
> "The webhook parsing layer is Razorpay-specific, but the diagnosis →
> intervention → escalation → audit pipeline is gateway-agnostic — same
> architecture would work behind Stripe or any other provider's webhooks."

**"What's the audit trail's compliance story?"**
> "Every event carries a full escalation_log narrating exactly what
> happened and when. One honest caveat: rows are updated in place rather
> than being a strictly insert-only immutable log — a production version
> aimed at strict compliance would append new rows per state transition
> instead."

---

## Fallback plan if live demo breaks

1. If the tunnel or webhook drops mid-demo: don't troubleshoot live —
   say "let's look at a run we captured earlier" and switch straight to
   the batch dashboard. Judges respect a calm pivot far more than
   watching you debug.
2. Keep a screen recording of one successful live run (trigger →
   promised → recovered) as an absolute last resort if the network
   fails entirely in the room.
3. Know your batch numbers cold (57.7% ± 5.9%, from `run_sensitivity.py`)
   so you can state them confidently without pulling up a file.
