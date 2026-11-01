# Data dictionary: output of the cleaning step

Contract version: **1.0.0**. Generated from `data_engineering/contracts.py`; do not edit by hand (run `uv run python -m data_engineering.contracts`).

## What the data means (read this first)

- **Purchased means marked as purchased in the app.** It is not proof that the product was really bought, it is not the quantity in stock (inventory) and it is not the quantity used up (consumption).
- **An interval is the time between two such marks of the same product.** It is not how long the product lasted: people stockpile, buy early, or forget to tap. A model trained on it predicts the next purchase mark, so its output is a suggestion to add the product to the list, never a claim that the product ran out.
- **These tables show the FINAL state of every item (hindsight).** A purchase that was reverted later is absent, even for dates before the revert. Do not build training features straight from them: reconstruct what was known at each prediction time first (planned: as-of replay).
- **Dates are UTC dates.** A purchase late in the evening local time may fall on the next UTC date. This simplification is postponed on purpose.
- **An item with an inconsistent history is rejected as a whole** (see quarantine.csv); we cannot tell which of its events to believe.
- **CSV has no NULL:** an empty cell means no value.

## purchases.csv

One row per shopping-list item that is STILL marked as purchased at the end of its history (a purchase that was reverted does not appear).

Unique: `item_id`, `source_event_id`.

| column | type | required | rules | meaning |
|---|---|---|---|---|
| `item_id` | int | yes | - | Shopping-list item this purchase belongs to. |
| `product_id` | int | no | - | Catalog product; empty for free-text items. |
| `product_name` | text | no | - | Product name from the newest products snapshot; empty for free-text items. |
| `list_id` | int | yes | - | Shopping list the item was on. |
| `quantity` | decimal | yes | > 0; unit: see unit column | Quantity bought, in `unit`. |
| `unit` | text | yes | one of g, kg, liter, ml, piece | Unit of `quantity`. |
| `purchased_at` | datetime | yes | - | When the item was marked purchased (timezone-aware, UTC). It is the moment of the tap, not necessarily of the real purchase. |
| `purchase_date` | date | yes | unit: UTC date | UTC calendar date of `purchased_at`. |
| `source_event_id` | int | yes | - | Id of the item_events row that recorded the purchase: lets any row be traced to the source. |

## purchase_intervals.csv

Days between consecutive purchases of the same product (items with no catalog product are skipped). Rows are grouped by product and, inside a product, ordered by time. The quality gate recomputes every row from purchases.csv and rejects any difference.

| column | type | required | rules | meaning |
|---|---|---|---|---|
| `product_id` | int | yes | - | Catalog product. |
| `product_name` | text | yes | - | Product name from the newest snapshot. |
| `previous_purchase_date` | date | yes | unit: UTC date | UTC date of the earlier purchase. |
| `purchase_date` | date | yes | unit: UTC date | UTC date of the later purchase. |
| `days_since_previous` | int | yes | >= 0; unit: days | Whole days between the two dates (0 = bought twice on the same date). |

## quarantine.csv

Events that were NOT used, with the reason. Nothing is dropped silently; a person can review this file.

| column | type | required | rules | meaning |
|---|---|---|---|---|
| `event_id` | int | no | - | Id of the rejected event; empty if the row could not even be read. |
| `item_id` | int | no | - | Item whose whole history was rejected; empty if unknown. |
| `reason` | text | yes | - | Why the event/item was rejected. |