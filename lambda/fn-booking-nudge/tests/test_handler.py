"""Tests for the booked-but-empty report."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

import handler
import pytest
from botocore.stub import ANY, Stubber


def results(rows: list[list[str]]) -> dict[str, Any]:
    """An Athena GetQueryResults page: the header row, then the data rows."""
    header = [
        "room_id",
        "booking_id",
        "module",
        "school",
        "starts",
        "ends",
        "capacity",
        "readings",
        "slots",
    ]
    return {
        "ResultSet": {
            "Rows": [
                {"Data": [{"VarCharValue": v} for v in row]} for row in [header, *rows]
            ]
        }
    }


ROW = [
    "NHHB.1.17",
    "123456",
    "ENGR5002_S12_1_202609",
    "Engineering",
    "10:00",
    "12:00",
    "40",
    "24",
    "24",
]


def test_report_date_defaults_to_yesterday() -> None:
    """With no date, the report covers yesterday."""
    now = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)
    assert handler.report_date({}, now) == date(2026, 11, 19)


def test_report_date_takes_the_event_date() -> None:
    """An on-demand run reports on the date it is given."""
    assert handler.report_date({"date": "2026-11-10"}) == date(2026, 11, 10)


def test_report_date_rejects_anything_but_a_date() -> None:
    """A date that does not parse never reaches the SQL."""
    with pytest.raises(ValueError, match="Invalid isoformat"):
        handler.report_date({"date": "2026-11-10'; DROP TABLE x"})


def test_room_list_rejects_values_unsafe_in_sql() -> None:
    """Only room ids pass into the SQL."""
    with pytest.raises(ValueError, match="Not a room id"):
        handler.room_list("NHHB.1.12,1' OR '1'='1")


def test_query_filters_the_day_exclusions_and_provisional_readings() -> None:
    """The query is for the day, without excluded rooms or provisional readings."""
    sql = handler.build_query(date(2026, 11, 10))
    assert "booking_date = '10/11/2026'" in sql
    assert "local_date = '2026-11-10'" in sql
    assert "NOT IN ('NHHB.1.12')" in sql
    assert "upper(s.room_id) = 'NHHB.1.15' AND s.local_date < '2026-10-14'" in sql
    assert (
        '"s3tablescatalog/obu-curated"."building_data"."trial_timetable_teaching"'
        in sql
    )
    assert "HAVING count(people) >= 0.5 * count(*) AND max(people) < 1" in sql
    assert "#" not in sql


def test_message_lists_each_booking() -> None:
    """The e-mail names each booking and says what was left out."""
    subject, body = handler.message(
        date(2026, 11, 10),
        [
            dict(
                zip(
                    [
                        "room_id",
                        "booking_id",
                        "module",
                        "school",
                        "starts",
                        "ends",
                        "capacity",
                    ],
                    ROW,
                    strict=False,
                )
            )
        ],
    )
    assert subject == "Booked but empty: 1 teaching booking on Tue 10 Nov 2026"
    assert (
        "- NHHB.1.17  10:00-12:00  ENGR5002_S12_1_202609, Engineering "
        "(booking 123456, capacity 40)"
    ) in body
    assert "Rooms left out: NHHB.1.12." in body
    assert "no booking organiser has been contacted" in body


def stub_query(stubber: Stubber, rows: list[list[str]]) -> None:
    """Queue a successful Athena run returning these rows."""
    stubber.add_response(
        "start_query_execution",
        {"QueryExecutionId": "q1"},
        {"QueryString": ANY, "WorkGroup": "obu-poc"},
    )
    stubber.add_response(
        "get_query_execution",
        {"QueryExecution": {"Status": {"State": "SUCCEEDED"}}},
        {"QueryExecutionId": "q1"},
    )
    stubber.add_response("get_query_results", results(rows), {"QueryExecutionId": "q1"})


def test_handler_publishes_the_empty_bookings() -> None:
    """Empty bookings go to the alert topic as one message."""
    with Stubber(handler.ATHENA) as athena, Stubber(handler.SNS) as sns:
        stub_query(athena, [ROW])
        sns.add_response(
            "publish",
            {"MessageId": "m1"},
            {
                "TopicArn": "arn:aws:sns:eu-west-2:123456789012:obu-poc-alerts",
                "Subject": "Booked but empty: 1 teaching booking on Tue 10 Nov 2026",
                "Message": ANY,
            },
        )
        out = handler.handler({"date": "2026-11-10"}, None)
        sns.assert_no_pending_responses()
    assert out == {"date": "2026-11-10", "empty_bookings": 1, "published": True}


def test_handler_stays_quiet_when_nothing_is_empty() -> None:
    """No empty bookings, no e-mail."""
    with Stubber(handler.ATHENA) as athena, Stubber(handler.SNS):
        stub_query(athena, [])
        out = handler.handler({"date": "2026-11-10"}, None)
    assert out == {"date": "2026-11-10", "empty_bookings": 0, "published": False}


def test_handler_raises_when_the_query_fails() -> None:
    """A failed query fails the run, so the error is visible."""
    with Stubber(handler.ATHENA) as athena:
        athena.add_response(
            "start_query_execution",
            {"QueryExecutionId": "q1"},
            {"QueryString": ANY, "WorkGroup": "obu-poc"},
        )
        athena.add_response(
            "get_query_execution",
            {
                "QueryExecution": {
                    "Status": {
                        "State": "FAILED",
                        "StateChangeReason": "TABLE_NOT_FOUND",
                    }
                }
            },
            {"QueryExecutionId": "q1"},
        )
        with pytest.raises(handler.QueryFailedError, match="TABLE_NOT_FOUND"):
            handler.handler({"date": "2026-11-10"}, None)
