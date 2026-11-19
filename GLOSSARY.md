# Flame Wise Tattoo Collective Invoices

Invoices the studio issues to its clients, and the payments that settle them.

## Language

**Invoice**:
A numbered request for payment from the studio to one Client, made of Line items.
_Avoid_: Bill, quote

**Client**:
The person or business an Invoice is addressed to; saved Clients form one Client list shared by every account.
_Avoid_: Customer, account

**Saved item**:
A prepared Line item (name, description, quantity, price, tax) that anyone can add to an Invoice or Monthly billing.
_Avoid_: Product, preset, quick item

**Monthly billing**:
An agreement to invoice one Client the same Line items every Billing period until it is stopped.
_Avoid_: Subscription, recurring invoice, plan

**Billing period**:
One month of Monthly billing, from its start day to the day before that day next month (e.g. Nov 15 – Dec 14); its Invoice is created, sent and emailed on the first day.
_Avoid_: Cycle, term

**Line item**:
One priced entry on an Invoice: description, quantity and unit price.
_Avoid_: Item, row

**Payment**:
Money received against one Invoice, never more than that Invoice's Balance; a card convenience fee is noted on the Payment, not counted toward the Invoice.
_Avoid_: Transaction, deposit (a deposit is a Line item)

**Balance**:
What the Client still owes on an Invoice: its total minus its Payments.
_Avoid_: Amount due, outstanding

## Invoice status

**Draft**:
An Invoice the Client cannot open yet.
_Avoid_: Unsent

**Sent**:
An Invoice the Client can open and pay, with no Payments yet.
_Avoid_: Open, issued

**Partly paid**:
A Sent Invoice with at least one Payment and a Balance above zero.
_Avoid_: Partial

**Paid**:
A Sent Invoice with a Balance of zero, including an Invoice whose total is $0.
_Avoid_: Settled, closed

**Void**:
A cancelled Invoice with no Payments.
_Avoid_: Cancelled, deleted

**Overdue**:
A Sent or Partly paid Invoice from the day after its due date (Toronto time); a view of the status, never stored.
_Avoid_: Late

## People

**Admin**:
An account that can do everything, including settings, accounts and deleting Draft or Void Invoices.
_Avoid_: Owner, superuser

**Studio Manager**:
An account that works with every Invoice but cannot change settings, accounts or delete Invoices.
_Avoid_: Staff, manager

**Advertising**:
An account that works only with the Invoices it created or that were Shared with it, and cannot remove Payments.

**Shared**:
An Invoice an Admin or Studio Manager has opened to chosen Advertising accounts, who then work on it like their own.
_Avoid_: Assigned, delegated
_Avoid_: Artist, sales
