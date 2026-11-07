"""The environment label: the decision rules, with no database needed.

`verdict` is a pure function on purpose, so every rule can be tested here.
A real database is used only in tests/integration/test_db_safety.py.
"""

import pytest

from backend.app.core.db_safety import (
    ENVIRONMENT_VARIABLES, ENVIRONMENTS, MARKER_PREFIX, parse_marker, set_marker, verdict)

NEON = "ep-young-butterfly-xxxx-pooler.c-6.us-east-2.aws.neon.tech"


@pytest.mark.parametrize("comment, expected", [
    ("household-environment=test", "test"),
    ("  household-environment=production  ", "production"),
    ("household-environment=", None),
    ("some other comment", None),
    ("", None),
    (None, None),
])
def test_parse_marker(comment, expected):
    assert parse_marker(comment) == expected


def test_matching_label_is_accepted():
    ok, _ = verdict("test", "test", NEON)
    assert ok


@pytest.mark.parametrize("marker, expected", [
    ("production", "test"), ("dev", "production"), ("test", "synthetic"), ("synthetic", "dev"),
])
def test_a_different_label_is_refused_even_on_localhost(marker, expected):
    # A label that says "another environment" is never accepted, local or not.
    for host in (NEON, "localhost"):
        ok, message = verdict(marker, expected, host)
        assert not ok
        assert "WRONG DATABASE" in message


def test_unmarked_remote_database_is_refused_with_the_fix_in_the_message():
    ok, message = verdict(None, "dev", NEON)
    assert not ok
    assert "mark_environment.py dev --apply" in message


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "postgres", None])
def test_unmarked_local_database_is_accepted(host):
    # CI creates a fresh local database every run; it cannot be production.
    ok, _ = verdict(None, "test", host)
    assert ok


def test_every_environment_has_a_variable_to_read():
    assert set(ENVIRONMENT_VARIABLES) == set(ENVIRONMENTS)


def test_set_marker_rejects_unknown_environment_before_touching_the_database():
    class Boom:
        def execute(self, *_):
            raise AssertionError("must not reach the database")
    with pytest.raises(ValueError):
        set_marker(Boom(), "prod'; DROP DATABASE x; --")


def test_marker_prefix_is_stable():
    # Existing databases already carry this text: changing it would orphan every label.
    assert MARKER_PREFIX == "household-environment="
