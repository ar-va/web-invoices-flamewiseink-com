"""Table tests for app/lifecycle.py: every rule agreed for invoice statuses.

Run: python -I -m unittest discover -s tests -p "test_lifecycle.py"
"""
import os
import sys
import unittest
from datetime import date

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import lifecycle as L  # noqa: E402

TODAY = date(2026, 11, 19)
ADMIN = L.Actor(1, L.ADMIN)
MANAGER = L.Actor(2, L.MANAGER)
AD = L.Actor(3, L.ADVERTISING)
OTHER_AD = L.Actor(4, L.ADVERTISING)


def inv(status="sent", total=10000, paid=0, due="2026-12-03", owner=3):
    return L.Invoice(status=status, total_cents=total, paid_cents=paid, due_date=due, owner_id=owner)


class Reading(unittest.TestCase):
    def test_balance_never_negative(self):
        self.assertEqual(L.balance(inv(total=10000, paid=2500)), 7500)
        self.assertEqual(L.balance(inv(total=10000, paid=12000)), 0)

    def test_overdue_starts_day_after_due_date(self):
        cases = [
            ("sent", "2026-11-19", "sent"),        # due today: not yet
            ("sent", "2026-11-18", "overdue"),     # due yesterday
            ("partial", "2026-11-18", "overdue"),
            ("paid", "2026-11-01", "paid"),
            ("draft", "2026-11-01", "draft"),
            ("void", "2026-11-01", "void"),
        ]
        for status, due, expected in cases:
            with self.subTest(status=status, due=due):
                self.assertEqual(L.display_status(inv(status=status, due=due), TODAY), expected)

    def test_client_can_pay_online(self):
        self.assertTrue(L.client_can_pay_online(inv("sent")))
        self.assertTrue(L.client_can_pay_online(inv("partial", paid=100)))
        for st in ("draft", "paid", "void"):
            self.assertFalse(L.client_can_pay_online(inv(st)))


class Permissions(unittest.TestCase):
    def test_advertising_only_sees_own(self):
        self.assertTrue(L.can_view(inv(owner=3), AD))
        self.assertFalse(L.can_view(inv(owner=3), OTHER_AD))
        self.assertEqual(L.allowed_actions(inv(owner=3), OTHER_AD), frozenset())
        self.assertTrue(L.can_view(inv(owner=3), MANAGER))
        self.assertFalse(L.can_view(inv(owner=3), None))

    def test_action_matrix(self):
        A = L.allowed_actions
        # draft, no payments
        self.assertEqual(A(inv("draft"), AD), {"edit", "email", "send", "void", "record_payment"})
        self.assertEqual(A(inv("draft"), ADMIN), {"edit", "email", "send", "void", "record_payment", "delete"})
        # sent, no payments: can go back to draft
        self.assertEqual(A(inv("sent"), AD), {"edit", "email", "void", "reopen", "record_payment"})
        # partly paid: no void, no reopen; only admin/manager remove payments
        self.assertEqual(A(inv("partial", paid=5000), AD), {"edit", "email", "record_payment"})
        self.assertEqual(A(inv("partial", paid=5000), MANAGER), {"edit", "email", "record_payment", "remove_payment"})
        # paid: no payments, admin can't delete
        self.assertEqual(A(inv("paid", paid=10000), ADMIN), {"edit", "email", "remove_payment"})
        # void: only reopen (+ delete for admin)
        self.assertEqual(A(inv("void"), AD), {"reopen"})
        self.assertEqual(A(inv("void"), ADMIN), {"reopen", "delete"})
        self.assertEqual(A(inv("void"), MANAGER), {"reopen"})

    def test_require_gives_readable_reason(self):
        with self.assertRaisesRegex(L.LifecycleError, "can't be voided"):
            L.require(inv("partial", paid=100), ADMIN, L.VOID_IT)
        with self.assertRaisesRegex(L.LifecycleError, "can't open"):
            L.require(inv(owner=3), OTHER_AD, L.EDIT)
        with self.assertRaisesRegex(L.LifecycleError, "Only Admins can delete"):
            L.require(inv("paid", paid=10000), ADMIN, L.DELETE)
        L.require(inv("draft"), ADMIN, L.DELETE)  # allowed: no raise


class Transitions(unittest.TestCase):
    def test_send(self):
        self.assertEqual(L.after_send(inv("draft")), "sent")
        self.assertEqual(L.after_send(inv("draft", total=0)), "paid")          # $0 invoice
        self.assertEqual(L.after_send(inv("draft", paid=4000)), "partial")

    def test_void_and_reopen(self):
        self.assertEqual(L.after_void(inv("sent")), "void")
        self.assertEqual(L.after_reopen(inv("void")), "draft")

    def test_manual_payment(self):
        self.assertEqual(L.after_payment(inv("sent"), 4000), "partial")
        self.assertEqual(L.after_payment(inv("sent"), 10000), "paid")
        self.assertEqual(L.after_payment(inv("draft"), 4000), "partial")      # draft becomes sent
        self.assertEqual(L.after_payment(inv("partial", paid=4000), 6000), "paid")
        with self.assertRaisesRegex(L.LifecycleError, "more than the \\$100.00"):
            L.after_payment(inv("sent"), 10001)
        with self.assertRaisesRegex(L.LifecycleError, "more than \\$0"):
            L.after_payment(inv("sent"), 0)

    def test_payment_removed(self):
        self.assertEqual(L.after_payment_removed(inv("paid", paid=10000), 10000), "sent")
        self.assertEqual(L.after_payment_removed(inv("paid", paid=10000), 4000), "partial")

    def test_edit(self):
        self.assertEqual(L.after_edit(inv("paid", paid=10000), 15000), "partial")   # added a line
        self.assertEqual(L.after_edit(inv("partial", paid=5000), 5000), "paid")     # edited down to what was paid
        self.assertEqual(L.after_edit(inv("draft"), 0), "draft")                     # drafts stay drafts
        self.assertEqual(L.after_edit(inv("sent"), 0), "paid")                       # $0 once sent
        with self.assertRaisesRegex(L.LifecycleError, "can't be less than the \\$150.00"):
            L.after_edit(inv("partial", total=20000, paid=15000), 10000)

    def test_card_payment_always_recorded(self):
        o = L.after_card_payment(inv("sent"), 10000)
        self.assertEqual((o.applied_cents, o.extra_cents, o.status, o.needs_review), (10000, 0, "paid", False))
        # convenience fee on top, expected: not flagged
        o = L.after_card_payment(inv("sent"), 10350, fee_expected=True)
        self.assertEqual((o.applied_cents, o.extra_cents, o.needs_review), (10000, 350, False))
        # total lowered while client was paying: extra flagged
        o = L.after_card_payment(inv("sent", total=8000), 10000)
        self.assertEqual((o.applied_cents, o.extra_cents, o.status, o.needs_review), (8000, 2000, "paid", True))
        # voided while client was paying: comes back, flagged
        o = L.after_card_payment(inv("void"), 4000)
        self.assertEqual((o.status, o.needs_review), ("partial", True))
        # moved back to draft while paying: comes back as sent/partial
        self.assertEqual(L.after_card_payment(inv("draft"), 10000).status, "paid")


if __name__ == "__main__":
    unittest.main()
