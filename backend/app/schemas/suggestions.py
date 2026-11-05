"""Response shapes of the suggestions endpoint."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["overdue", "due_soon", "not_yet", "unknown"]
ScopeType = Literal["household", "user"]
HistoryStrength = Literal["none", "short", "moderate", "long"]


class SuggestionRead(BaseModel):
    scope_type: ScopeType           # whose rhythm this is: a household or the user alone
    scope_id: int
    product_id: int
    product_name: str
    last_purchase_date: date | None
    typical_gap_days: int | None
    expected_date: date | None
    days_until: int | None          # negative = overdue; worked out from TODAY
    status: Status                  # worked out from TODAY
    # How much history stands behind the date. NOT a probability of being right.
    history_strength: HistoryStrength
    gaps_used: int
    # Worked out from the user's CURRENT lists (not from the published file):
    already_on_list: bool = False   # an item of this product is waiting to be bought
    on_list_id: int | None = None   # the list it is on (the oldest such item's list)


class SuggestionsRead(BaseModel):
    generated_at: datetime
    as_of_date: date
    dataset_run_id: str
    stale: bool                     # the file is older than the configured limit
    suggestions: list[SuggestionRead]
