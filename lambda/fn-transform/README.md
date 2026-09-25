# fn-transform

The config-driven transform and threshold engine. An S3 object-created
notification on the `raw/` prefix of the landing bucket is its only trigger, and
one invocation runs both paths: the curated Iceberg write that feeds Athena and
QuickSight, and the snapshot upsert plus threshold evaluation that feed the read
API and the alert email.

Onboarding a source means writing one mapping-config item. There is no hook for
per-source code: a file the config cannot express is rejected.

## Layout

| Path | What it is |
| --- | --- |
| `app/` | The deployment package. Its contents sit at the function root, which is why the modules import each other by bare name. |
| `app/handler.py` | Entry point: unpacks the notification, delegates, reports counts. |
| `app/config.py` | Environment settings, object-key parsing, the mapping-config contract. |
| `app/mapping.py` | File parsing, field mapping, type coercion, validation. |
| `app/transform.py` | What one object does, from the claim through to the events. |
| `app/state.py` | The delivery claim and the snapshot upsert. |
| `app/alerting.py` | Rules, threshold evaluation, the alert-state machine, the event. |
| `app/iceberg_writer.py` | Curated table create, schema evolution and append. |
| `app/errors.py` | Writing rejected files and records to the error prefix. |
| `app/reasons.py` | The closed set of rejection reason codes. |
| `app/exceptions.py` | The engine's exception types. |
| `tests/` | Test suite. Not deployed. |
| `required_*.json` | What the infrastructure needs to grant, set and configure. |

## Running the checks

From the `lambda/` directory one level up:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy --strict fn-transform
uv run pytest fn-transform -v --disable-socket
uv run pip-audit -r fn-transform/requirements.txt
```

## Dependencies

The package bundles its own libraries; no Lambda layer is attached. The pins in
`requirements.txt` are what `required_runtime.json` declares as
`provided_by: package`:

- `aws-lambda-powertools` for structured logging, the typed S3 event class and
  the embedded-metric-format metrics.
- `pyiceberg` for the REST catalog client.
- `pyarrow` for the in-memory table the append is built from.

`boto3` and `botocore` come from the runtime image, so they are not pinned
here. The function does not use X-Ray, so `required_runtime.json`
declares `"tracing": false`.

## Configuration

Every variable in `required_env_vars.json` is required and is read at module
load, so a missing one fails initialisation, before any file is processed.

`AWS_REGION` is also read, for signing requests to the Glue Iceberg REST
endpoint. It is set by the Lambda runtime and is a reserved key, so it must not
be declared in the function's environment configuration.

## The mapping config

Mapping configs live in the config table under
`pk = config#{source}#{type}`, `sk = mapping`. The key of the dropped object
decides which one applies: `raw/{source}/{type}/yyyy/mm/dd/{filename}`.

```json
{
  "pk": "config#bms#environmental",
  "sk": "mapping",
  "input_format": "csv",
  "has_header": true,
  "fields": [
    {"from": "SensorRef", "to": "room_id",       "type": "string"},
    {"from": "Timestamp", "to": "reading_at",    "type": "timestamp", "format": "epoch_ms"},
    {"from": "RoomTemp",  "to": "temperature_c", "type": "decimal",   "scale": "0.1"},
    {"from": "RH",        "to": "humidity_pct",  "type": "decimal"}
  ],
  "validation": {
    "required": ["room_id", "reading_at"],
    "ranges": {"temperature_c": {"min": "-20", "max": "60"}}
  },
  "entity": {"type": "room", "key_field": "room_id"},
  "target_table": "building_data.bms_environmental"
}
```

- `input_format` is `csv`, `json` (an array of objects) or `jsonl`.
- `has_header` applies to CSV. With a header, `from` names a column. Without
  one, the config's field order is the column order and `from` is documentation.
- `type` is `string`, `decimal`, `integer`, `timestamp` or `boolean`. A
  `timestamp` field needs a `format` of `epoch_ms`, `epoch_s` or `iso8601`.
  `scale` multiplies a numeric value after it is read.
- `validation.required` and `validation.ranges` may only name fields the
  mapping produces. Ranges are inclusive.
- `entity` says which mapped field identifies the thing a reading is about. It
  produces the partition key `{type}#{value}`, which the snapshot item, the
  alert-state items and the alert rules all share.
- `target_table` names the namespace and table inside the curated table bucket.
  The warehouse setting already binds the catalog to one bucket and Glue
  namespaces are single level, so only the last two path components are
  addressable: `building_data.bms_environmental` and the fully qualified
  `s3tablescatalog/obu-curated/building_data/bms_environmental` resolve to the
  same table.

A config that is present but unusable, for example one with no `entity` block,
rejects the file with `INVALID_MAPPING_CONFIG`; the invocation itself
succeeds.

## Alert rules

Rules sit beside the mapping configs, under `pk = rule#{entity type}#{entity
value}` with the metric name as the sort key.

```json
{
  "pk": "rule#room#NHHB-2.14",
  "sk": "temperature_c",
  "comparison": ">",
  "threshold": 26.0,
  "severity": "warning",
  "notify_on_recovery": false
}
```

`comparison` is one of `>`, `>=`, `<`, `<=`, `==`, `!=`. A rule row the engine
cannot read is skipped, so one bad row does not cost the rest of the file.

Alerting fires on transitions only, in both directions. The first reading over
the threshold moves the entity to `BREACH`, stamps `breach_started_at` and emits
an event. Readings that stay over it change nothing and emit nothing. A reading
back under the threshold resets the state, and emits a recovery event only when
the rule sets `notify_on_recovery`. Both writes are conditional on the state
being moved away from, so two near-simultaneous readings produce one transition
and the loser writes nothing.

Events go onto the alerting bus with the `Source` set from
`ALERT_EVENT_SOURCE` and a `DetailType` of `Threshold Breach` or `Threshold
Recovery`. The routing rule matches on that same source, so the variable and
the rule are configured together. The detail carries
the entity, metric, value, threshold, comparison, severity, when the breach
began, and the file the reading came from, so an EventBridge rule can route on
it and a person can act on it without looking anything up.

## Two failure paths

Known-bad data goes to the error prefix and the invocation succeeds. Platform
failures leave the invocation so Lambda retries and the event reaches the
dead-letter queue.

| Reason code | Scope | What it means |
| --- | --- | --- |
| `MALFORMED_KEY` | File | The key is not `raw/{source}/{type}/yyyy/mm/dd/{filename}`. |
| `NO_MAPPING_CONFIG` | File | No config exists for that source and type. |
| `INVALID_MAPPING_CONFIG` | File | The config exists but does not describe a usable mapping. |
| `UNPARSEABLE_FILE` | File or record | The bytes are not the declared format. A single bad JSON Lines line is a record-level rejection; an undecodable or wrongly shaped file is a file-level one. |
| `MISSING_REQUIRED_FIELD` | Record | A required field was absent or empty. |
| `TYPE_COERCION_FAILED` | Record | A value could not be read as its declared type. |
| `VALUE_OUT_OF_RANGE` | Record | A value fell outside its configured bounds. |

A rejected file is copied to `{ERROR_PREFIX}{key without raw/}` with a
`.rejected.json` sidecar carrying the reason. Rejected records from an otherwise
good file are written once per file to `{ERROR_PREFIX}{key without
raw/}.rejects.jsonl`, one JSON object per line carrying the reason code, the
record, its index and the source key.

Every rejection is counted on the `RejectedRecords` metric in embedded metric
format, emitted twice over. Once as a plain total with no dimensions, which is
the series the data-quality alarm watches. Once per `source` and `reason` pair,
which is what a data owner triages against. The alarm needs the undimensioned
total because it cannot aggregate across dimension pairs it has not seen yet.
`FilesReceived` counts every notification record the function handled.

## Duplicate delivery

Before anything else, the invocation writes a claim item to the state table
keyed on bucket, key and version. The claim is two-phase: taking it writes
`status: processing`, and the object is only marked `status: done` once it has
been fully processed. Only `done` suppresses a later delivery.

That split is what makes a failure safe. If anything after the claim raises,
the invocation releases the claim on its way out, so Lambda's retry and a
manual dead-letter redrive both re-process the object from the start. If the
claim were held on failure, the retry would find the object claimed, return
success, and the file would be lost with no message on the queue to show for
it.

An invocation that is hard-killed cannot release anything. A claim still
`processing` after 360 seconds is therefore treated as abandoned and taken over
by the next delivery. The window is the 300 second function timeout plus a 60
second margin, so an invocation that is still working cannot outlast it.

The claim carries an expiry in the `ttl` attribute, set from `CLAIM_TTL_DAYS`
as epoch seconds. The state table's time-to-live has to be configured on that
attribute name or claims never expire. Snapshot and alert-state items carry no
expiry: they are the current state the read API serves.

If `ObjectInFlightError` reaches the dead-letter queue, two deliveries of the
same object version arrived close together and the second gave way to avoid
processing the file twice. A single occurrence is the mechanism
working. Repeated occurrences for the same key mean an invocation is taking
longer than the takeover window without being killed, which is a sizing
question: check the file's row count against the timeout before redriving.

## Sizing

The function timeout is 300 seconds, sized against the largest file the engine
accepts, 200,000 rows. The arithmetic and the other constraints the
infrastructure has to honour are in `required_event_source_config.json`. Two of
them matter more than the rest:

- The S3 notification must be filtered to the `raw/` prefix. Without it, writing
  an error record re-triggers the function and it loops on its own output.
- The function needs a failure destination or dead-letter queue with an alarm on
  its depth. S3 invokes asynchronously, so there is no caller to return an error
  to and a file that fails every retry is otherwise lost with no signal.

At 1 GB of memory a file is held as raw records, mapped records and one Arrow
table at once. Below that the append is the first thing to fail, and since
Lambda CPU scales with memory a smaller setting also lengthens the mapping pass
the timeout was sized against.

## Permissions

`required_api_actions.json` is the function role's own grant. The curated write
goes through the Glue Iceberg REST endpoint, so the role needs Glue catalog
actions plus `lakeformation:GetDataAccess`, and no direct access to the table
bucket.

Two things outside the role have to be in place, or the first curated write
fails with an access error that the role's policy will not explain:

- Lake Formation grants on the catalog, database and table for this role.
  An IAM allow without the Lake Formation grant is a denial.
- Lake Formation's own data-access role needs the `s3tables` actions that let it
  vend credentials for the table bucket, and external engines must be allowed to
  access data in Amazon S3 locations with full table access.
