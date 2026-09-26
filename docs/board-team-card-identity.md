# Identifying each held chance card

`card_id` identifies a card type, such as `card_extra_roll`. `team_card_id` is the UUID of one `TeamChanceCard` row. Two draws of the same type have different UUIDs. The existing UUID primary key is reused; no migration is required.

## Responses

These responses now include `team_card_id` alongside the existing card type:

- `POST /api/v1/board/chance/now`
- `GET /api/v1/board/me`: each entry in `chance_cards`
- `POST /api/v1/board/dice/roll`: `usable_chance_card`, when present
- `POST /api/v1/board/chance/use`
- `POST /api/v1/board/chance/confirm`

Discard responses contain `discarded_team_card_id` and `kept_team_card_id`, in addition to the existing type fields.

## Requests

Send the selected copy's UUID for both use and discard, with the existing `Idempotency-Key` header:

```json
{"team_card_id": "018f3f1e-0100-7a91-a30b-630000000001"}
```

Card-specific arguments, such as `offset` or `destination_index`, remain unchanged. `card_id` may be included, but must match the selected copy's type. Older clients may still send only `card_id`; use selects an unused copy and pins that row before waiting for the board lock. New clients should send `team_card_id` so identical cards can be selected explicitly.

Invalid UUID values return `400 INVALID_REQUEST`. An unknown UUID, another team's card, a discarded card or a mismatched card type returns `404 CHANCE_CARD_NOT_FOUND` for use. An already-used UUID returns `409 CHANCE_CARD_ALREADY_USED` and never falls through to another unused copy. Discarding requires two held cards; the selected UUID must belong to that held set. Identifiers and permissions do not grant access to another team's cards.

The existing inventory rule still requires discarding one card before use when two cards are held. Identical cards drawn sequentially can each be used once. Existing data where a newer copy is used and an older copy remains held is also supported. Requests to discard different copies are serialized on the team's board state so simultaneous requests cannot discard both cards.
