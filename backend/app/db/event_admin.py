"""The explicit administrative path for correcting `item_events`.

The table is append-only in the database itself (see the migration
c4d8f2a61b73). The only way to change or delete an event is to say so, inside
the transaction that does it:

    with Session(engine) as session:
        allow_event_correction(session)         # valid until this transaction ends
        session.execute(delete(ItemEvent)...)
        session.commit()

Use it for test cleanup and synthetic data, or for a reviewed manual
correction. Normal application code must NEVER call it: a wrong event is
fixed by appending a new one.
"""

from sqlalchemy import text
from sqlalchemy.orm import Session

FLAG = "household.allow_event_correction"


def allow_event_correction(session: Session) -> None:
    """Turn the flag on for the CURRENT transaction only (PostgreSQL).

    `set_config(..., true)` is the transaction-local form of SET: when the
    transaction commits or rolls back the flag is gone, so it cannot be left
    on by accident. Other databases (the SQLite used by unit tests) have no
    such trigger and need nothing.
    """
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("SELECT set_config(:flag, 'on', true)"), {"flag": FLAG})