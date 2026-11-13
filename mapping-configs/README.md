# Mapping configs

One file per source and type, in the DynamoDB item format the config table
holds, so each loads unchanged:

```bash
aws dynamodb put-item --table-name "$CONFIG_TABLE" --item file://mapping-configs/weather-hourly.json --region eu-west-2
```

A config applies to files dropped after it is loaded. A file already rejected
with `NO_MAPPING_CONFIG` is not retried on its own: re-drop it.

| File | Source folder | Target table | Status |
| --- | --- | --- | --- |
| `weather-hourly.json` | `raw/weather/hourly/` | `building_data.weather_hourly` | Draft for the Open-Meteo archive CSV (2026-10-30 file maps in full), awaiting Oxford Brookes sign-off |
| `aruba-ap_counts.json` | `raw/aruba/ap_counts/` | `building_data.wifi_client_sessions` | Draft, awaiting Oxford Brookes sign-off |
| `reference-rooms.json` | `raw/reference/rooms/` | `building_data.rooms` | Loaded 2026-10-28 (207 rooms) |
| `smartvis-occupancy.json` | `raw/smartvis/occupancy/` | `building_data.smartvis_occupancy` | Draft; the 2026-09-01 to 2026-11-12 export maps in full locally, awaiting Oxford Brookes sign-off |

Load into the config table the transform actually reads - the account still
holds config tables from failed first creates, so take the name from the
function, not from a name search:

```bash
CONFIG_TABLE=$(aws lambda get-function-configuration --function-name building-data-dev-transform \
  --region eu-west-2 --query Environment.Variables.CONFIG_TABLE_NAME --output text)
```

## Room reference list

`building_data.rooms` is the Archibus room list for NHHB, with each room's Aruba
access point (`access_point_id`) and Smart-Vis sensor (`occupancy_sensor_id`).
It is what the Wi-Fi and occupancy sources join to, to report by room, floor,
room type and department. `room_id` is the Archibus room name (`NHHB.2.14-a`):
large rooms are split into `-a`/`-b` halves sharing one room code, so the code is
not unique. The entity type is `room_reference`, kept apart from live `room`
state so a reference row can never overwrite a reading.

Oxford Brookes supply it as a workbook; the engine reads CSV, so convert first,
then drop the CSV under `raw/reference/rooms/<yyyy>/<mm>/<dd>/`:

```bash
uv run --no-project --with openpyxl python mapping-configs/rooms_xlsx_to_csv.py \
  "RS NHHB-Room Data-Sept 2026.xlsx" nhhb-rooms.csv
```

Oxford Brookes keep the list up to date by editing the same spreadsheet and
re-sending it (agreed 2026-10-29). Each new version is converted and dropped
under that day's date folder; nothing else changes.

One row, room code `G0.1U`, has no room name and is rejected on every load.
Oxford Brookes confirmed it is an accessible toilet (NHHB.G.01U) and can be
ignored, so that rejection, and the rejected-records alarm email it sets off,
are expected.

The table is append-only, so a re-sent list adds a second copy. Queries over it
take only the latest file: `WHERE source_file = (SELECT max_by(source_file,
ingested_at) FROM rooms)`.

## Weather

Oxford Brookes pull hourly weather from the Open-Meteo historical archive
(`https://archive-api.open-meteo.com/v1/archive`), in its default units (°C, %,
mm, km/h, W/m²), with times in UTC. The API returns one list per measurement,
which the engine cannot read, so they flatten it to one CSV row per hour and
add a `location` column, as `weather-hourly.json` expects.

The table only adds rows, so files must not overlap: each should cover whole
days that have finished. The archive also returns the rest of the current day,
which cannot yet be observed, so a file ending today repeats hours the next
file will carry again.

## Smart-Vis occupancy

SMARTviz exports a workbook of readings per NHHB space: a people count from IMB
door counters (every five minutes) or a presence value from Elsys Eye sensors
(0 to 1, sent when it changes), never both, so one of the two columns is always
blank. Times are Europe/London wall-clock time with no offset. Convert first,
then drop the daily files under `raw/smartvis/occupancy/<yyyy>/<mm>/<dd>/`, in
date order:

```bash
uv run --no-project --with openpyxl python mapping-configs/smartvis_xlsx_to_csv.py \
  "Oxford Brookes University - <period> - Data-granular NHHB Building.xlsx" smartvis-csv/
```

The script writes one CSV per local day, adds the UTC time the engine stores as
`reading_at` and keeps the local date, time and offset. It turns the space name
into a room id (`NHHB 1.01` -> `NHHB.1.01`), which is the read API's `roomId`
and joins to `building_data.rooms` on `room_code`, not on
`occupancy_sensor_id`: the export carries no device ids. SMARTviz reports whole
rooms, so a room split into `-a`/`-b` halves in Archibus has one series.

Each file updates a room's current state from its last row, and the script
sorts by room and time so that row is the latest. Dropping an older day after a
newer one would set the state back, hence date order.

Known in the 2026-09-01 to 2026-11-12 export:

- The hour the clocks went back (01:00-01:59 on 25 October) appears once, not
  twice; the script reads it as BST, leaving 01:00-01:59 UTC empty.
- SMARTviz resets every room to zero at 00:00, and inserts zero readings off
  the five-minute grid to correct counting drift. Both are system generated,
  not observed; flag them in queries (`local_time = '00:00'`, minutes not a
  multiple of five on a people-count row).
- Gaps are missing rows, not blank ones: rooms joined SMARTviz on different
  dates (NHHB 1.09 on 1 October, NHHB G.05 on 10 November) and sensors drop out
  (NHHB 1.15 has nothing from 25 September to 18 October).
- Counts above the stated capacity occur (NHHB 1.09 peaks at 51 against 26), so
  capacity is stored but not used as a validation limit.

The `bms/environmental` config used by the smoke test is in `DEPLOYMENT.md`.
