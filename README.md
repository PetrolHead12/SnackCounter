# Snack Counter Automation: core logic

Oslo and Bengaluru each have one snack counter, kept stocked by volunteers who buy snacks with their own money and
claim it back through the company's HR system (run by another company, which approves claims in 1-10 days). Run on
goodwill, that fails in predictable ways: the counter stays empty because everyone assumes someone else will refill
it, two people buy on the same day, a few people do most of the work and wait weeks to be repaid, and nobody knows
what is spent or which snacks get eaten.

This project is the core logic that coordinates that process. People still buy and stock the snacks; what is
automated is who does it, what happens when they don't, the HR claim, the budget and the reporting. Anyone reports
"empty". The system picks **one** volunteer, gives them an exclusive, working-hours-aware offer, hands the refill to
someone else if they don't deliver (and finally to the office admin), files the HR claim as soon as the receipt is in
(after an admin decision if it is over the cap or the budget), checks the budget before and after the purchase, and
produces one monthly report across NOK and INR.

**Status.** Implemented and tested: everything described here, run against a *simulated* chat tool, a *simulated* HR
system and a controllable clock. Not implemented: real chat or HR integrations, inbound chat commands, a scheduler
that calls `tick()`, persistence (state is in memory and lost on restart) and authentication. See "Deliberately not
built". The reasoning behind the design, and the assumptions it rests on, are in [DESIGN.md](DESIGN.md).

```
python -m unittest discover -s tests -t .    # 103 tests, well under a second, standard library only
python demo.py                               # two-minute tour: chat transcript, audit trail, monthly report
```

Needs Python 3.9 or newer; developed and tested on 3.12 (not run on older versions). There is nothing to install.
Run the commands from the project folder (the one containing `snackcounter/`, `tests/` and `demo.py`); on some systems
the interpreter is called `python3`. One test module: `python -m unittest tests.test_budget -v`.

## Where things are

| File | Owns |
|---|---|
| `service.py` | the refill lifecycle, HR claim handling, admin actions: the only module that decides state changes |
| `selection.py` | **who buys**: pure functions for availability (`unavailable_reason`) and the fairness order (`rank_candidates`) |
| `budget.py` | **the budget check**: ceiling before the purchase, receipt check after, plus the ledger |
| `reporting.py` | the monthly report: per office in its own currency, one converted total, held purchases included and shown separately |
| `models.py` | plain data records (`Office`, `Volunteer`, `Refill`, `Claim`) and their state enums |
| `workcal.py`, `money.py` | working-time deadlines; integer money and fixed-rate conversion |
| `ports.py`, `simulated.py` | interfaces for chat, HR and clock; simulated chat and HR (the simulated HR decides after a configurable delay, default 3 days, and can fail, lose a reply or reject) |
| `demo.py`, `tests/` | the demo; the automated tests. `tests/world.py` holds the Oslo/Bengaluru configuration (budgets, typical purchases, volunteers) that the tests and the demo share |

`service.py` sits on top: it uses the other modules and none of them imports it. The simulated chat and HR depend only
on the interfaces in `ports.py`, so real adapters can be plugged in without changing the logic. Everything a chat command
or button would do is a `RefillService` method. Anyone: `report_empty`. Volunteers: `accept`, `decline`, `release`,
`complete`, `set_leave`, `register_visit`. Office admin: `admin_assign`, `approve_held`, `admin_cancel`. Scheduler and
management: `tick`, `monthly_report`.

## How a refill moves from "empty" to "HR approved"

A refill (the counter) and its claim (the money) are separate lifecycles: the counter can be full again long before HR
has decided.

| | States |
|---|---|
| **Refill** | `requested` → `offered` → `committed` → `stocked`. Side exits: `escalated` (needs the office admin) and `cancelled` |
| **Claim** | `queued` → `submitted` → `approved` or `rejected`. A receipt over the cap or over budget starts as `held` and becomes `queued` when the admin approves it |

1. Anyone reports the counter empty (`report_empty`). A second report joins the open refill instead of starting another.
2. If what is left of the month's budget is below the smallest typical purchase, the refill goes straight to the office
   admin. Otherwise the volunteer's spending ceiling is the lower of the per-purchase cap and what is left.
3. The best-ranked eligible volunteer gets an exclusive offer (4 working hours to answer). Accepting starts the delivery
   deadline (8 working hours).
4. Declining, not answering in time, a late "yes", releasing the refill, or missing the delivery deadline excludes that
   person from this refill and offers it to the next. After 3 offers, or when nobody is eligible, it is escalated: the
   admin can assign any available volunteer or cancel it.
5. The volunteer submits the amount, receipt image and items (`complete`). The refill is `stocked`, and the budget
   check decides what happens to the claim: within the cap and the budget it is sent to HR at once; otherwise it is
   `held` until the admin approves it (`approve_held`).
6. A failed submission stays `queued` and is retried on every `tick()` with the same reference. `tick()` also polls HR
   for `approved` or `rejected`. A claim with no HR decision after 10 days notifies the volunteer and the admin once;
   the system does not contact HR itself.

`tick()` is meant to be called every few minutes by a scheduler (not included). It hands over offers and commitments
that have run out, retries unsent claims and polls HR. The two deadlines are also enforced at the moment someone acts:
`accept()` refuses a late "yes" and passes the refill on, and `complete()` refuses a late receipt, so neither waits for
the next `tick()`.

## Budget handling

- **Limits used.** Oslo: 6,000 NOK a month, typical purchase 300-1,200 NOK. Bengaluru: 15,000 INR a month, typical
  purchase 500-3,000 INR. These come from the brief and are configured in `tests/world.py`. The top of the typical
  range is the per-purchase cap; the bottom is the smallest purchase worth funding.
- **Two moments.** Before the purchase the volunteer is told a spending ceiling. After it, the receipt is checked
  against the per-purchase cap and what is left of the month.
- **Soft limits.** A receipt above the cap or beyond the remaining budget is recorded and held for the admin: never
  rejected, never silently sent to HR.
- **Held purchases are real spending.** Every purchase counts as spent from the moment it is made. Held ones reduce
  what is left, can exhaust the budget, and are in the monthly total, with the held part shown separately. Admin
  approval only releases the claim to HR.
- **Exhausted budget.** If what is left is below the smallest typical purchase, nobody is asked: the admin decides.
  Assigning a volunteer is the approval to go past the budget (the per-purchase cap still applies).
- **What counts.** A purchase counts in the month it was made (its purchase date, in the office's timezone). An HR
  rejection does not remove it from spending: the snacks were bought.
- **Scope.** Per office, in the office's own currency, per calendar month in the office's timezone, no rollover. A
  receipt in another currency is refused.

## The problems in the brief, and what stops each one

| Problem | Mechanism | Test |
|---|---|---|
| Box empty for days, everyone assumes someone else will | A named person gets an exclusive offer with a deadline counted in **working** hours. No answer or no delivery: the next person. A late "yes" or a late receipt is refused on the spot, even if the scheduler has not run yet, and a late "yes" also passes the refill on at once. After 3 attempts, or nobody eligible, the office admin is pulled in | `NobodyDelivers`, `OfferDeadline`, `CommitmentDeadline`, `EscalationToAdmin` |
| Two people buy the same day | One open refill per office (extra reports join it). One exclusive assignee. Every call runs under one in-process lock (single process only: see DESIGN.md) | `OneBuyerPerRefill` (incl. a thread race) |
| A few people do most refills | Ranked by refills completed in the last 90 days (people with a claim HR has not yet decided go last), then round-robin | `Fairness` |
| Weeks to get money back | The claim is filed automatically at receipt time. A failed submission is retried with the same reference, so the simulated HR creates no duplicates (a real HR is assumed to behave the same way). People with a claim HR has not yet decided are asked last. After 10 days with no HR decision the volunteer and the admin are notified once | `FailuresNeverDuplicate`, `HROutcomes` |
| Nobody knows monthly spend | One ledger counts every purchase, including those held for admin approval (shown separately), and feeds both the budget check and the report | `Totals`, `Reconciliation`, `HeldPurchasesStillCount` |
| Which snacks get eaten | The volunteer enters item lines with the receipt; "empty" reports can name what ran out. The report shows units restocked and stock-outs per snack, a proxy for what is eaten | `WhatGetsEaten` |

## Assumptions

The brief left the operational details to our judgment and asked us to make reasonable assumptions and state them.
The assumptions below were chosen deliberately, with simplicity, practicality and extensibility in mind, and each is
confined to a small part of the code (last column). They are assumptions, not confirmed business requirements: the
budgets, typical purchase ranges, headcounts and HR's 1-10 day approval time come from the brief, and everything else
is ours. The reasoning behind each one is in [DESIGN.md](DESIGN.md).

| Topic | Assumption | If it is wrong |
|---|---|---|
| Empty signal | Any employee can report an empty counter (`report_empty`; a chat command would call it, but chat is simulated). One counter per office, so one open refill per office | Several boxes: key the "open refill" by (office, box) |
| Availability | Self-declared leave (`set_leave`); no calendar feed. Buyers need not be at the office at the moment of buying. When the admin assigns an escalated refill they may overrule the fairness order and who was already asked, but **not availability**: someone on leave, or in the other office, is refused | Fill `Volunteer.leave` from a calendar/OOO source instead of `set_leave` |
| Visitors | A registered visit (`register_visit`) makes the person eligible at the **host** office only, for those dates | Visitors should stay on the home rota too: make eligibility a set of offices (`Volunteer.office_on`, `unavailable_reason`) |
| Fairness | Ranked by: no claim awaiting an HR decision first, then fewest refills completed in the last 90 days, then asked longest ago. A soft rule: nobody is excluded for having a claim waiting, so a refill can always find someone | Change the sort key in `rank_candidates` (`selection.py`); the window is `Policy.fairness_window_days` |
| Currency | Receipts must be in the office's currency, and claims go to HR **in that currency**; HR is assumed to convert to the employee's payroll currency. Budgets are therefore exact, with no FX | HR wants payroll currency: convert in the HR adapter only |
| HR contract | Every claim carries our unique reference (`create_claim(reference=...)`, also written into the description so people can reconcile), and the simulated HR treats a repeated reference as the same claim. Outcomes are polled: pending, approved or rejected. The company login id is assumed to be the HR employee id. **"HR approved" is the last thing we can see; it does not mean paid** (see below) | A real HR without this idempotency needs another way to avoid duplicates, for example a lookup before retrying (not implemented) |
| Budget | Calendar month in the office's timezone, no rollover. Limits are **soft**: an over-cap or over-budget receipt is held for the office admin, never blocked (the money is spent) and never sent to HR silently. **A held purchase still counts as spent**: it reduces what is left, can exhaust the budget, and is in the monthly total, with the held part shown separately. Exhausted budget escalates to the admin, whose assignment is the approval | Hard block: replace "held" with a refusal at `complete()` |
| Timing | Accept within 4 working hours, deliver within 8 (about a day), 3 attempts, Mon-Fri 09-17 in the office's timezone (`WorkCalendar` supports holidays; none are configured in the demo or the shared test world, only in the calendar's own tests). A late yes is refused by `accept()` (which also passes the refill on) and a late receipt by `complete()`, both with the same rule `tick()` uses. Admin is the final escalation | All in `Policy` and `WorkCalendar` |
| Reporting | NOK in the demo and tests; the caller passes the month's fixed rates (`FxRates`, assumed to come from Finance; the demo's 1 INR = 0.125 NOK is illustrative). Each office is summed natively, then converted once. Total spending is every purchase made; `Spent = HR approved + Awaiting HR + Rejected + Held` | Another reporting currency: pass a different `FxRates` (the currency must be known to `money.py`; only NOK and INR are today) |
| Snack data and receipts | The volunteer types the items and quantities with the receipt, so "eaten" is estimated from units restocked and stock-out reports. The image is passed to HR and kept only until HR has accepted it; a SHA-256 fingerprint is kept to refuse a receipt submitted twice | OCR could fill the items at `complete()`; to keep images, store them where `Claim.receipt` is dropped |

### "HR approved" is not "paid"

The simulated HR system reports `pending`, `approved` or `rejected`, and nothing else. The payroll payout, and any
confirmation that it happened, belongs to the real HR company's systems and is outside this integration. So nothing
here says a volunteer has been reimbursed. "HR approved" is the last status we can observe, and the point from which a
claim stops counting as awaiting a decision when ranking volunteers. Adding payout confirmation later is additive: a
new `ClaimState`, a polling step after approval, and one more bucket in the report. The refill lifecycle is untouched.

## Deliberately not built

None of the following exists in this repository: real chat and HR adapters (the interfaces are in `ports.py`) and
inbound chat commands; a scheduler that calls `tick()` every few minutes; authentication (the chat tool is assumed to
identify people); persistence (state is in memory; the records are plain data, so they could be stored in tables
later, and the in-process lock would then give way to database transactions); calendar-based availability;
sensor-based inventory detection; receipt OCR (volunteers type item lines, so "eaten" is a proxy); payroll payout
confirmation; reminders before a deadline; a grace path for a receipt that arrives after the deadline (it is refused
whether or not the refill has already been handed on, and the admin handles it: a known trade-off); an SLA on admin
approvals; FX at purchase date; monitoring and dashboards.
