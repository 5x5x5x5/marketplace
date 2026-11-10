"""Domain services: the marketplace's logic, callable from any front end.

Each module takes a SQLAlchemy `Session` (and a `PaymentProvider` where money
moves) plus plain arguments, and returns ORM rows or view models. None of
them imports FastAPI or Starlette, the routers, `api` or `pages`: the JSON
routers (`marketplace.routers`) and the server-rendered pages both call down
into these, never the reverse. Expected failures are raised as
`marketplace.errors.DomainError`; `api.py` maps those to HTTP.

Transactions stay with the caller. A service flushes when the caller needs
server-generated values, and commits only where a comment explains why it
must persist ahead of an error response.
"""
