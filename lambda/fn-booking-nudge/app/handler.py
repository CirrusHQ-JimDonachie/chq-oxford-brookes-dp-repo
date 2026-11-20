"""Booked but empty: e-mail the teaching bookings nobody turned up to.

Runs once a day on a schedule, for the previous day, or on demand for any day
given as {"date": "YYYY-MM-DD"}. It asks Athena for the teaching bookings in
rooms with a SMARTviz door counter where the counter's readings cover at least
half the booking and it counted nobody throughout, the same rule as the Empty
outcome on the Room Occupancy dashboard's Booked vs actual sheet, and
publishes them as one message to the alert topic.

This is the PoC's demonstration of ECS strategic question 2.3: the message goes
to the alert topic's subscribers, not to booking organisers, because the
TimeEdit exports carry no organiser. Smart-Vis data arrives in files, not live,
so the report is a day behind rather than 20 minutes into a booking.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import boto3

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

LOGGER = logging.getLogger()
LOGGER.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

WORKGROUP = os.environ["ATHENA_WORKGROUP"]
CATALOG = os.environ["CURATED_CATALOG"]
NAMESPACE = os.environ["CURATED_NAMESPACE"]
TEACHING_TABLE = os.environ["TEACHING_TABLE"]
TOPIC_ARN = os.environ["ALERT_TOPIC_ARN"]
EXCLUDED_ROOMS = os.environ.get("EXCLUDED_ROOMS", "")
PROVISIONAL_PERIODS = os.environ.get("PROVISIONAL_PERIODS", "")
SEND_WHEN_NONE = os.environ.get("SEND_WHEN_NONE", "false").lower() == "true"
QUERY_TIMEOUT_SECONDS = int(os.environ.get("QUERY_TIMEOUT_SECONDS", "240"))

ROOM_PATTERN = re.compile(r"^NHHB\.[A-Z0-9.]+$")
TABLE_PATTERN = re.compile(r"^[a-z0-9_]+$")
# SNS refuses a subject over 100 characters.
MAX_SUBJECT = 100

ATHENA = boto3.client("athena")
SNS = boto3.client("sns")


class QueryFailedError(RuntimeError):
    """Athena finished the query in a state other than SUCCEEDED."""


def report_date(event: Mapping[str, Any], now: datetime | None = None) -> date:
    """The day to report on: the event's date, or yesterday.

    The schedule fires at 07:00 UK time, when the UTC date is already the UK
    date, so yesterday in UTC is yesterday in the UK too.
    """
    given = event.get("date")
    if given:
        return date.fromisoformat(str(given))
    return (now or datetime.now(UTC)).date() - timedelta(days=1)


def room_list(value: str) -> list[str]:
    """Comma-separated room ids, upper-cased and checked, so they are safe in SQL."""
    rooms = [room.strip().upper() for room in value.split(",") if room.strip()]
    for room in rooms:
        if not ROOM_PATTERN.match(room):
            msg = f"Not a room id: {room!r}"
            raise ValueError(msg)
    return rooms


def provisional_periods(value: str) -> list[tuple[str, date]]:
    """Room:YYYY-MM-DD pairs: readings for the room before that date are provisional."""
    periods: list[tuple[str, date]] = []
    for item in value.split(","):
        if not item.strip():
            continue
        room, _, until = item.strip().partition(":")
        periods.extend((r, date.fromisoformat(until)) for r in room_list(room))
    return periods


def build_query(day: date) -> str:
    """The Athena query for one day's empty teaching bookings.

    Every value placed in the SQL is a date or a room id that has been parsed
    and checked first, never raw input.
    """
    if not TABLE_PATTERN.match(TEACHING_TABLE):
        msg = f"Not a table name: {TEACHING_TABLE!r}"
        raise ValueError(msg)
    curated = f'"{CATALOG}"."{NAMESPACE}"'
    excluded = room_list(EXCLUDED_ROOMS)
    excluded_sql = (
        "AND upper(room) NOT IN ({})".format(", ".join(f"'{r}'" for r in excluded))
        if excluded
        else ""
    )
    provisional_sql = "".join(
        f" AND NOT (upper(s.room_id) = '{room}'"
        f" AND s.local_date < '{until.isoformat()}')"
        for room, until in provisional_periods(PROVISIONAL_PERIODS)
    )
    # Safe to build as text: every interpolated value is checked above or is a
    # parsed date, and Athena's execution parameters cannot carry a table name.
    return f"""
WITH b AS (
  SELECT DISTINCT booking_id, upper(room) AS room_id, module, school,
         date_parse(booking_date || ' ' || start_time, '%d/%m/%Y %H:%i') AS start_at,
         CASE WHEN end_time LIKE '24:%'
              THEN date_parse(booking_date, '%d/%m/%Y') + INTERVAL '1' DAY
              ELSE date_parse(booking_date || ' ' || substr(end_time, 1, 5),
                              '%d/%m/%Y %H:%i')
         END AS end_at
  FROM {curated}."{TEACHING_TABLE}"
  WHERE booking_date = '{day.strftime("%d/%m/%Y")}'
    AND upper(room) LIKE 'NHHB.%'
    AND regexp_like(start_time, '^\\d{{2}}:\\d{{2}}$')
    AND regexp_like(end_time, '^\\d{{2}}:\\d{{2}}')
    {excluded_sql}
), sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people,
         max(capacity) AS capacity
  FROM {curated}."smartvis_occupancy" s
  WHERE local_date = '{day.isoformat()}'
    AND people_count IS NOT NULL AND minute(reading_at) % 5 = 0{provisional_sql}
  GROUP BY 1, 2
), slots AS (
  SELECT b.*, u.slot_at, sv.people, sv.capacity
  FROM b
  CROSS JOIN UNNEST(sequence(b.start_at, b.end_at - INTERVAL '5' MINUTE,
                             INTERVAL '5' MINUTE)) AS u(slot_at)
  LEFT JOIN sv ON sv.room_id = b.room_id AND sv.slot_at = u.slot_at
  WHERE b.end_at > b.start_at AND b.room_id IN (SELECT room_id FROM sv)
)
SELECT room_id, booking_id, module, school,
       date_format(start_at, '%H:%i') AS starts, date_format(end_at, '%H:%i') AS ends,
       max(capacity) AS capacity, count(people) AS readings, count(*) AS slots
FROM slots
GROUP BY room_id, booking_id, module, school, start_at, end_at
HAVING count(people) >= 0.5 * count(*) AND max(people) < 1
ORDER BY room_id, start_at
"""  # noqa: S608


def run_query(sql: str) -> list[dict[str, str]]:
    """Run a query in the workgroup and return its rows as dictionaries."""
    execution_id = ATHENA.start_query_execution(QueryString=sql, WorkGroup=WORKGROUP)[
        "QueryExecutionId"
    ]
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    while True:
        status = ATHENA.get_query_execution(QueryExecutionId=execution_id)[
            "QueryExecution"
        ]["Status"]
        state = status["State"]
        if state == "SUCCEEDED":
            break
        if state in {"FAILED", "CANCELLED"}:
            msg = f"Query {execution_id} {state}: {status.get('StateChangeReason', '')}"
            raise QueryFailedError(msg)
        if time.monotonic() > deadline:
            ATHENA.stop_query_execution(QueryExecutionId=execution_id)
            msg = f"Query {execution_id} still running after {QUERY_TIMEOUT_SECONDS} s"
            raise QueryFailedError(msg)
        time.sleep(1)
    rows: list[dict[str, str]] = []
    header: list[str] = []
    for page in ATHENA.get_paginator("get_query_results").paginate(
        QueryExecutionId=execution_id
    ):
        for row in page["ResultSet"]["Rows"]:
            values = [cell.get("VarCharValue", "") for cell in row["Data"]]
            if not header:
                header = values
                continue
            rows.append(dict(zip(header, values, strict=True)))
    return rows


def message(day: date, bookings: Sequence[Mapping[str, str]]) -> tuple[str, str]:
    """The e-mail subject and body for a day's empty bookings."""
    when = f"{day.strftime('%a')} {day.day} {day.strftime('%b %Y')}"
    count = len(bookings)
    noun = "booking" if count == 1 else "bookings"
    subject = f"Booked but empty: {count} teaching {noun} on {when}"[:MAX_SUBJECT]
    lines = [
        (
            f"These teaching bookings on {when} had nobody counted by the room's door "
            "counter for the whole booking, with readings covering at least half of it."
        ),
        "",
    ]
    if not bookings:
        lines.append("None found.")
    for row in bookings:
        details = ", ".join(v for v in (row.get("module"), row.get("school")) if v)
        capacity = f", capacity {row['capacity']}" if row.get("capacity") else ""
        lines.append(
            f"- {row['room_id']}  {row['starts']}-{row['ends']}  {details}"
            f" (booking {row['booking_id']}{capacity})"
        )
    excluded = ", ".join(room_list(EXCLUDED_ROOMS)) or "none"
    lines += [
        "",
        (
            f"Rooms left out: {excluded}. Non-teaching bookings, such as examinations, "
            "are not checked. Readings SMARTviz could not verify are left out."
        ),
        "",
        (
            "Source: TimeEdit timetable and SMARTviz door counters, via the Oxford "
            "Brookes building data platform (proof of concept). This is a test "
            "notification: no booking organiser has been contacted."
        ),
    ]
    return subject, "\n".join(lines)


def handler(event: Mapping[str, Any], context: object) -> dict[str, Any]:
    """Report one day's empty teaching bookings to the alert topic."""
    del context
    day = report_date(event)
    bookings = run_query(build_query(day))
    published = False
    if bookings or SEND_WHEN_NONE:
        subject, body = message(day, bookings)
        SNS.publish(TopicArn=TOPIC_ARN, Subject=subject, Message=body)
        published = True
    result = {
        "date": day.isoformat(),
        "empty_bookings": len(bookings),
        "published": published,
    }
    LOGGER.info(json.dumps(result))
    return result
