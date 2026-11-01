"""Response shapes of the suggestions endpoint."""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Status = Literal["overdue", "due_soon", "not_yet", "unknown"]
Confidence = Literal["none", "low", "medium", "high"]


class SuggestionRead(BaseModel):
    product_id: int
    product_name: str
    last_purchase_date: date | None
    typical_gap_days: int | None
    expected_date: date | None
    days_until: int | None          # negative = overdue; worked out from TODAY
    status: Status                  # worked out from TODAY
    confidence: Confidence
    gaps_used: int


class SuggestionsRead(BaseModel):
    generated_at: datetime
    as_of_date: date
    dataset_run_id: str
    stale: bool                     # the file is older than the configured limit
    suggestions: list[SuggestionRead]