# Snack Counter Automation: Design Document

Core logic for the Oslo and Bengaluru snack counters, with simulated chat and HR (see `README.md`).

## 1. What "automate" means

The brief's failures are coordination failures: nobody owns the refill, two people act at once, a few carry the cost and
wait for repayment, and nobody sees the spend. People still buy and stock the snacks; the system automates **who**
refills, **what happens if they don't**, **the HR claim**, **budget control** and **reporting**. *Why:* that removes
every listed failure without purchasing or hardware integrations.

## 2. From "empty" to "HR approved"

**Refill:** `empty → offered → committed → stocked`, with `escalated` (the admin) when nobody can take it.
**Claim:** `receipt → [held →] queued → submitted → HR approved / rejected`.

1. Anyone reports the counter empty; later reports join the open refill (one per office).
2. If less than the smallest typical purchase is left of the month's budget, the admin decides; otherwise the
   best-ranked available volunteer gets an exclusive offer with a spending ceiling.
3. 4 working hours to answer, 8 to deliver. Declining, silence, a late "yes", releasing or a missed deadline passes it
   on; after 3 offers or nobody eligible, the admin assigns or cancels.
4. The volunteer submits amount, receipt image and items. Within cap and budget the claim goes to HR at once; otherwise
   it is held until the admin approves.
5. `tick()`, run every few minutes by a scheduler, hands over lapsed offers, retries unsent claims, polls HR and flags
   a claim undecided after 10 days.

The brief's "reimbursed" is modelled up to **HR approved**. Payroll payout and its confirmation are outside the simulated
HR integration, so the system never says anyone has been paid.

## 3. Parts and the data each keeps

- **Refill service.** *Refill:* office, state, assignee, deadline, offers made, who was already asked, spending ceiling,
  purchase (amount, date, items, receipt SHA-256), audit log. *Claim:* our reference, employee id, amount and currency,
  state, HR claim id, retries; the receipt image only until HR has it. *Volunteer:* home office, leave, visits, last
  offered.
- **Budget ledger.** One entry per purchase: refill, office, month, amount, held or cleared. Feeds the budget checks and
  the report.
- **Selection, reporting, money and calendar helpers, and the chat, HR and clock interfaces** keep nothing. HR keeps the
  image and its decision; we keep our reference and its claim id.

## 4. How each problem is prevented

- **Box empty for days.** A named owner, working-hour deadlines (also enforced when someone answers), automatic
  hand-over, the admin as backstop.
- **Two people buy the same day.** One open refill per office, one exclusive assignee, every change under one lock; a
  late receipt is refused, so one refill gives one claim.
- **A few people do most refills.** Ranked by recent refills; undecided claims last.
- **Weeks to get money back.** Claim filed at receipt time, retried until HR has it (same reference, no duplicates),
  flagged after 10 days. HR's own decision time and the payroll payout stay outside our control.
- **Nobody knows spend or favourites.** One ledger counts every purchase, held ones included; the report has both
  currencies and a total; item lines and stock-outs show what is eaten.

## 5. Assumptions and why

The brief left operational details to our judgment, so each assumption was chosen for simplicity, practicality and
extensibility. Budgets, typical ranges and HR's 1-10 day approval time are from the brief; the rest is assumed.

- **Empty signal.** Any employee reports it in chat; one counter per office. *Why:* no hardware, works from day one;
  sensors could post the same event later.
- **Availability and visitors.** Volunteers declare their own leave and need not be in the office when buying; a visitor
  is eligible at the host office only, buying in its currency. *Why:* no calendar source was given and the deadline
  matters more than presence; a visitor can restock the counter beside them, and that office's budget stays exact;
  hand-over catches stale leave.
- **Selection.** Undecided claim last, then fewest refills in 90 days, then asked longest ago. *Why:* targets "few do
  most" and "wait weeks"; counts refills, not money; soft, so a refill always finds someone.
- **Timing.** Windows in section 2 count working time only (Mon-Fri 09-17 local); one admin per office is the final
  escalation. *Why:* about a day to restock, worst case bounded; elapsed hours would let a Friday-evening offer cycle
  through sleeping volunteers; someone must own exceptions.
- **Budget.** Calendar month, no rollover; the top of the typical range is the per-purchase cap, the bottom the smallest
  purchase worth funding; receipts over the cap or beyond what is left are held for the admin; held purchases count
  as spent. *Why:* the money is already spent, so rejecting strands the volunteer, silent approval loses control, and
  ignoring held purchases hides overspend.
- **Currency.** Receipts in the office's currency; claims go to HR in it and HR converts to payroll currency; the report
  converts each office's subtotal once at a fixed monthly rate (NOK). *Why:* the brief repays in payroll currency,
  implying HR converts; budgets stay exact.
- **HR contract.** Claims carry our unique reference (a repeat is harmless); outcomes are polled; the company login is
  the HR employee id. *Why:* safe retries need idempotency and "reimbursed" must be observable; fallback: look the claim
  up before retrying.
- **Snack data and receipts.** Volunteers type the items, so "eaten" is estimated from units restocked and stock-outs;
  the image goes to HR and we keep a SHA-256 fingerprint. *Why:* answers "which snacks" cheaply (OCR is costly);
  HR is the system of record, so we hold the minimum.

## 6. Trade-offs, and what we would deliberately not build

- **Exclusive offers, not "first to click".** Slower if someone is silent, but fair.
- **Hold, not block.** Keeps a human decision in the loop, at the cost of admin time.
- **Strict deadlines.** A late receipt goes to the admin; there is no grace path.
- **In-memory state under one lock** suits a prototype. Production needs database transactions, and the hand-over a
  late `accept()` triggers must be committed before the error is reported.

**Not built** (all additive; full list in `README.md`): real chat and HR adapters, the scheduler, persistence, calendar
and sensor integrations, receipt OCR, payroll payout confirmation, reminders, dashboards.
