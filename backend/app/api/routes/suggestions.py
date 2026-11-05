from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from backend.app.api.routes.auth import get_current_user
from backend.app.core.config import settings
from backend.app.db.database import get_db
from backend.app.db.models import User
from backend.app.schemas.suggestions import SuggestionsRead
from backend.app.services.suggestion_lists import products_waiting_on_lists, user_scopes
from backend.app.services.suggestions import (
    SuggestionsUnavailableError,
    load_suggestions,
)

router = APIRouter()


def get_now() -> datetime:
    """The current moment. A dependency so tests can fix the clock."""
    return datetime.now(timezone.utc)


@router.get("/suggestions", response_model=SuggestionsRead)
def read_suggestions(
    now: datetime = Depends(get_now),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user),
) -> SuggestionsRead:
    try:
        return load_suggestions(
            settings.suggestions_path,
            today=now.date(),            # UTC date, like every date in the data layer
            now=now,
            stale_after_days=settings.suggestions_stale_after_days,
            allowed_scopes=user_scopes(db, current_user.id),
            waiting=products_waiting_on_lists(db, current_user.id),
        )
    except SuggestionsUnavailableError:
        # No paths or parser details in the response: they are for the logs,
        # not for a client.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Suggestions are not available yet.",
        )
