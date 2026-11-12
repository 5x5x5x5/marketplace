# Diagrams

Four pictures for people who need to understand the marketplace without
reading the code. Read them in order; each is a standalone SVG.

Every dollar figure comes from the demo run (`uv run python scripts/demo.py`):
a $20.00 job, a $14.00 payout, a $6.00 spread.

For what the product looks like in a browser, see
[`../screenshots/`](../screenshots/).

## 1. One job, two prices

The business model. The buyer and the seller each see only their own number;
the platform sees both and keeps the difference.

![One job, two prices](01-one-job-two-prices.svg)

## 2. The journey of one job

Every state a job can be in, drawn as a route map. A job starts one of two
ways: dispatch (the platform picks the seller) or browse (the buyer picks a
listing). Both then follow the same line, with the same ways off it.

![The journey of one job](02-job-journey.svg)

## 3. Follow the money

Who holds the money after each step, and how it moves back when a job is
cancelled, disputed or charged back.

![Follow the money](03-follow-the-money.svg)

## 4. Inside the service

The parts of the program and what each one is for, with the file names for
anyone who wants to go and look.

![Inside the service](04-inside-the-service.svg)
