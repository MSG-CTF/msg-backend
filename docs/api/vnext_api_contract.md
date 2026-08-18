# MSG CTF vNext API Contract

## Status

This document is the target API and data contract for the 2026 CTF platform.
It supersedes endpoint examples that conflict with the current board fixture or
with the security and persistence decisions below. It is a design contract,
not an implementation-complete claim: the repository currently exposes only
part of the auth API and the board domain base models.

## Global rules

- Public participant APIs use the `/api/v1` namespace and a bearer access token.
- The grader-only KOTH score ingestion endpoint is `/internal/koth/scores`.
  It does not accept a team access token. Authenticate it with an HMAC
  signature or a service API key, include a unique `request_id`, and persist
  the accepted event for replay protection.
- Requests that change one-time state use `Idempotency-Key`. Dice, chance draw,
  chance use, airport move, quarantine escape, roulette, payment approval, and
  KOTH score ingestion are idempotent.
- API JSON uses `snake_case`. Every response keeps the existing envelope:
  `{ "code": "SUCCESS", "message": "...", "data": ... }`.
- Never persist raw flags, raw refresh tokens, payment tokens, or QR tokens.
  Persist a secure hash only.
- Team identity comes from the access token. Do not accept a client supplied
  team name or team ID for a `/me` endpoint.

## Auth and team

- `POST /api/v1/auth/login`: accepts `login_id`, `password`; returns both
  `access_token` and `refresh_token`, user/team details, role, and
  `is_team_leader`.
- `POST /api/v1/auth/refresh`: rotates the refresh token.
- `POST /api/v1/auth/logout`: revokes the submitted refresh token and records
  the access-token JTI when supplied.
- `GET /api/v1/auth/me` and `GET /api/v1/teams/me`: return token-derived user
  and team state.
- `GET /api/v1/teams/me/solves`, `GET /api/v1/teams/me/mileage-history`, and
  `POST /api/v1/teams/me/qr-token` read from `challenge_solves`,
  `mileage_ledger`, and `qr_payment_tokens`.

## Board

The canonical board has 36 tiles: 30 `CHALLENGE` tiles plus two `CHANCE` tiles
and one each of `START`, `AIRPORT`, `QUARANTINE`, and `ROULETTE`. The committed
fixture places `START` at index `1`, `CHANCE` at `7` and `30`, `QUARANTINE` at
`16`, `AIRPORT` at `21`, and `ROULETTE` at `25`.
The active challenge timer is 900 seconds (15 minutes).

- `GET /api/v1/board`: returns the 36 global `board_tiles`.
- `GET /api/v1/board/me`: returns `team_board_states`, active access, owned
  chance cards, and opened/candidate challenges.
- `GET /api/v1/board/dice/status` and `POST /api/v1/board/dice/roll`: only a
  team leader may roll. Roll returns `dice_a`, `dice_b`, and `rolled_number`.
- `GET /api/v1/board/cell/current`: has no path parameter. It derives the
  current tile from the authenticated team's board state.
- `POST /api/v1/board/cell/open`: selects a server-provided candidate and
  creates one `team_challenge_accesses` record. The client does not send a tile
  index.
- Chance, airport, quarantine, and roulette endpoints use their respective
  event tables so each state transition remains auditable.
- Landing on a chance tile draws a card. Only cards whose policy permits a
  pre-roll effect may be used before a dice roll; a card is not chosen before
  every roll.
- `GET /api/v1/timer` returns `server_time`, contest time, and
  `next_dice_reset_at`. The client animates seconds locally and resynchronizes
  against server time; it does not send browser time as authority.

## Challenges and instances

- `GET /api/v1/challenges`, `GET /api/v1/challenges/{challenge_id}`, and
  `POST /api/v1/challenges/{challenge_id}/submit` read/write challenge access,
  solve, and submission records. A team can solve a challenge once.
- `POST /api/v1/instances`, `GET /api/v1/teams/me/instance`,
  `POST /api/v1/instances/{instance_id}/reset`,
  `DELETE /api/v1/instances/{instance_id}`, and
  `POST /api/v1/instances/{instance_id}/extend` operate on `challenge_instances`
  and append `instance_events`.
- A team has at most two active dynamic instances across all challenges. When a
  third instance is requested, issue a kill/termination command to the most
  recently started existing active instance, record it as `replaced_instance_id`,
  and then start the newest instance. The transition must be serialized per team
  so concurrent requests cannot exceed the limit.

## KOTH, ranking, and administration

- `GET /api/v1/koth/leaderboard`, `/clubs`, `/clubs/{club_id}`, and `/me` use
  `koth_clubs`, `koth_challenges`, `koth_rounds`, `koth_team_scores`, and
  `koth_occupations`.
- The functional-spec baseline is six clubs, with a four-hour KOTH round that
  exposes two challenges globally. `koth_round_challenges` makes the schedule
  explicit. If product policy changes to two challenges per club, only the
  round selection constraint changes; no score history needs migration.
- `POST /internal/koth/scores` only upserts the latest score after service
  authentication and stores a `koth_score_events` audit record. The grader
  sends the score timestamp and emits an event every fifteen minutes.
- `GET /api/v1/ranking` and `GET /api/v1/ranking/me` are the canonical team
  ranking routes. `/api/v1/leaderboard` is a deprecated alias and should not
  become a second data source.
- Admin ban/unban, mileage adjustment, instance monitoring, payment checkout,
  history, and refund are all recorded in `admin_audit_logs`,
  `mileage_ledger`, and `payment_transactions`.
- Ban, board move, manual dice grant, and clear-tile correction capture a
  `team_state_snapshots` record first. Restoring a snapshot is an explicit
  admin action, not an implicit side effect of banning or unbanning.
- Infrastructure capacity, node resource samples, forced instance actions, and
  recent operational events are stored in `infrastructure_nodes`,
  `node_resource_snapshots`, and `instance_events`.

## Leaderboard

- Team ranking and member ranking are separate response views over
  `team_rank_snapshots` and `user_rank_snapshots`; neither endpoint mixes the
  two. The top-three names and top-eight score-series are derived from the same
  snapshots, not persisted as a second leaderboard.

## ERD coverage from the prior CTF

| Prior entity | vNext table or tables |
| --- | --- |
| `TeamEntity` | `teams` |
| `UserEntity` | `users` |
| `RefreshEntity` | `refresh_tokens` |
| `BlacklistedTokenEntity` | `blacklisted_tokens` |
| `PaymentTokenEntity` | `qr_payment_tokens` |
| `TeamPaymentHistoryEntity` | `payment_transactions`, `mileage_ledger` |
| `ChallengeEntity` | `challenges` |
| `HistoryEntity`, `TeamHistoryEntity` | `challenge_solves` |
| `SubmissionEntity` | `flag_submissions` |
| `SignatureEntity` | `signatures` |
| `ChallengeSignaturePolicy` | `challenge_signature_policies` |
| `SignatureCodeEntity` | `signature_codes` |
| `TeamSignatureUnlockEntity` | `team_signature_unlocks` |
| `FileEntity` | `files`, `challenge_files` |
| `IPActivityEntity` | `ip_activities` |
| `IPBanEntity` | `ip_bans` |
| `ContestConfigEntity` | `contest_configs` |
| `LeaderboardEntity` | `team_rank_snapshots`, `user_rank_snapshots` |

The replacement model removes denormalized member and solved-challenge string
fields in favor of foreign keys and audit records. It adds board state, chance,
airport, quarantine, roulette, dynamic instances, QR payment, and KOTH tables
required by the current API contract.
