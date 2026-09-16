# Board score and idempotency completion criteria

Issues [#30](https://github.com/MSG-CTF/msg-backend/issues/30) and [#32](https://github.com/MSG-CTF/msg-backend/issues/32) describe gaps addressed by merged [PR #29](https://github.com/MSG-CTF/msg-backend/pull/29). This follow-up verifies their complete public API flows and records the existing retention policy.

## #30: submission and ranking

`apps/challenge/test_board_submission_contract.py` starts with authenticated candidate lookup and problem opening, then submits the correct flag and reads team information, ranking and leaderboard. It checks the historical earned score, current dynamic score, mileage, cleared access and extra dice, including duplicate submission without duplicate rewards. Its fault-injection test verifies that a scoring failure rolls back the board access, dice, recharge deadline, solve, score and mileage together, and that a retry succeeds.

Existing `apps/challenge/tests.py` additionally verifies recalculation of previous solvers' team scores, concurrent submissions and board/scoring lock ordering. `Solve.earned_score` preserves the score at submission; rankings use current challenge scores. Stored `Team.team_score` is the Jeopardy subtotal. Public team totals add KOTH and signature points consistently.

## #32: durable request replay

`apps/board/test_idempotency_retention.py` expires the cached response and ages the DB record by 30 days. The same request still returns the original complete response without another roll or deduction. A different body with the retained key is rejected, even after cache expiry.

Existing tests in `test_dice_resilience.py`, `test_idempotency_restart.py` and `tests.py` cover cache read/write failures, an actual unavailable Redis connection, termination before and after DB commit, simultaneous requests, and body conflicts. The DB's unique user/method/path/key scope and transaction protect gameplay changes and the saved response together.

Redis's 300-second TTL applies only to the response cache. DB request records do not automatically expire during game-data retention. No cleanup job deletes these records. Keep them for the full lifetime of the corresponding contest data; do not delete request records alone while those game actions can still be retried. Reset/archive them only together with the related contest data. A rolled-back transaction leaves neither gameplay changes nor a completed response record, so retrying executes the action once.
