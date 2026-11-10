# Seeded-bug evaluation: results 2026-11-10

Repo commit: `8d3508005eab870edbafc637cb6872dae3b73bd6`. Postgres: yes. Semgrep: uvx semgrep==1.184.0 --config .semgrep/rules src scripts.

Test columns exclude `tests/test_architecture.py`, which is its own column. A test layer counts as catching a case when at least one test that passes on the unmodified tree fails on the mutant. The reviewer column is graded against the rubric in each case file; raw reviewer output sits next to each verdict. See `evals/README.md` for what this does and does not show.

## Summary

| Layer | Bugs caught |
|---|---|
| Tests on SQLite | 10 / 16 |
| Tests on Postgres | 12 / 16 |
| Tests (either backend) | 12 / 16 |
| Architecture tests | 4 / 16 |
| Semgrep rules | 1 / 16 |
| Reviewer agent (caught; partial not counted) | 16 / 16 |

Overlap below counts these layers: tests, architecture, semgrep, reviewer.

- Caught by exactly one layer: `asym-cancel-notice-shared-payload` (reviewer), `money-charge-key-per-offer` (reviewer)
- Caught by no layer: none
- Reviewer partial catches: none
- False positives on controls: none

## Cases

| Case | Kind | Class | Tests SQLite | Tests PG | Arch | Semgrep | Reviewer |
|---|---|---|---|---|---|---|---|
| `asym-availability-seller-id-in-body` | bug | asymmetry/identity-from-request | missed | missed | **caught** | **caught** | **caught** |
| `asym-cancel-notice-shared-payload` | bug (synthetic) | asymmetry/notification-payload | missed | missed | missed | missed | **caught** |
| `asym-f1-seller-receipt-ledger-model` | bug | asymmetry/wrong-view-model | **caught** | **caught** | **caught** | missed | **caught** |
| `asym-seller-review-buyer-id` | bug | asymmetry/forbidden-field-in-role-view | **caught** | **caught** | missed | missed | **caught** |
| `ctrl-cancel-shared-tail` | control | control/refactor | clean | clean | clean | clean | **clean** |
| `ctrl-offer-view-responded-at` | control | control/new-field-right-view | clean | clean | clean | clean | **clean** |
| `ctrl-rename-superseded-helper` | control | control/rename | clean | clean | clean | clean | **clean** |
| `ctrl-stale-sweep-ordered` | control | control/money-path-correct-change | clean | clean | clean | clean | **clean** |
| `money-a1-cancel-blocking-offer-lock` | bug | money-path/lock-order | missed | **caught** | missed | missed | **caught** |
| `money-a2-full-clawback-reads-as-failure` | bug | money-path/webhook-mapping | **caught** | **caught** | missed | missed | **caught** |
| `money-charge-key-per-offer` | bug (synthetic) | money-path/idempotency-key-derivation | missed | missed | missed | missed | **caught** |
| `money-dispute-pin-not-committed` | bug | money-path/mid-handler-commit | **caught** | **caught** | missed | missed | **caught** |
| `money-f2-commit-in-teardown` | bug | money-path/commit-ordering | **caught** | **caught** | **caught** | missed | **caught** |
| `money-f5-admin-cancel-stale-relock` | bug | money-path/stale-relock | missed | **caught** | missed | missed | **caught** |
| `money-floor-gross-not-net` | bug | money-path/margin-floor | **caught** | **caught** | missed | missed | **caught** |
| `money-late-success-resurrects-void` | bug | money-path/resurrection | **caught** | **caught** | missed | missed | **caught** |
| `money-payout-retry-replays-reversed-key` | bug | money-path/idempotency-key-reuse | **caught** | **caught** | missed | missed | **caught** |
| `ops-pagination-in-python` | bug | operational/pagination | **caught** | **caught** | missed | missed | **caught** |
| `ops-retention-reaps-failed-webhooks` | bug | operational/retention | **caught** | **caught** | missed | missed | **caught** |
| `ops-webhook-db-on-event-loop` | bug | operational/event-loop-blocking | missed | missed | **caught** | missed | **caught** |

## Detail

### `asym-availability-seller-id-in-body`

- Origin: SECURITY.md safe-to-pilot sweep C3/C4 (seller identity taken from caller-supplied input instead of the token); the class behind the 'identity comes from the principal' rule.
- Where: src/marketplace/models.py: AvailabilityRequest; src/marketplace/services/profiles.py: post_availability
- Reviewer: `asymmetry-reviewer` (in-scope); packet `cases/asym-availability-seller-id-in-body/packet/prompt.md` (code rdcc896)
- SQLite: nothing
- Postgres: nothing
- Architecture: `tests/test_architecture.py::test_request_bodies_never_carry_buyer_or_seller_id`
- Semgrep: `marketplace-request-model-identity-field src/marketplace/models.py:665`
- Reviewer review/run-1.md: caught: Critical at AvailabilityRequest.seller_id / post_availability: 'Availability body `seller_id` overrides the authenticated seller (impersonation)', with the write landing on the victim's availability.

### `asym-cancel-notice-shared-payload`

- Origin: Synthetic: no historical instance. The rule it breaks is the notifications design's role-safe payload rule (CLAUDE.md 'Notifications are enqueue-only'; asymmetry-reviewer item 5 names exactly this shape).
- Where: src/marketplace/services/jobs.py: _notify_cancelled
- Reviewer: `asymmetry-reviewer` (in-scope); packet `cases/asym-cancel-notice-shared-payload/packet/prompt.md` (code r8373cc)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Important at _notify_cancelled: one snapshot dict is enqueued for both roles, so 'the seller's row stores the buyer price ... the buyer's row stores the seller payout'. Names which amount reaches which party.

### `asym-f1-seller-receipt-ledger-model`

- Origin: F1 (docs/tryout-findings-2026-07-16.md; SECURITY.md findings history). Fixed in 12309c6 'Fix seller receipt leak (F1) and cancel-vs-complete double-pay (F5)'.
- Where: src/marketplace/routers/seller.py: complete_job (POST /v1/seller/jobs/{job_id}/complete)
- Reviewer: `asymmetry-reviewer` (in-scope); packet `cases/asym-f1-seller-receipt-ledger-model/packet/prompt.md` (code rb73eb4)
- SQLite: `tests/test_end_to_end.py::test_full_flow_books_correct_margin`, `tests/test_information_asymmetry.py::test_completion_receipt_is_seller_scoped`
- Postgres: `tests/test_end_to_end.py::test_full_flow_books_correct_margin`, `tests/test_information_asymmetry.py::test_completion_receipt_is_seller_scoped`
- Architecture: `tests/test_architecture.py::test_role_routes_do_not_return_the_other_sides_fields[seller-forbidden1]`
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at routers/seller.py complete_job: 'Seller completion receipt returns admin TransactionOut (buyer price and margin leak to every seller)'.

### `asym-seller-review-buyer-id`

- Origin: 95a459a 'Fix wave: withhold buyer identity from sellers, guard PI-less chargebacks' (SellerReviewOut was the first seller-facing view to leak buyer_id).
- Where: src/marketplace/models.py: SellerReviewOut (response of POST /v1/seller/jobs/{job_id}/review)
- Reviewer: `asymmetry-reviewer` (in-scope); packet `cases/asym-seller-review-buyer-id/packet/prompt.md` (code r4c36be)
- SQLite: `tests/test_seller_reviews.py::test_seller_reviews_buyer_happy_path_and_aggregate`
- Postgres: `tests/test_seller_reviews.py::test_seller_reviews_buyer_happy_path_and_aggregate`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at SellerReviewOut: 'Seller review receipt now returns buyer_id to the seller', letting a seller correlate buyers across jobs.

### `ctrl-cancel-shared-tail`

- Origin: Control: a behavior-preserving refactor of both cancel paths (same lock order, same locked re-reads, same provider calls).
- Where: src/marketplace/services/jobs.py: cancel_job, admin_cancel_job, new _cancel_locked
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/ctrl-cancel-shared-tail/packet/prompt.md` (code rc59423)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: clean: 'No findings.' The reviewer traced lock order, the re-check and the moved lines on both cancel paths.

### `ctrl-offer-view-responded-at`

- Origin: Control: a legitimate new field on the seller's own offer view (the seller's own response time; no counterparty data).
- Where: src/marketplace/models.py: SellerOfferView
- Reviewer: `asymmetry-reviewer` (in-scope); packet `cases/ctrl-offer-view-responded-at/packet/prompt.md` (code rcffd1a)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: clean: 'No findings.' A timestamp on the seller's own offer row; no money or buyer identity.

### `ctrl-rename-superseded-helper`

- Origin: Control: a harmless rename of a private helper in the webhook applier.
- Where: src/marketplace/services/webhooks.py: _superseded -> _newer_event_applied
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/ctrl-rename-superseded-helper/packet/prompt.md` (code r0b7a9e)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: clean: 'No findings.' Pure rename, both references updated.

### `ctrl-stale-sweep-ordered`

- Origin: Control: the fix shape the money-path audit proposed for deferred item D6 (stale-payment sweep iterates without an order).
- Where: src/marketplace/services/maintenance.py: _sweep_stale_payments
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/ctrl-stale-sweep-ordered/packet/prompt.md` (code r7d50ec)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: clean: 'No findings.' The added ordering changes no lock, guard or provider call.

### `money-a1-cancel-blocking-offer-lock`

- Origin: A1 (docs/money-path-audit-2026-11.md). Fixed in 7287d30 'Skip offers locked by accept/decline/sweep when a cancel expires them'.
- Where: src/marketplace/services/offers.py: expire_open_offers (called by cancel_job and admin_cancel_job)
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-a1-cancel-blocking-offer-lock/packet/prompt.md` (code rc44ba8)
- SQLite: nothing
- Postgres: `tests/test_money_path_audit.py::test_cancel_vs_accept_never_deadlocks[admin]`, `tests/test_money_path_audit.py::test_cancel_vs_accept_never_deadlocks[buyer]`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at expire_open_offers: 'Blocking offer lock under the Job lock reintroduces an ABBA deadlock with accept, decline and the sweep', with the interleaving spelled out.

### `money-a2-full-clawback-reads-as-failure`

- Origin: A2 (docs/money-path-audit-2026-11.md). Fixed in b63c870 'Keep a payout PAID when a full dispute clawback reverses its transfer'.
- Where: src/marketplace/services/webhooks.py: _apply_payment_event, transfer_failed branch
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-a2-full-clawback-reads-as-failure/packet/prompt.md` (code r7e25b6)
- SQLite: `tests/test_money_path_audit.py::test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry`, `tests/test_money_path_audit.py::test_full_clawback_reversal_event_does_not_fail_the_payout`, `tests/test_money_path_audit.py::test_full_clawback_reversal_event_twice_and_late`
- Postgres: `tests/test_money_path_audit.py::test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry`, `tests/test_money_path_audit.py::test_full_clawback_reversal_event_does_not_fail_the_payout`, `tests/test_money_path_audit.py::test_full_clawback_reversal_event_twice_and_late`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at the transfer_failed branch: a full dispute clawback 'marks the payout FAILED and an admin retry pays the seller again', traced through the adapter mapping and retry_payout.

### `money-charge-key-per-offer`

- Origin: Synthetic: no historical instance. Derived from the money-path audit's retry table (accept_offer row: after a crash between charge and commit, the next accept, possibly by another seller after re-match, must replay the same key 'because it is derived from the job').
- Where: src/marketplace/services/jobs.py: accept_offer
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-charge-key-per-offer/packet/prompt.md` (code r8f839b)
- SQLite: nothing
- Postgres: nothing
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Important at accept_offer: 'Charge key now varies per offer, so a re-offered job can charge the buyer twice', with the first PaymentIntent orphaned. The reviewer rated its own confidence medium.

### `money-dispute-pin-not-committed`

- Origin: 32a6f31 'Close disputes final-review findings: partial-reversal mapping, pinned resolve retries, ...' and 51252f1; the pin-and-commit precedent in CLAUDE.md. Also the precondition A2's fix relies on (b63c870).
- Where: src/marketplace/services/disputes.py: resolve_dispute
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-dispute-pin-not-committed/packet/prompt.md` (code re0c0f3)
- SQLite: `tests/test_disputes.py::test_concurrent_duplicate_resolve_cannot_double_book`, `tests/test_disputes.py::test_resolve_pins_amounts_across_a_502_retry`, `tests/test_money_path_audit.py::test_dispute_retry_converges_when_the_full_reversal_event_lands_mid_retry`
- Postgres: `suite timed out`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at resolve_dispute: 'Pinned dispute amounts no longer survive a provider 502, so a retry can book different amounts than the provider moved'.
- Follow-up notes: `cases/money-dispute-pin-not-committed/notes.md`

### `money-f2-commit-in-teardown`

- Origin: F2 (docs/tryout-findings-2026-07-16.md). Fixed by the commit-before-response branch (docs/superpowers/plans/2026-07-16-commit-before-response.md).
- Where: src/marketplace/db.py: get_session and CommitRoute.get_route_handler
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-f2-commit-in-teardown/packet/prompt.md` (code rea6980)
- SQLite: `tests/test_live_server.py::test_commit_lands_before_client_has_response`, `tests/test_live_server.py::test_failed_commit_returns_500_envelope_and_persists_nothing`, `tests/test_live_server.py::test_read_your_writes_over_socket`, `tests/test_live_server.py::test_same_key_immediate_retry_replays_byte_identical`
  - did not fail again on rerun: `tests/test_live_server.py::test_read_your_writes_over_socket`
- Postgres: `tests/test_live_server.py::test_commit_lands_before_client_has_response`, `tests/test_live_server.py::test_failed_commit_returns_500_envelope_and_persists_nothing`, `tests/test_live_server.py::test_read_your_writes_over_socket`, `tests/test_live_server.py::test_same_key_immediate_retry_replays_byte_identical`
- Architecture: `tests/test_architecture.py::test_get_session_teardown_does_not_commit`
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at db.get_session: the commit now runs in teardown after the response is sent, so a chained request can read pre-write state; a second Critical covers a failed commit behind a 2xx. Confidence stated as medium (not run).

### `money-f5-admin-cancel-stale-relock`

- Origin: F5 (docs/tryout-findings-2026-07-16.md). Fixed in 12309c6 'Fix seller receipt leak (F1) and cancel-vs-complete double-pay (F5)'.
- Where: src/marketplace/services/jobs.py: admin_cancel_job
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-f5-admin-cancel-stale-relock/packet/prompt.md` (code r7d48ed)
- SQLite: nothing
- Postgres: `tests/test_payments.py::test_cancel_vs_complete_race`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at admin_cancel_job: the locked get returns the identity-map object, the guard reads stale ACCEPTED, and 'a cancel racing complete can refund the buyer and pay the seller'.

### `money-floor-gross-not-net`

- Origin: 28b5c33 'Make the margin floor fee-aware at both enforcement sites' (fee-aware margin phase; match-time test added in dd92407).
- Where: src/marketplace/matching.py: _priced (used by every matching strategy)
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-floor-gross-not-net/packet/prompt.md` (code r166e4d)
- SQLite: `tests/test_matching.py::test_floor_filters_candidate_fee_aware`
- Postgres: `tests/test_matching.py::test_floor_filters_candidate_fee_aware`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at matching._priced: 'Inlined floor check drops the fee estimate, so matching uses the gross spread', with a worked numeric example.

### `money-late-success-resurrects-void`

- Origin: 3c5d1af 'Never resurrect a voided payment: late success no-ops on dead jobs' (SECURITY.md later fixes: 'Late payment success').
- Where: src/marketplace/services/webhooks.py: _apply_payment_event, payment_succeeded branch
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-late-success-resurrects-void/packet/prompt.md` (code ra1bc59)
- SQLite: `tests/test_payments.py::test_late_success_never_resurrects_a_voided_cancel`, `tests/test_webhook_replay.py::test_payment_succeeded_late_after_buyer_cancel_never_resurrects`
- Postgres: `tests/test_payments.py::test_late_success_never_resurrects_a_voided_cancel`, `tests/test_webhook_replay.py::test_payment_succeeded_late_after_buyer_cancel_never_resurrects`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at the payment_succeeded branch: a late success 'marks a voided payment SUCCEEDED while the job stays dead, so the buyer is never refunded'.

### `money-payout-retry-replays-reversed-key`

- Origin: b41be67 'Apply final-review fix wave: ... reversed-transfer retry keys ...' (retry_payout must not replay the key of a reversed transfer).
- Where: src/marketplace/services/payouts.py: retry_payout
- Reviewer: `money-path-reviewer` (in-scope); packet `cases/money-payout-retry-replays-reversed-key/packet/prompt.md` (code r14480f)
- SQLite: `tests/test_payments.py::test_reversed_transfer_retry_forces_a_new_transfer`
- Postgres: `tests/test_payments.py::test_reversed_transfer_retry_forces_a_new_transfer`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Critical at retry_payout: 'replays the key of a reversed transfer and books PAID with no money moved'.

### `ops-pagination-in-python`

- Origin: e53860e 'Paginate the catalog and list endpoints in SQL' (SECURITY.md M7: pagination applied in SQL).
- Where: src/marketplace/services/common.py: paginate (every list endpoint)
- Reviewer: `spec-compliance-reviewer` (weak: no narrow reviewer lists this class; spec-compliance reviews it against the commit message and CLAUDE.md with no plan task); packet `cases/ops-pagination-in-python/packet/prompt.md` (code r201b0e)
- SQLite: `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/audit-admin-audit_log]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/buyers-admin-buyer_profiles]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/disputes-admin-disputes]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/jobs-admin-jobs]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/listings-admin-listings]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/notifications-admin-notifications]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/payouts-admin-payouts]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/reports-admin-reports]` and 6 more
- Postgres: `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/audit-admin-audit_log]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/buyers-admin-buyer_profiles]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/disputes-admin-disputes]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/jobs-admin-jobs]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/listings-admin-listings]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/notifications-admin-notifications]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/payouts-admin-payouts]`, `tests/test_capacity.py::test_list_endpoints_paginate_in_sql[/v1/admin/reports-admin-reports]` and 6 more
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: The spec-compliance reviewer's format has categories, not severities. Its top-category [Constraint] finding at services.common.paginate says it 'fetches every row and slices in Python ... exactly the banned pattern' and that 'memory and latency grow with table size on every page request'. Graded caught on substance; a strict reading of 'Critical or Important' would make this partial.

### `ops-retention-reaps-failed-webhooks`

- Origin: df4b9c0 'Add retention sweeps, move webhook DB work off the event loop' (PENDING outbox never reaped) and f8f54dc 'Merge webhook outbox: persisted failures, retry, dead-letter, replay-safe handlers' (FAILED webhook rows exempt).
- Where: src/marketplace/services/maintenance.py: _sweep_retention
- Reviewer: `money-path-reviewer` (partial: the reviewer lists webhook replay and outbox handling but not retention); packet `cases/ops-retention-reaps-failed-webhooks/packet/prompt.md` (code r6facd3)
- SQLite: `tests/test_webhook_outbox.py::test_retention_spares_failed_rows_and_reaps_terminal_ones`
- Postgres: `tests/test_webhook_outbox.py::test_retention_spares_failed_rows_and_reaps_terminal_ones`
- Architecture: nothing
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Important at _sweep_retention: 'Retention sweep deletes FAILED webhook rows that are still owed a retry', so the event is lost with no dead-letter row.

### `ops-webhook-db-on-event-loop`

- Origin: df4b9c0 'Add retention sweeps, move webhook DB work off the event loop'.
- Where: src/marketplace/routers/payments.py: payments_webhook
- Reviewer: `money-path-reviewer` (in-scope (item 7: DB work on the coroutine)); packet `cases/ops-webhook-db-on-event-loop/packet/prompt.md` (code r74c6ef)
- SQLite: nothing
- Postgres: nothing
- Architecture: `tests/test_architecture.py::test_async_webhook_handlers_offload_to_a_thread`
- Semgrep: nothing
- Reviewer review/run-1.md: caught: Important at payments_webhook: 'Sync webhook processor now runs on the event loop', stalling every in-flight request while it waits on the database.

## Baseline (unmodified tree)

- SQLite: 489 passed, 11 skipped, 12 warnings in 74.80s (0:01:14)
- Postgres: 500 passed, 12 warnings in 93.14s (0:01:33)
- Architecture: 36 passed in 2.82s
- Semgrep findings: 0
