# Board API alignment (2026-09-14)

Source: [Notion Board API](https://app.notion.com/p/3ab0f19c72be80ab9e57e3776b67323f).

- The 36-cell board uses START, CHALLENGE, CHANCE, AIRPORT, and ROULETTE.
- Cells 16 and 25 are roulette cells. Each awards 50/100/150/200 with equal probability, once per team per cell.
- The catalog contains five cards: reroll, roll twice and choose, move offset, free travel, and extra roll.
- Quarantine state, escape endpoints, and the two retired card effects have been removed.

## Consecutive dice rolls

Teams may spend all three stored rolls without waiting for the 15-minute challenge reward window or dice recharge. A challenge must still be selected on arrival before the next roll. The `timer_running` field reports the current challenge reward window independently of `can_roll`; it no longer produces a `TIMER_RUNNING` block. Empty dice, an unconfirmed roll, and board completion still block rolling. Further rolls preserve the existing recharge deadline, and opened challenges remain available after moving.

## Existing databases

Apply `python manage.py migrate` with the new release. Migration `board.0004_align_board_api_spec` updates cell 16 in place and translates pending landings on it. It also updates special-cell labels and the move-offset description.

Team positions, dice balances and recharge times, consumed cells, challenge access/solve records, and mileage are preserved. Retired card definitions and draw history remain for auditing; previously held retired cards are marked discarded and excluded from gameplay responses. No new card or dice reward is granted during conversion. Schema rollback does not reactivate these cards or restore deleted escape codes.

Do not run `seed_board` to update an existing game: that command resets board progress. Use it only when deliberately initializing a fresh demo board. Restart backend processes after applying the migration so their model definitions match the database.

## Verification

Run `python manage.py test apps.board apps.challenge apps.adminpanel`. Tests cover the fixed layout, both roulette cells and duplicate rewards, the five-card catalog and flows, removal of escape routes, and migration of an existing game. The migration test uses historical models and checks that team progress and mileage remain intact.
