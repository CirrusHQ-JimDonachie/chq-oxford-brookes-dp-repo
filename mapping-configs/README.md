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
| `weather-hourly.json` | `raw/weather/hourly/` | `building_data.weather_hourly` | Draft, awaiting Oxford Brookes sign-off |
| `aruba-ap_counts.json` | `raw/aruba/ap_counts/` | `building_data.wifi_client_sessions` | Draft, awaiting Oxford Brookes sign-off |
| `reference-rooms.json` | `raw/reference/rooms/` | `building_data.rooms` | Loaded 2026-10-28 (207 rooms) |

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

The table is append-only, so a re-sent list adds a second copy. Queries over it
take only the latest file: `WHERE source_file = (SELECT max_by(source_file,
ingested_at) FROM rooms)`.

The `bms/environmental` config used by the smoke test is in `DEPLOYMENT.md`.
