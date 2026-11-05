"""Tests for the data contracts and the generated data dictionary."""
import pytest

from data_engineering.contracts import (
    CONTRACTS,
    DICTIONARY_PATH,
    PURCHASE_INTERVALS,
    PURCHASES,
    QUARANTINE,
    Column,
    check_value,
    render_data_dictionary,
    validate_table,
)

GOOD_PURCHASE = ("1,5,Rice,1,household,3,2.000,kg,2026-10-01T10:00:00+00:00,2026-10-01,77")
HEADER = ",".join(PURCHASES.column_names)


def csv_of(*lines):
    return "\r\n".join([HEADER, *lines]) + "\r\n"


# ------------------------------- single values ------------------------------

@pytest.mark.parametrize("column, text, expected", [
    (Column("n", "int", ""), "12", None),
    (Column("n", "int", ""), "1.5", "not an integer"),
    (Column("n", "int", ""), "", "empty but required"),
    (Column("n", "int", "", nullable=True), "", None),
    (Column("q", "decimal", "", minimum=0, minimum_exclusive=True), "0", "must be > 0"),
    (Column("q", "decimal", "", minimum=0, minimum_exclusive=True), "-1", "must be > 0"),
    (Column("q", "decimal", "", minimum=0, minimum_exclusive=True), "NaN", "not a finite number"),
    (Column("q", "decimal", "", minimum=0, minimum_exclusive=True), "0.001", None),
    (Column("d", "int", "", minimum=0), "0", None),
    (Column("d", "date", ""), "2026-13-01", "not an ISO date (YYYY-MM-DD)"),
    (Column("t", "datetime", ""), "2026-10-01T10:00:00", "datetime has no time zone"),
    (Column("t", "datetime", ""), "yesterday", "not an ISO datetime"),
    (Column("u", "text", "", allowed=frozenset({"kg"})), "cup", "value not allowed"),
])
def test_check_value(column, text, expected):
    assert check_value(column, text) == expected


# ------------------------------- whole files --------------------------------

def test_a_good_purchases_file_has_no_violations():
    assert validate_table(PURCHASES, csv_of(GOOD_PURCHASE)) == []


def test_an_empty_file_with_the_right_header_is_valid():
    assert validate_table(PURCHASES, csv_of()) == []


def test_wrong_columns_are_reported_by_name():
    (violation,) = validate_table(PURCHASES, "item_id,qty\r\n1,2\r\n")

    assert violation.rule == "columns"
    assert "missing" in violation.examples[0] and "unexpected ['qty']" in violation.examples[0]


@pytest.mark.parametrize("line, rule", [
    ("1,5,Rice,1,household,3,-2,kg,2026-10-01T10:00:00+00:00,2026-10-01,77", "must be > 0"),
    ("1,5,Rice,1,household,3,2,cup,2026-10-01T10:00:00+00:00,2026-10-01,77", "value not allowed"),
    ("1,5,Rice,1,household,3,2,kg,2026-10-01T10:00:00,2026-10-01,77", "datetime has no time zone"),
    (",5,Rice,1,household,3,2,kg,2026-10-01T10:00:00+00:00,2026-10-01,77", "empty but required"),
    ("1,5,Rice,1,household,3,2,kg,2026-10-01T10:00:00+00:00,2026-10-01", "row length"),
])
def test_bad_rows_are_reported_with_the_line_number(line, rule):
    violations = validate_table(PURCHASES, csv_of(line))

    assert [v.rule for v in violations] == [rule]
    assert "line 2" in violations[0].examples[0]


def test_unique_columns_must_not_repeat():
    violations = validate_table(PURCHASES, csv_of(GOOD_PURCHASE, GOOD_PURCHASE))

    assert {v.column for v in violations} == {"item_id", "source_event_id"}


def test_optional_columns_may_be_empty():
    line = "1,,,1,household,3,2,kg,2026-10-01T10:00:00+00:00,2026-10-01,77"   # a free-text item

    assert validate_table(PURCHASES, csv_of(line)) == []


def test_examples_are_limited_so_a_report_stays_small():
    lines = [f"{i},5,Rice,1,household,3,-2,kg,2026-10-01T10:00:00+00:00,2026-10-01,{i}" for i in range(50)]

    (violation,) = validate_table(PURCHASES, csv_of(*lines))

    assert violation.count == 50 and len(violation.examples) == 3


def test_every_table_has_a_contract():
    assert set(CONTRACTS) == {"purchases.csv", "purchase_intervals.csv", "quarantine.csv"}
    assert PURCHASE_INTERVALS.column_names[-1] == "days_since_previous"
    assert QUARANTINE.column_names == ("event_id", "item_id", "reason")


# --------------------------- generated documentation -------------------------

def test_the_committed_data_dictionary_matches_the_contracts():
    # If this fails: run
    #   uv run python -m data_engineering.contracts
    #
    # Only the trailing newline is ignored: editors (format on save, "trim
    # final newline") and git line-ending settings add or remove it, and that
    # is not a difference in meaning.
    committed = DICTIONARY_PATH.read_text(encoding="utf-8")

    assert committed.rstrip() == render_data_dictionary().rstrip()


def test_the_dictionary_states_what_purchased_does_not_mean():
    text = render_data_dictionary()

    assert "not proof that the product was really bought" in text
    assert "FINAL state" in text


# ------------------------------- scopes ---------------------------------------

@pytest.mark.parametrize("scope_type, scope_id, fragment", [
    ("household", "3", None),
    ("user", "3", None),
    ("family", "3", "value not allowed"),          # only household / user exist
    ("household", "0", "must be > 0"),
    ("household", "", "empty but required"),
])
def test_scope_columns_are_checked(scope_type, scope_id, fragment):
    line = f"1,5,Rice,1,{scope_type},{scope_id},2,kg,2026-10-01T10:00:00+00:00,2026-10-01,77"
    violations = validate_table(PURCHASES, csv_of(line))

    if fragment is None:
        assert violations == []
    else:
        assert any(fragment in v.rule for v in violations)


def test_the_old_file_without_scope_columns_is_rejected():
    old_header = "item_id,product_id,product_name,list_id,quantity,unit,purchased_at,purchase_date,source_event_id"
    text = old_header + "\r\n1,5,Rice,1,2.000,kg,2026-10-01T10:00:00+00:00,2026-10-01,77\r\n"

    (violation,) = validate_table(PURCHASES, text)

    assert violation.rule == "columns" and "scope_type" in violation.examples[0]


def test_intervals_start_with_the_scope_and_the_dictionary_explains_it():
    assert PURCHASE_INTERVALS.column_names[:2] == ("scope_type", "scope_id")
    text = render_data_dictionary()
    assert "Every purchase belongs to one scope" in text
    assert "Contract version: **2.0.0**" in text
