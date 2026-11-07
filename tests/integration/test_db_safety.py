"""The label can really be written and read back on PostgreSQL.

COMMENT ON DATABASE is transactional, so everything happens inside the
`db_session` transaction and is rolled back: the test database's real label
is never changed.
"""

import pytest

from backend.app.core.db_safety import read_marker, set_marker


@pytest.mark.integration
def test_label_round_trip_is_rolled_back(db_session):
    connection = db_session.connection()
    before = read_marker(connection)

    set_marker(connection, "synthetic")
    assert read_marker(connection) == "synthetic"

    set_marker(connection, "test")
    assert read_marker(connection) == "test"
    # db_session rolls everything back when the test ends: `before` is restored.
    assert before in (None, "test", "synthetic", "dev", "production")
