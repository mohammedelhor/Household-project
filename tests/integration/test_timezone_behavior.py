from datetime import timezone
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from backend.app.db.models import User


@pytest.mark.integration
def test_database_created_at_is_timezone_aware(
    test_engine,
):
    SessionFactory = sessionmaker(
        bind=test_engine,
        autoflush=False,
        autocommit=False,
    )

    email = f"timezone-{uuid4().hex}@example.com"

    try:
        with SessionFactory() as session:
            user = User(
                email=email,
                password_hash="test-hash",
            )

            session.add(user)
            session.commit()

            user_id = user.id

        with SessionFactory() as session:
            created_at = session.scalar(
                select(User.created_at).where(
                    User.id == user_id
                )
            )

        assert created_at is not None
        assert created_at.tzinfo is not None
        assert created_at.utcoffset() is not None

        cairo_time = created_at.astimezone(
            ZoneInfo("Africa/Cairo")
        )

        utc_time = created_at.astimezone(timezone.utc)

        assert cairo_time.tzinfo is not None
        assert utc_time.tzinfo is not None

        assert cairo_time.astimezone(timezone.utc) == utc_time

    finally:
        with SessionFactory() as session:
            session.query(User).filter(
                User.email == email
            ).delete(
                synchronize_session=False
            )
            session.commit()