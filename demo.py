"""A two-minute tour:  python demo.py

Oslo        the happy path, from "empty" to "HR approved".
Bengaluru   an ignored offer is handed on; a too-large receipt is held for the admin.
Visitor     everyone local is away, so a visiting Oslo colleague is asked; claim stays in rupees.
Then HR decides and the one monthly report is printed.
"""

from datetime import date, timedelta

from snackcounter import SnackLine, render_text
from tests.world import FX, INR, NOK, World

w = World(n_oslo=4, n_blr=3)
_shown = 0


def step(title):
    print(f"\n=== {title}")


def chat():
    """Print the chat messages sent since the last call."""
    global _shown
    for m in w.chat.sent[_shown:]:
        print(f"   chat -> {m.user_id:<10} {m.text}")
    _shown = len(w.chat.sent)


step("Oslo: someone reports the box empty")
r1 = w.svc.report_empty("OSL", "emp-17", ["chips", "nuts"])
chat()
buyer = r1.assignee_id
w.svc.accept(r1.id, buyer)
w.svc.complete(r1.id, buyer, NOK("740"), b"oslo-receipt-1", [SnackLine("chips", 8), SnackLine("nuts", 4)])
chat()

step("Bengaluru: the first volunteer never answers")
r2 = w.svc.report_empty("BLR", "emp-31", ["namkeen"])
chat()
w.run_out_the_clock(r2)
chat()
second = r2.assignee_id
w.svc.accept(r2.id, second)
claim = w.svc.complete(r2.id, second, INR("3400"), b"blr-receipt-1", [SnackLine("namkeen", 12)])
print(f"   claim {claim.id} is {claim.state.value}: 3,400 INR is above the 3,000 INR cap")
chat()
w.svc.approve_held(r2.id, "admin-blr")
chat()

step("Bengaluru again: all locals on leave, an Oslo colleague is visiting")
for b in ("b1", "b2", "b3"):
    w.svc.set_leave(b, date(2026, 11, 9), date(2026, 11, 22))
w.svc.register_visit("o3", "BLR", date(2026, 11, 9), date(2026, 11, 20))
r3 = w.svc.report_empty("BLR", "emp-32")
w.svc.accept(r3.id, r3.assignee_id)
claim3 = w.svc.complete(r3.id, "o3", INR("1800"), b"blr-receipt-2", [SnackLine("fruit", 15)])
chat()
hr_record = w.hr.claims[claim3.hr_claim_id]
print(f"   HR received {hr_record.amount} for employee {hr_record.employee_id}: HR converts to her payroll currency")

step("Ten days later: HR has decided")
w.clock.advance(timedelta(days=10))
w.svc.tick()
chat()

step("Monthly report")
print(render_text(w.svc.monthly_report(2026, 11, FX)))

step("Audit trail of the Bengaluru refill that was handed on")
for at, text in r2.log:
    print(f"   {at:%Y-%m-%d %H:%M}Z  {text}")
