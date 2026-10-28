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

The `bms/environmental` config used by the smoke test is in `DEPLOYMENT.md`.
