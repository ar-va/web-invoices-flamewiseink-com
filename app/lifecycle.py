"""Invoice lifecycle: every rule about an Invoice's status, Balance and allowed actions.

Pure functions, no database or web code. Routes load an Invoice, ask this module
what is allowed and what the next status is, then save the answer.
Terms follow GLOSSARY.md.
"""
from dataclasses import dataclass
from datetime import date

# Stored statuses. "overdue" is never stored; see display_status().
DRAFT, SENT, PARTIAL, PAID, VOID = "draft", "sent", "partial", "paid", "void"
OPEN_STATUSES = (SENT, PARTIAL)          # the ones that can become Overdue

# Roles (see GLOSSARY.md)
ADMIN, MANAGER, ADVERTISING = "admin", "manager", "advertising"
SEES_ALL_ROLES = (ADMIN, MANAGER)

# Actions a person can take on an Invoice
EDIT, SEND, EMAIL, VOID_IT, REOPEN, SHARE = "edit", "send", "email", "void", "reopen", "share"
RECORD_PAYMENT, REMOVE_PAYMENT, DELETE = "record_payment", "remove_payment", "delete"


class LifecycleError(ValueError):
    """An action the rules don't allow. The message is safe to show to the user."""


@dataclass(frozen=True)
class Invoice:
    status: str
    total_cents: int
    paid_cents: int
    due_date: str            # ISO date, e.g. "2026-12-03"
    owner_id: int | None     # account that created it
    shared_with: frozenset = frozenset()   # Advertising accounts it was shared with

    @classmethod
    def from_row(cls, row, shared_with=frozenset()):
        return cls(status=row["status"], total_cents=row["total_cents"], paid_cents=row["paid_cents"],
                   due_date=row["due_date"], owner_id=row["created_by"], shared_with=frozenset(shared_with))


@dataclass(frozen=True)
class Actor:
    id: int
    role: str


@dataclass(frozen=True)
class CardOutcome:
    """Result of recording an approved card payment."""
    applied_cents: int       # counted toward the Invoice (never more than the Balance)
    extra_cents: int         # charged on top of that (convenience fee or overpayment)
    status: str              # new stored status
    needs_review: bool       # money arrived on a Void Invoice or above the Balance


# ---------------------------------------------------------------- reading


def balance(inv: Invoice) -> int:
    return max(0, inv.total_cents - inv.paid_cents)


def is_overdue(inv: Invoice, today: date) -> bool:
    return inv.status in OPEN_STATUSES and inv.due_date < today.isoformat()


def display_status(inv: Invoice, today: date) -> str:
    return "overdue" if is_overdue(inv, today) else inv.status


def client_can_pay_online(inv: Invoice) -> bool:
    return inv.status in OPEN_STATUSES and balance(inv) > 0


# ---------------------------------------------------------------- who may do what


def sees_all(actor: Actor | None) -> bool:
    return bool(actor) and actor.role in SEES_ALL_ROLES


def can_view(inv: Invoice, actor: Actor | None) -> bool:
    """Admin/Studio Manager see all; others see Invoices they created or that were shared with them.

    A shared Invoice works exactly like one the person created.
    """
    return bool(actor) and (sees_all(actor) or inv.owner_id == actor.id or actor.id in inv.shared_with)


def allowed_actions(inv: Invoice, actor: Actor | None) -> frozenset:
    if not can_view(inv, actor):
        return frozenset()
    acts = set()
    if inv.status != VOID:
        acts |= {EDIT, EMAIL}
    if inv.status == DRAFT:
        acts.add(SEND)
    if inv.status != VOID and inv.paid_cents == 0:
        acts.add(VOID_IT)
    if (inv.status == SENT and inv.paid_cents == 0) or inv.status == VOID:
        acts.add(REOPEN)
    if inv.status != VOID and balance(inv) > 0:
        acts.add(RECORD_PAYMENT)
    if actor.role in SEES_ALL_ROLES and inv.paid_cents > 0:
        acts.add(REMOVE_PAYMENT)
    if actor.role == ADMIN and inv.status in (DRAFT, VOID):
        acts.add(DELETE)
    if actor.role in SEES_ALL_ROLES:
        acts.add(SHARE)
    return frozenset(acts)


def require(inv: Invoice, actor: Actor | None, action: str):
    if action in allowed_actions(inv, actor):
        return
    if not can_view(inv, actor):
        raise LifecycleError("Your account can't open this invoice.")
    raise LifecycleError(_REFUSALS.get(action, "That isn't allowed for this invoice."))


_REFUSALS = {
    EDIT: "Void invoices can't be edited. Move it back to draft first.",
    EMAIL: "Void invoices can't be emailed.",
    SEND: "Only drafts can be marked as sent.",
    VOID_IT: "Invoices with payments can't be voided. Remove the payments first, or edit the invoice to what was kept.",
    REOPEN: "Only sent invoices without payments, or void invoices, can go back to draft.",
    RECORD_PAYMENT: "This invoice can't take a payment (it's void or already paid).",
    REMOVE_PAYMENT: "Only Admin and Studio Manager accounts can remove payments.",
    DELETE: "Only Admins can delete, and only draft or void invoices. Void it instead.",
    SHARE: "Only Admin and Studio Manager accounts can share invoices.",
}


# ---------------------------------------------------------------- what an action changes


def _settle(base: str, total: int, paid: int) -> str:
    """Status implied by the money, for an Invoice that is past Draft and not Void."""
    if base in (DRAFT, VOID):
        return base
    if paid >= total:            # includes $0 invoices
        return PAID
    return PARTIAL if paid > 0 else SENT


def after_send(inv: Invoice) -> str:
    return _settle(SENT, inv.total_cents, inv.paid_cents)


def after_void(inv: Invoice) -> str:
    return VOID


def after_reopen(inv: Invoice) -> str:
    return DRAFT


def after_edit(inv: Invoice, new_total_cents: int) -> str:
    if new_total_cents < inv.paid_cents:
        raise LifecycleError(
            f"The new total can't be less than the ${inv.paid_cents / 100:,.2f} already received.")
    base = DRAFT if inv.status == DRAFT else SENT
    return _settle(base, new_total_cents, inv.paid_cents)


def after_payment(inv: Invoice, amount_cents: int) -> str:
    """Manual payment (e-Transfer, cash, terminal). Capped at the Balance."""
    if amount_cents <= 0:
        raise LifecycleError("Amount must be more than $0.")
    if amount_cents > balance(inv):
        raise LifecycleError(
            f"That's more than the ${balance(inv) / 100:,.2f} still owed. "
            "Add a line item for tips or extras first.")
    return _settle(SENT, inv.total_cents, inv.paid_cents + amount_cents)


def after_payment_removed(inv: Invoice, amount_cents: int) -> str:
    paid = max(0, inv.paid_cents - amount_cents)
    base = DRAFT if inv.status == DRAFT else SENT
    return _settle(base, inv.total_cents, paid)


def after_card_payment(inv: Invoice, charged_cents: int, fee_expected: bool = False) -> CardOutcome:
    """An approved card charge is real money: always recorded, never refused.

    A Void or Draft Invoice comes back as Sent/Partly paid; anything above the
    Balance is kept as extra; it is flagged for review unless a convenience fee
    was expected.
    """
    owed = balance(inv)
    applied = min(charged_cents, owed)
    status = _settle(SENT, inv.total_cents, inv.paid_cents + applied)
    extra = charged_cents - applied
    return CardOutcome(applied_cents=applied, extra_cents=extra, status=status,
                       needs_review=inv.status == VOID or (extra > 0 and not fee_expected))
