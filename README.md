# Snack Counter Automation: core logic

Anyone reports "empty". The system picks **one** volunteer, gives them an exclusive, working-hours-aware offer,
hands the refill to someone else if they don't deliver, files the HR claim the moment the receipt arrives,
checks the budget before and after the purchase, and produces one monthly report across NOK and INR.

```
python -m unittest discover -s tests -t .    # 103 tests, ~0.2 s, no dependencies (stdlib only)
python demo.py                               # two-minute tour: chat transcript, audit trail, monthly report
```

## Where things are

| File | Owns |
|---|---|
| `service.py` | the refill lifecycle, HR claim handling, admin actions. The only module that changes state |
| `selection.py` | **who buys**: one pure function (eligibility + fairness order) |
| `budget.py` | **the budget check**: ceiling before the purchase, receipt check after, plus the ledger |
| `reporting.py` | the monthly report (per office in its own currency, one converted total) |
| `workcal.py`, `money.py` | working-time deadlines; integer money and fixed-rate conversion |
| `ports.py`, `simulated.py` | interfaces for chat, HR and clock; fakes (HR decides in 1-10 days, can fail, can lose replies) |

## The problems in the brief, and what stops each one

| Problem | Mechanism | Test |
|---|---|---|
| Box empty for days, everyone assumes someone else will | A named person gets an exclusive offer with a deadline counted in **working** hours. No answer or no delivery: the next person. A late "yes" or a late receipt is refused on the spot, even if the scheduler has not run yet, and a late "yes" also passes the refill on at once. After 3 attempts, or nobody eligible, the office admin is pulled in | `NobodyDelivers`, `OfferDeadline`, `CommitmentDeadline`, `EscalationToAdmin` |
| Two people buy the same day | One open refill per office (extra reports join it). One exclusive assignee. Every state change is atomic | `OneBuyerPerRefill` (incl. a thread race) |
| A few people do most refills | Ranked by refills completed in the last 90 days, then round-robin | `Fairness` |
| Weeks to get money back | The claim is filed automatically at receipt time. HR failures are retried with the same reference, so no duplicates. People whose claim HR has not approved yet are asked last. Claims silent after 10 days are chased once | `FailuresNeverDuplicate`, `HROutcomes` |
| Nobody knows monthly spend | One ledger counts every purchase, including those held for admin approval (shown separately), and feeds both the budget check and the report | `Totals`, `Reconciliation`, `HeldPurchasesStillCount` |
| Which snacks get eaten | Receipt carries item lines; "empty" reports can name what ran out. Report shows units restocked and stock-outs per snack | `WhatGetsEaten` |

## Assumptions

Nobody answered my questions, so each of these is an explicit decision, kept small and cheap to change.

| Topic | Assumption | If it is wrong |
|---|---|---|
| Empty signal | Any employee reports it in chat. One counter per office, so one open refill per office | Several boxes: key the "open refill" by (office, box) |
| Availability | Self-declared leave; no calendar feed. Buyers need not be at the office at the moment of buying. When the admin assigns an escalated refill they may overrule the fairness order and who was already asked, but **not availability**: someone on leave, or in the other office, is refused | Replace `set_leave` with a calendar/OOO adapter |
| Visitors | A registered visit makes the person eligible at the **host** office only, for those dates | Opt-in is already per visit; to stay eligible at home, drop the override in `Volunteer.office_on` |
| Currency | Receipts must be in the office's currency, and claims go to HR **in that currency**; HR converts to payroll currency. Budgets are therefore exact, with no FX | HR wants payroll currency: convert in the HR adapter only |
| HR contract | `create_claim` takes our unique reference (or we embed it in the description) and repeats are idempotent. Outcomes are polled: pending, approved or rejected. **"HR approved" is the last thing we can see; it does not mean paid** (see below) | No idempotency: stop auto-retrying on timeouts and have a human check HR |
| Budget | Calendar month in the office's timezone, no rollover. Limits are **soft**: an over-cap or over-budget receipt is held for the office admin, never blocked (the money is spent) and never sent to HR silently. **A held purchase still counts as spent**: it reduces what is left, can exhaust the budget, and is in the monthly total, with the held part shown separately. Exhausted budget escalates to the admin, whose assignment is the approval | Hard block: replace "held" with a refusal at `complete()` |
| Timing | Accept within 4 working hours, deliver within 8 (about a day), 3 attempts, Mon-Fri 09-17 local plus office holidays. A late yes is refused by `accept()` (which also passes the refill on) and a late receipt by `complete()`, both with the same rule `tick()` uses. Admin is the final escalation | All in `Policy` and `WorkCalendar` |
| Reporting | NOK by default, fixed monthly rate from Finance, each office summed natively then converted once. Total spending is every purchase made; `Spent = HR approved + Awaiting HR + Rejected + Held` | Another currency or rate rule is one `FxRates` |

### "HR approved" is not "paid"

The simulated HR system reports `pending`, `approved` or `rejected`, and nothing else. The payroll payout, and any
confirmation that it happened, belongs to the real HR company's systems and is outside this integration. So nothing
here says a volunteer has been reimbursed. "HR approved" is the last status we can observe, and the point from which
we stop treating someone as waiting for money when ranking volunteers. Adding payout confirmation later is additive:
a new `ClaimState`, a polling step after approval, and one more bucket in the report. The refill lifecycle is untouched.

## Deliberately not built

Real chat and HR adapters (the interfaces are in `ports.py`); payroll payout confirmation; persistence (in memory
here, and each record maps to a table; the lock becomes a conditional `UPDATE`); receipt OCR (volunteers type item
lines, so "eaten" is a proxy); reminders before a deadline; a grace path for a receipt that arrives after the
deadline (it is refused whether or not the refill has already been handed on, and the admin handles it: a known
trade-off); an SLA on admin approvals; FX at purchase date; dashboards. Each is additive and none changes the
refill lifecycle.
