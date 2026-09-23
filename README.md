# Stockroom

A system that keeps track of a warehouse's supplies: who took what, what is running low, and
what was bought, so the team stops running out of things.

![Dashboard](docs/screenshots/dashboard.png)

## The problem

Before studying computer science, I worked in a parcel warehouse in Montreal. Nobody tracked
supplies there. Packing materials, labels, pens and office supplies were simply taken when
someone needed them.

- **We only found out something was gone when someone reached for it.** That happened about
  once a week.
- **Every week, someone had to drive to Costco or another store** to replace whatever had run
  out, usually in a hurry.
- **Nobody could say who had used what, or what we would need next,** because nothing was
  ever counted.
- **Receipts for those last-minute purchases** ended up in pockets and chat messages, which
  made paying people back slow.

After my internship, I built the system I wish that warehouse had.

## Who it's for

Small and mid-sized warehouses and logistics teams, with three kinds of users:

- **Employees** take supplies and get reimbursed for things they buy.
- **Supervisors** run the stockroom and decide what to order.
- **Managers** set the rules, pay out reimbursements and review the history.

## What you can do with it

### As an employee

1. **Take an item by scanning it.** Open "Issue stock", scan the barcode with your camera,
   and enter how many you're taking and for which team. The count updates right away.
2. **Get paid back.** Bought something urgent yourself? Submit the receipt and follow its
   status until it is paid.

![Issue stock](docs/screenshots/issue-stock.png)

### As a supervisor

1. **See what is running low before it runs out.** Stockroom looks at how fast each item
   was used over the last 90 days and tells you how many days of stock are left and how much
   to order.
2. **Pick the best supplier.** Suppliers are ranked by price, delivery time and rating.
3. **Ask the assistant.** Ask "What should I reorder this week?" The AI assistant suggests
   specific orders and explains why. You review each one before anything is ordered.
4. **Get warned about price increases.** If a supplier raises a price past the limit you set,
   you get an alert.

![Procurement](docs/screenshots/procurement.png)

### As a manager

1. **Approve and pay reimbursements,** with the receipt attached to each one.
2. **See the full history.** Every item taken, every purchase and every approval is recorded
   with who did it and when, and can be searched or exported.

![Audit log](docs/screenshots/audit-log.png)

## Why it works this way

- **Scanning takes seconds.** If recording an item is slower than just grabbing it, people
  won't bother. A barcode scan is fast enough that they actually do it.
- **Reorder suggestions come from real usage, not guesses.** Every number can be traced back
  to actual records, so a supervisor can see why the system says to order a certain amount.
- **The AI can only suggest, never buy.** Spending money needs a person's approval. Each
  suggestion is also checked against real stock levels and real suppliers first.
- **Approved receipts can't be swapped.** Once a receipt is approved, the system keeps that
  exact file, so nobody can replace it with a different one afterwards.

## What it achieves

- **Never gives out stock it doesn't have.** If 10 items are left and 20 people request them at
  the same moment, exactly 10 requests go through and the other 10 are told there isn't
  enough. I tested this against a real database.
- **Nothing goes unrecorded.** A change and its history record are saved together, so the
  record can never be missing.
- **Stays fast when busy.** With 50 people using it at once, it handled 163 requests per
  second with zero errors, and the stock counts stayed correct.
- **Thoroughly tested.** 289 automated tests cover 95% of the code, and every change is
  checked automatically before it goes in.

## About this project

This is a personal portfolio project, not a product in use. There are no real customers
behind it yet.

---

**For engineers:** architecture, design decisions, test details and how to run it locally are in
[TECHNICAL.md](TECHNICAL.md). Built with Python, FastAPI and PostgreSQL.
