"""Serving the suggestions that the data pipeline published.

LAYERS
------
This module reads ONE JSON file. It does not import the ML or data-engineering
packages and does not query their sources: the file is the whole contract
(its shape is documented in ml/suggest.py). That keeps the backend usable
even if the ML layer is rewritten, as long as the file keeps its shape.

WHAT IS WORKED OUT HERE
-----------------------
`allowed_scopes` and `waiting` come from the caller. The file holds the
suggestions of EVERY scope; a user may see only their own personal scope and
their household's, so everything else is dropped here before anything is
returned (a test checks that another family's data never leaks).
`waiting` (scope -> product id -> list id) describes the user's current lists,
which change all day, so it is never read from the file.

The file holds the stable facts (expected DATE, typical gap, history_strength,
which only says how much history there was, never how likely the guess is).
`days_until` and `status` depend on today, so they are computed per request.
The rule below intentionally repeats ml.suggest.status_for (a tiny function is
cheaper than a shared package); a unit test fails if the two ever disagree.
"""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from pydantic import BaseModel, ValidationError

from backend.app.schemas.suggestions import SuggestionRead, SuggestionsRead

SUPPORTED_SCHEMA_VERSION = 2      # v2: scope fields, `confidence` renamed `history_strength`
SOON_DAYS = 2


class SuggestionsUnavailableError(Exception):
    """No usable suggestions file (missing, unreadable, or an unknown format)."""


class _Dataset(BaseModel):
    run_id: str


class _Entry(BaseModel):
    scope_type: str
    scope_id: int
    product_id: int
    product_name: str
    last_purchase_date: date | None
    typical_gap_days: int | None
    expected_date: date | None
    history_strength: str
    gaps_used: int


class _File(BaseModel):
    schema_version: int
    generated_at: datetime
    as_of_date: date
    dataset: _Dataset
    suggestions: list[_Entry]


def status_for(days_until: int) -> str:
    if days_until < 0:
        return "overdue"
    return "due_soon" if days_until <= SOON_DAYS else "not_yet"


def load_suggestions(
    path: Path,
    *,
    today: date,
    now: datetime,
    stale_after_days: int,
    allowed_scopes: set[tuple[str, int]],
    waiting: dict[tuple[str, int], dict[int, int]] | None = None,
) -> SuggestionsRead:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        parsed = _File.model_validate(raw)
    except (OSError, ValueError, ValidationError) as exc:
        # ValueError covers bad JSON; ValidationError covers a wrong shape.
        raise SuggestionsUnavailableError(exc.__class__.__name__) from exc

    if parsed.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise SuggestionsUnavailableError(
            f"unsupported schema_version {parsed.schema_version}"
        )

    waiting = waiting or {}
    items: list[SuggestionRead] = []
    for entry in parsed.suggestions:
        scope = (entry.scope_type, entry.scope_id)
        if scope not in allowed_scopes:
            continue                      # another family's data: never returned
        if entry.expected_date is None:
            days_until, status = None, "unknown"
        else:
            days_until = (entry.expected_date - today).days
            status = status_for(days_until)
        items.append(SuggestionRead(
            **entry.model_dump(), days_until=days_until, status=status,
            already_on_list=entry.product_id in waiting.get(scope, {}),
            on_list_id=waiting.get(scope, {}).get(entry.product_id),
        ))

    # Most urgent first (most overdue = smallest days_until); unknown last.
    items.sort(key=lambda s: (s.days_until is None, s.days_until or 0, s.product_name,
                              s.scope_type, s.scope_id))

    generated_at = parsed.generated_at
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=timezone.utc)
    return SuggestionsRead(
        generated_at=generated_at,
        as_of_date=parsed.as_of_date,
        dataset_run_id=parsed.dataset.run_id,
        stale=now - generated_at > timedelta(days=stale_after_days),
        suggestions=items,
    )
