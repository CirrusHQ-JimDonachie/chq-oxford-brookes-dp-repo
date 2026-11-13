"""Turn a SMARTviz granular export into the daily CSVs the smartvis/occupancy config reads.

The export is a workbook, which the engine cannot read, and its Date and Time
columns are Europe/London wall-clock time with no offset, which the engine
would take as UTC. This writes one CSV per local day with:

- ``reading_at_utc``: the reading time in UTC, for the curated table and alerts;
- ``local_date``, ``local_time`` and ``utc_offset``: the source time as exported,
  so it stays traceable and the clock changes are visible;
- ``room_code`` and ``room_id``: the space name as an Archibus room code
  (``NHHB 1.01`` -> ``1.01``) and a room id in the Archibus style
  (``NHHB.1.01``). SMARTviz reports whole rooms, so a room Archibus splits into
  ``-a``/``-b`` halves has one id here;

and every source column kept as it was. Rows are sorted by room and time, so
each room's last row in a file is its latest reading, which is the one the
engine keeps as the room's current state.

Times inside the hour the clocks go back (01:00-01:59 on the last Sunday of
October) occur twice in UK time; the export carries each only once, so they
are read as the first occurrence (BST) and counted in the summary. A time that
does not exist (inside the hour the clocks go forward) stops the conversion.

    uv run --no-project --with openpyxl python mapping-configs/smartvis_xlsx_to_csv.py \
        "Oxford Brookes University - ... - Data-granular NHHB Building.xlsx" out-dir/
"""

import csv
import sys
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from openpyxl import load_workbook

LONDON = ZoneInfo("Europe/London")

SOURCE_COLUMNS = (
    "Building",
    "Space",
    "Date",
    "Time",
    "Occupancy (People Count - IMB)",
    "Occupancy (Presence - Elsys Eye)",
    "Capacity",
    "Area",
)
OUTPUT_COLUMNS = (
    "reading_at_utc",
    "local_date",
    "local_time",
    "utc_offset",
    "room_code",
    "room_id",
    *SOURCE_COLUMNS,
)


def to_utc(local: datetime) -> tuple[datetime, bool]:
    """Read a naive Europe/London time as UTC.

    Returns:
        The UTC time and whether the local time was ambiguous (clocks going back).

    Raises:
        ValueError: The local time does not exist (clocks going forward).
    """
    first = local.replace(tzinfo=LONDON, fold=0)
    second = local.replace(tzinfo=LONDON, fold=1)
    if first.utcoffset() != second.utcoffset():
        if first.utcoffset() < second.utcoffset():
            # fold=0 offset smaller than fold=1 only happens in the spring gap.
            msg = f"{local:%Y-%m-%d %H:%M} does not exist in Europe/London"
            raise ValueError(msg)
        return first.astimezone(UTC), True
    return first.astimezone(UTC), False


def offset_text(delta: timedelta) -> str:
    """Format a UTC offset as +HH:MM."""
    minutes = int(delta.total_seconds() // 60)
    sign = "+" if minutes >= 0 else "-"
    return f"{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"


def cell_text(value: object) -> str:
    """Write a cell as the engine will read it: blank stays blank."""
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def main(source: str, target_dir: str) -> None:
    """Write the export's rows to one CSV per local day."""
    sheet = load_workbook(source, read_only=True, data_only=True).worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = tuple(cell_text(cell) for cell in next(rows))
    if header != SOURCE_COLUMNS:
        sys.exit(f"Unexpected header in {source}: {header}")

    days: dict[str, list[dict[str, str]]] = {}
    ambiguous = 0
    spaces: Counter[str] = Counter()
    for row in rows:
        values = dict(zip(SOURCE_COLUMNS, (cell_text(cell) for cell in row), strict=True))
        if not any(values.values()):
            continue
        local = datetime.strptime(f"{values['Date']} {values['Time']}", "%d-%b-%Y %H:%M")
        utc, was_ambiguous = to_utc(local)
        ambiguous += was_ambiguous
        space = values["Space"]
        building, _, code = space.partition(" ")
        if building != "NHHB" or not code:
            sys.exit(f"Space {space!r} is not of the form 'NHHB <room code>'")
        spaces[space] += 1
        day = f"{local:%Y-%m-%d}"
        days.setdefault(day, []).append(
            {
                "reading_at_utc": utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "local_date": day,
                "local_time": f"{local:%H:%M}",
                "utc_offset": offset_text(local.replace(tzinfo=LONDON).utcoffset()),
                "room_code": code,
                "room_id": f"{building}.{code}",
                **values,
            }
        )

    out = Path(target_dir)
    out.mkdir(parents=True, exist_ok=True)
    for day, day_rows in sorted(days.items()):
        day_rows.sort(key=lambda r: (r["room_id"], r["reading_at_utc"]))
        with (out / f"smartvis-nhhb-{day}.csv").open("w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=OUTPUT_COLUMNS)
            writer.writeheader()
            writer.writerows(day_rows)

    total = sum(spaces.values())
    sys.stdout.write(
        f"{total} readings for {len(spaces)} spaces written to {len(days)} daily files "
        f"in {out} ({min(days)} to {max(days)}); "
        f"{ambiguous} readings in a repeated clock-change hour read as BST\n"
    )


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
