"""Write the QuickSight definitions for the Smart-Vis occupancy content as JSON files.

build.sh calls this, then passes the files to the QuickSight API. It writes:

- occupancy-table.json, coverage-table.json: the two datasets' physical tables
  (custom SQL from sql/, over the curated catalog);
- occupancy-dashboard.json: the Room Occupancy dashboard;
- topic.json: the Q Topic, modelled on both datasets.

    python3 quicksight/occupancy.py <out dir> <data source arn> <occupancy dataset arn> \
        <coverage dataset arn> <curated catalog> <namespace>
"""

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

OCCUPANCY_COLUMNS = [
    ("room_id", "STRING"), ("room_code", "STRING"), ("space_name", "STRING"),
    ("floor", "STRING"), ("room_type", "STRING"), ("department", "STRING"),
    ("archibus_rooms", "STRING"), ("sensor_type", "STRING"),
    ("local_at", "DATETIME"), ("reading_at_utc", "DATETIME"), ("local_date", "DATETIME"),
    ("weekday", "STRING"), ("hour_of_day", "INTEGER"), ("core_hours", "STRING"),
    ("people_count", "DECIMAL"), ("people_count_scheduled", "DECIMAL"),
    ("people_count_core", "DECIMAL"), ("presence", "DECIMAL"), ("capacity", "INTEGER"),
    ("area_m2", "DECIMAL"), ("utilisation_pct", "DECIMAL"), ("utilisation_pct_core", "DECIMAL"),
    ("reading_kind", "STRING"), ("source_file", "STRING"),
]
COVERAGE_COLUMNS = [
    ("room_id", "STRING"), ("room_code", "STRING"), ("space_name", "STRING"),
    ("capacity", "INTEGER"), ("local_date", "DATETIME"), ("first_day", "DATETIME"),
    ("in_service", "STRING"), ("expected_slots", "INTEGER"), ("observed_slots", "INTEGER"),
    ("longest_gap_slots", "INTEGER"), ("longest_gap_hours", "DECIMAL"),
    ("midnight_resets", "INTEGER"), ("corrections", "INTEGER"),
]

COVERAGE_EXPRESSION = "sum({observed_slots}) / sum({expected_slots}) * 100"
CAVEAT = (
    "Derived from available sensor observations; gaps and system-generated "
    "corrections may affect interpretation."
)


def physical_table(name, sql_file, columns, data_source_arn, curated):
    sql = (HERE / "sql" / sql_file).read_text().replace("{curated}", curated)
    # The map key is an id (letters, digits, hyphens); the name may have underscores.
    return {name.replace("_", "-"): {"CustomSql": {
        "DataSourceArn": data_source_arn, "Name": name, "SqlQuery": sql,
        "Columns": [{"Name": n, "Type": t} for n, t in columns],
    }}}


# --- dashboard building blocks -------------------------------------------------------

def col(ds, name):
    return {"DataSetIdentifier": ds, "ColumnName": name}


def title(text, subtitle=None):
    out = {"Title": {"Visibility": "VISIBLE", "FormatText": {"PlainText": text}}}
    if subtitle:
        out["Subtitle"] = {"Visibility": "VISIBLE", "FormatText": {"PlainText": subtitle}}
    return out


def measure(field_id, ds, name, how=None, fmt=None):
    field = {"FieldId": field_id, "Column": col(ds, name)}
    if how:
        field["AggregationFunction"] = {"SimpleNumericalAggregation": how}
    if fmt:
        field["FormatConfiguration"] = fmt
    return {"NumericalMeasureField": field}


def dim(field_id, ds, name):
    return {"CategoricalDimensionField": {"FieldId": field_id, "Column": col(ds, name)}}


def date_dim(field_id, ds, name, granularity="DAY"):
    return {"DateDimensionField": {"FieldId": field_id, "Column": col(ds, name),
                                   "DateGranularity": granularity}}


def number_format(decimals=1, suffix=None):
    cfg = {"DecimalPlacesConfiguration": {"DecimalPlaces": decimals}}
    if suffix:
        cfg["Suffix"] = suffix
    # A numerical field's FormatConfiguration is itself a NumberFormatConfiguration.
    return {"FormatConfiguration": {"NumberDisplayFormatConfiguration": cfg}}


PCT = number_format(1, "%")
ONE_DP = number_format(1)


def labels(ds, *fields):
    """Axis labels: AxisLabelOptions for (field id, column, label) triples. Without them
    QuickSight shows the column name, e.g. "people_count_core (Average)"."""
    return {"AxisLabelOptions": [
        {"CustomLabel": label, "ApplyTo": {"FieldId": field_id, "Column": col(ds, name)}}
        for field_id, name, label in fields]}


def tooltip(*fields):
    """A tooltip naming each (field id, label) pair, instead of the column names."""
    return {"TooltipVisibility": "VISIBLE", "SelectedTooltipType": "DETAILED",
            "FieldBasedTooltip": {"AggregationVisibility": "HIDDEN",
                                  "TooltipTitleType": "PRIMARY_VALUE",
                                  "TooltipFields": [
                                      {"FieldTooltipItem": {"FieldId": f, "Label": label,
                                                            "Visibility": "VISIBLE"}}
                                      for f, label in fields]}}


def kpi(visual_id, text, value, subtitle=None):
    return {"KPIVisual": {"VisualId": visual_id, **title(text, subtitle),
                          "ChartConfiguration": {"FieldWells": {"Values": [value]}}}}


def grid(*elements):
    """A grid layout; each element is (visual id, column span, row span)."""
    return [{"Configuration": {"GridLayout": {"Elements": [
        {"ElementId": e, "ElementType": "VISUAL", "ColumnSpan": c, "RowSpan": r}
        for e, c, r in elements]}}}]


def sheet_filters(sheet_id, date_id, room_id, ds):
    """Date-range and room filters for one sheet, applied to both datasets.

    A filter shared by several sheets needs a default control configuration, so
    each sheet gets its own pair; CrossDataset applies it to every dataset with a
    column of the same name and type.
    """
    scope = {"SelectedSheets": {"SheetVisualScopingConfigurations": [
        {"SheetId": sheet_id, "Scope": "ALL_VISUALS"}]}}
    return [
        {"FilterGroupId": f"fg-{date_id}", "CrossDataset": "ALL_DATASETS", "Status": "ENABLED",
         "Filters": [{"TimeRangeFilter": {
             "FilterId": date_id, "Column": col(ds, "local_date"),
             "IncludeMinimum": True, "IncludeMaximum": True, "NullOption": "NON_NULLS_ONLY",
             "TimeGranularity": "DAY",
             "RangeMinimumValue": {"StaticValue": "2026-09-01T00:00:00.000Z"},
             "RangeMaximumValue": {"StaticValue": "2027-12-31T00:00:00.000Z"}}}],
         "ScopeConfiguration": scope},
        {"FilterGroupId": f"fg-{room_id}", "CrossDataset": "ALL_DATASETS", "Status": "ENABLED",
         "Filters": [{"CategoryFilter": {
             "FilterId": room_id, "Column": col(ds, "room_id"),
             "Configuration": {"FilterListConfiguration": {
                 "MatchOperator": "CONTAINS", "SelectAllOptions": "FILTER_ALL_VALUES",
                 "NullOption": "NON_NULLS_ONLY"}}}}],
         "ScopeConfiguration": scope},
    ]


def dashboard(occupancy_arn, coverage_arn):
    occ, cov = "occ", "cov"
    coverage_kpi = measure("cov-pct", cov, "coverage_pct", fmt=PCT)

    occupancy_sheet = {
        "SheetId": "occupancy", "Name": "Occupancy",
        "FilterControls": [
            {"DateTimePicker": {"FilterControlId": "ctl-date", "Title": "Dates",
                                "SourceFilterId": "f-date", "Type": "DATE_RANGE"}},
            {"Dropdown": {"FilterControlId": "ctl-room", "Title": "Room",
                          "SourceFilterId": "f-room", "Type": "MULTI_SELECT"}},
        ],
        "Visuals": [
            kpi("occ-coverage", "Coverage", coverage_kpi,
                "Share of expected five-minute door-counter readings received in the "
                "selected rooms and dates. " + CAVEAT),
            {"GaugeChartVisual": {"VisualId": "occ-gauge", **title(
                "Average occupancy as % of capacity",
                "Door-counter rooms, weekdays 08:00-18:00 UK time"),
                "ChartConfiguration": {
                    "FieldWells": {"Values": [measure("g-util", occ, "utilisation_pct_core",
                                                      "AVERAGE", PCT)]},
                    "GaugeChartOptions": {"Arc": {"ArcAngle": 180},
                                          "ArcAxis": {"Range": {"Min": 0, "Max": 100}}},
                    "TooltipOptions": tooltip(("g-util", "Average % of capacity"))}}},
            {"HeatMapVisual": {"VisualId": "occ-heat", **title(
                "Average people per room by weekday and hour",
                "Door-counter rooms, UK local time; correction readings left out"),
                "ChartConfiguration": {
                    "FieldWells": {"HeatMapAggregatedFieldWells": {
                        "Rows": [dim("h-day", occ, "weekday")],
                        "Columns": [{"NumericalDimensionField": {
                            "FieldId": "h-hour", "Column": col(occ, "hour_of_day")}}],
                        "Values": [measure("h-val", occ, "people_count_scheduled", "AVERAGE", ONE_DP)],
                    }},
                    "RowLabelOptions": labels(occ, ("h-day", "weekday", "Weekday")),
                    "ColumnLabelOptions": labels(occ, ("h-hour", "hour_of_day", "Hour starting")),
                    "Legend": {"Visibility": "VISIBLE", "Title": {
                        "Visibility": "VISIBLE", "CustomLabel": "Average people"}},
                    "Tooltip": tooltip(("h-day", "Weekday"), ("h-hour", "Hour starting"),
                                       ("h-val", "Average people per room"))}}},
            {"BarChartVisual": {"VisualId": "occ-floor", **title(
                "Occupancy as % of capacity by floor",
                "Average, door-counter rooms, weekdays 08:00-18:00 UK time"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim("f-floor", occ, "floor")],
                        "Values": [measure("f-util", occ, "utilisation_pct_core", "AVERAGE", PCT)],
                    }},
                    "CategoryLabelOptions": labels(occ, ("f-floor", "floor", "Floor")),
                    "ValueLabelOptions": labels(occ, ("f-util", "utilisation_pct_core",
                                                      "Average % of capacity")),
                    "Tooltip": tooltip(("f-floor", "Floor"), ("f-util", "Average % of capacity"))}}},
            {"BarChartVisual": {"VisualId": "occ-type", **title(
                "Occupancy as % of capacity by room type",
                "Average, door-counter rooms, weekdays 08:00-18:00 UK time"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "FieldWells": {
                    "BarChartAggregatedFieldWells": {
                        "Category": [dim("t-type", occ, "room_type")],
                        "Values": [measure("t-util", occ, "utilisation_pct_core", "AVERAGE", PCT)],
                    }},
                    "CategoryLabelOptions": labels(occ, ("t-type", "room_type", "Room type")),
                    "ValueLabelOptions": labels(occ, ("t-util", "utilisation_pct_core",
                                                      "Average % of capacity")),
                    "Tooltip": tooltip(("t-type", "Room type"), ("t-util", "Average % of capacity"))}}},
            {"BarChartVisual": {"VisualId": "occ-peak", **title(
                "Peak people count against capacity by room",
                "Highest five-minute count in the selected dates, door-counter rooms"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "BarsArrangement": "CLUSTERED",
                    "FieldWells": {"BarChartAggregatedFieldWells": {
                        "Category": [dim("p-room", occ, "room_id")],
                        # The legend shows field names and ignores custom labels, so these
                        # are calculated fields with readable names (see CalculatedFields).
                        "Values": [measure("p-peak", occ, "Peak count", "MAX"),
                                   measure("p-cap", occ, "Room capacity", "MAX")],
                    }},
                    "SortConfiguration": {"CategorySort": [
                        {"FieldSort": {"FieldId": "p-room", "Direction": "ASC"}}]},
                    "CategoryLabelOptions": labels(occ, ("p-room", "room_id", "Room")),
                    "ValueLabelOptions": labels(occ, ("p-peak", "Peak count", "People"),
                                                ("p-cap", "Room capacity", "People")),
                    "Legend": {"Visibility": "VISIBLE", "Title": {"Visibility": "HIDDEN"}},
                    "Tooltip": tooltip(("p-room", "Room"), ("p-peak", "Peak count"),
                                       ("p-cap", "Room capacity"))}}},
            {"LineChartVisual": {"VisualId": "occ-trend", **title(
                "Daily average people per room, with 14-day forecast",
                "Door-counter rooms, weekdays 08:00-18:00 UK time, from 21 Sep 2026; "
                "the near-empty weeks before then would skew the forecast"),
                "ChartConfiguration": {
                    "FieldWells": {"LineChartAggregatedFieldWells": {
                        "Category": [date_dim("l-day", occ, "local_date")],
                        "Values": [measure("l-val", occ, "people_count_core", "AVERAGE", ONE_DP)],
                        "Colors": []}},
                    "ForecastConfigurations": [{"ForecastProperties": {
                        "PeriodsForward": 14, "PeriodsBackward": 0, "PredictionInterval": 90}}],
                    "XAxisLabelOptions": labels(occ, ("l-day", "local_date", "Date")),
                    "PrimaryYAxisLabelOptions": labels(occ, ("l-val", "people_count_core",
                                                             "Average people per room")),
                    "Tooltip": tooltip(("l-day", "Date"), ("l-val", "Average people per room"))}}},
            {"TableVisual": {"VisualId": "occ-presence", **title(
                "Presence-sensor rooms",
                "Sensors report when someone arrives or leaves, so this is the share of "
                "reports showing someone present, not the share of time"),
                "ChartConfiguration": {
                    "FieldWells": {"TableAggregatedFieldWells": {
                        "GroupBy": [dim("pr-room", occ, "room_id")],
                        "Values": [measure("pr-n", occ, "presence", "COUNT"),
                                   measure("pr-occ", occ, "presence_pct", fmt=PCT)],
                    }},
                    "FieldOptions": {"SelectedFieldOptions": [
                        {"FieldId": "pr-room", "CustomLabel": "Room"},
                        {"FieldId": "pr-n", "CustomLabel": "Reports"},
                        {"FieldId": "pr-occ", "CustomLabel": "Reports showing presence"},
                    ]}}}},
        ],
        "Layouts": grid(("occ-coverage", 12, 6), ("occ-gauge", 12, 6), ("occ-floor", 12, 6),
                        ("occ-heat", 36, 12), ("occ-trend", 36, 10), ("occ-peak", 18, 14),
                        ("occ-type", 18, 7), ("occ-presence", 18, 7)),
    }

    quality_sheet = {
        "SheetId": "quality", "Name": "Data quality",
        "FilterControls": [
            {"DateTimePicker": {"FilterControlId": "ctl-date-q", "Title": "Dates",
                                "SourceFilterId": "f-date-q", "Type": "DATE_RANGE"}},
            {"Dropdown": {"FilterControlId": "ctl-room-q", "Title": "Room",
                          "SourceFilterId": "f-room-q", "Type": "MULTI_SELECT"}},
        ],
        "Visuals": [
            kpi("q-coverage", "Coverage", measure("q-cov", cov, "coverage_pct", fmt=PCT),
                "Reporting-period coverage: every day in the range counts, including "
                "days before a room was monitored"),
            kpi("q-gap", "Longest gap (hours)",
                measure("q-gap-v", cov, "longest_gap_hours", "MAX", ONE_DP),
                "Longest run of missing readings within one day"),
            kpi("q-resets", "Midnight resets", measure("q-res", cov, "midnight_resets", "SUM"),
                "System-generated zero at 00:00, not an observed exit"),
            kpi("q-corr", "Correction readings", measure("q-cor", cov, "corrections", "SUM"),
                "Zero readings inserted off the five-minute grid to correct drift"),
            {"TableVisual": {"VisualId": "q-rooms", **title(
                "Coverage by room", CAVEAT + " Presence-sensor rooms report on change, "
                "so coverage does not apply to them."),
                "ChartConfiguration": {
                    "FieldWells": {"TableAggregatedFieldWells": {
                        "GroupBy": [dim("qr-room", cov, "room_id")],
                        "Values": [
                            measure("qr-cov", cov, "coverage_pct", fmt=PCT),
                            measure("qr-svc", cov, "in_service_coverage_pct", fmt=PCT),
                            measure("qr-obs", cov, "observed_slots", "SUM"),
                            measure("qr-exp", cov, "expected_slots", "SUM"),
                            measure("qr-gap", cov, "longest_gap_hours", "MAX", ONE_DP),
                            measure("qr-res", cov, "midnight_resets", "SUM"),
                            measure("qr-cor", cov, "corrections", "SUM"),
                        ]}},
                    "SortConfiguration": {"RowSort": [
                        {"FieldSort": {"FieldId": "qr-cov", "Direction": "ASC"}}]},
                    "FieldOptions": {"SelectedFieldOptions": [
                        {"FieldId": "qr-room", "CustomLabel": "Room"},
                        {"FieldId": "qr-cov", "CustomLabel": "Coverage"},
                        {"FieldId": "qr-svc", "CustomLabel": "In-service coverage"},
                        {"FieldId": "qr-obs", "CustomLabel": "Slots received"},
                        {"FieldId": "qr-exp", "CustomLabel": "Slots expected"},
                        {"FieldId": "qr-gap", "CustomLabel": "Longest gap (h)"},
                        {"FieldId": "qr-res", "CustomLabel": "Midnight resets"},
                        {"FieldId": "qr-cor", "CustomLabel": "Corrections"},
                    ]}}}},
            {"LineChartVisual": {"VisualId": "q-trend", **title(
                "Coverage by day", "All selected door-counter rooms together"),
                "ChartConfiguration": {
                    "FieldWells": {"LineChartAggregatedFieldWells": {
                        "Category": [date_dim("qt-day", cov, "local_date")],
                        "Values": [measure("qt-cov", cov, "coverage_pct", fmt=PCT)],
                        "Colors": []}},
                    "XAxisLabelOptions": labels(cov, ("qt-day", "local_date", "Date")),
                    "PrimaryYAxisLabelOptions": labels(cov, ("qt-cov", "coverage_pct", "Coverage")),
                    "Tooltip": tooltip(("qt-day", "Date"), ("qt-cov", "Coverage"))}}},
        ],
        "Layouts": grid(("q-coverage", 9, 6), ("q-gap", 9, 6), ("q-resets", 9, 6),
                        ("q-corr", 9, 6), ("q-rooms", 36, 14), ("q-trend", 36, 10)),
    }

    return {
        "DataSetIdentifierDeclarations": [
            {"Identifier": occ, "DataSetArn": occupancy_arn},
            {"Identifier": cov, "DataSetArn": coverage_arn},
        ],
        "CalculatedFields": [
            {"DataSetIdentifier": cov, "Name": "coverage_pct", "Expression": COVERAGE_EXPRESSION},
            {"DataSetIdentifier": cov, "Name": "in_service_coverage_pct",
             "Expression": "sum(ifelse({in_service} = 'Yes', {observed_slots}, 0)) / "
                           "sum(ifelse({in_service} = 'Yes', {expected_slots}, 0)) * 100"},
            {"DataSetIdentifier": occ, "Name": "presence_pct",
             "Expression": "avg({presence}) * 100"},
            {"DataSetIdentifier": occ, "Name": "Peak count",
             "Expression": "{people_count_scheduled}"},
            {"DataSetIdentifier": occ, "Name": "Room capacity", "Expression": "{capacity}"},
        ],
        "FilterGroups": [
            *sheet_filters("occupancy", "f-date", "f-room", occ),
            *sheet_filters("quality", "f-date-q", "f-room-q", occ),
            {"FilterGroupId": "fg-presence", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "Filters": [{"CategoryFilter": {
                 "FilterId": "f-presence", "Column": col(occ, "sensor_type"),
                 "Configuration": {"FilterListConfiguration": {
                     "MatchOperator": "CONTAINS", "CategoryValues": ["Presence sensor"]}}}}],
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "occupancy", "Scope": "SELECTED_VISUALS",
                  "VisualIds": ["occ-presence"]}]}}},
            # Presence rooms have no people count, so they would show as empty bars.
            {"FilterGroupId": "fg-door", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "Filters": [{"CategoryFilter": {
                 "FilterId": "f-door", "Column": col(occ, "sensor_type"),
                 "Configuration": {"FilterListConfiguration": {
                     "MatchOperator": "CONTAINS", "CategoryValues": ["Door counter"]}}}}],
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "occupancy", "Scope": "SELECTED_VISUALS",
                  "VisualIds": ["occ-floor", "occ-type", "occ-peak"]}]}}},
            # The forecast fits whatever the line shows; the weeks before 21 Sep are
            # near-empty and would read as a rising trend.
            {"FilterGroupId": "fg-trend-start", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "Filters": [{"TimeRangeFilter": {
                 "FilterId": "f-trend-start", "Column": col(occ, "local_date"),
                 "IncludeMinimum": True, "IncludeMaximum": True, "NullOption": "NON_NULLS_ONLY",
                 "TimeGranularity": "DAY",
                 "RangeMinimumValue": {"StaticValue": "2026-09-21T00:00:00.000Z"},
                 "RangeMaximumValue": {"StaticValue": "2027-12-31T00:00:00.000Z"}}}],
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "occupancy", "Scope": "SELECTED_VISUALS",
                  "VisualIds": ["occ-trend"]}]}}},
        ],
        "Sheets": [occupancy_sheet, quality_sheet],
    }


# --- Q Topic -------------------------------------------------------------------------

def dimension(name, friendly, description, synonyms, **extra):
    return {"ColumnName": name, "ColumnFriendlyName": friendly, "ColumnDescription": description,
            "ColumnSynonyms": synonyms, "ColumnDataRole": "DIMENSION", "IsIncludedInTopic": True,
            **extra}


def topic_measure(name, friendly, description, synonyms, aggregation, not_allowed, **extra):
    return {"ColumnName": name, "ColumnFriendlyName": friendly, "ColumnDescription": description,
            "ColumnSynonyms": synonyms, "ColumnDataRole": "MEASURE", "Aggregation": aggregation,
            "NotAllowedAggregations": not_allowed, "NonAdditive": True, "IsIncludedInTopic": True,
            **extra}


def hidden(*names):
    return [{"ColumnName": n, "IsIncludedInTopic": False} for n in names]


PERCENT_FORMAT = {"DisplayFormat": "NUMBER", "DisplayFormatOptions": {
    "FractionDigits": 1, "Suffix": "%"}}

ROOM_SYNONYMS = ["room", "space", "location", "room id", "NHHB room", "teaching room"]


def topic(occupancy_arn, coverage_arn):
    occupancy_columns = [
        dimension("room_id", "Room", "Room, e.g. NHHB.2.14 (building NHHB, floor 2, room 14).",
                  ROOM_SYNONYMS),
        dimension("room_code", "Room code", "Archibus room code without the building, e.g. 2.14.",
                  ["room number", "code"]),
        dimension("floor", "Floor", "Floor of the NHHB building.",
                  ["floor", "level", "storey", "zone"],
                  CellValueSynonyms=[{"CellValue": "Ground floor", "Synonyms": ["ground", "G", "floor G", "level 0"]},
                                     {"CellValue": "Floor 1", "Synonyms": ["first floor", "level 1"]},
                                     {"CellValue": "Floor 2", "Synonyms": ["second floor", "level 2"]}]),
        dimension("room_type", "Room type", "Archibus room type, e.g. COMPUTERS, LECTURE, MEETING.",
                  ["type of room", "room category", "space type"]),
        dimension("department", "Department", "Department the room belongs to in Archibus.",
                  ["department", "school", "owner"]),
        dimension("sensor_type", "Sensor type",
                  "Door counter (counts people every five minutes) or presence sensor (occupied or not).",
                  ["sensor", "device type"]),
        dimension("local_at", "Time", "When the reading was taken, in UK local time.",
                  ["time", "date", "when", "timestamp", "reading time"], TimeGranularity="MINUTE"),
        dimension("local_date", "Date", "UK date of the reading.", ["day", "date"],
                  TimeGranularity="DAY"),
        dimension("weekday", "Weekday", "Day of the week, 1 Mon to 7 Sun.",
                  ["day of week", "weekday"]),
        dimension("hour_of_day", "Hour", "Hour of the day, 0 to 23, UK local time.",
                  ["hour", "time of day"]),
        dimension("core_hours", "Core hours", "Yes for weekdays 08:00 to 18:00 UK time.",
                  ["working hours", "teaching hours", "office hours", "daytime"]),
        dimension("reading_kind", "Reading kind",
                  "Scheduled, Midnight reset, Off-grid correction (system generated) or Presence event.",
                  ["reading type"]),
        topic_measure("people_count_scheduled", "People count",
                      "Number of people in the room from the door counters, at five-minute intervals. "
                      "Average it over time; add it up across rooms only at one moment.",
                      ["people", "occupancy", "headcount", "occupants", "how many people",
                       "number of people", "count"], "AVERAGE", ["SUM"]),
        topic_measure("utilisation_pct", "Occupancy % of capacity",
                      "People count as a percentage of the room's capacity.",
                      ["utilisation", "utilization", "how full", "occupancy rate",
                       "percent of capacity", "occupancy percentage"], "AVERAGE", ["SUM"],
                      DefaultFormatting=PERCENT_FORMAT),
        topic_measure("capacity", "Capacity", "Seats or people the room is designed for.",
                      ["capacity", "seats", "max occupancy", "size"], "MAX", ["SUM", "AVERAGE"]),
        topic_measure("presence", "Presence",
                      "Presence sensors only: 1 when someone is present, 0 when empty, sent on change.",
                      ["occupied", "in use", "presence"], "AVERAGE", ["SUM"]),
        topic_measure("area_m2", "Area (m²)", "Floor area of the room in square metres.",
                      ["area", "floor area", "square metres", "size in m2"], "MAX", ["SUM", "AVERAGE"]),
        *hidden("space_name", "archibus_rooms", "reading_at_utc", "people_count",
                "people_count_core", "utilisation_pct_core", "source_file"),
    ]
    coverage_columns = [
        dimension("room_id", "Room", "Room, e.g. NHHB.2.14.", ROOM_SYNONYMS),
        dimension("local_date", "Date", "UK date.", ["day", "date"], TimeGranularity="DAY"),
        dimension("in_service", "In service", "Yes on and after the room's first reading.",
                  ["monitored", "commissioned"]),
        topic_measure("longest_gap_hours", "Longest gap (hours)",
                      "Longest run of missing door-counter readings within a day, in hours.",
                      ["gap", "outage", "longest gap", "missing data"], "MAX", ["SUM", "AVERAGE"]),
        topic_measure("midnight_resets", "Midnight resets",
                      "System-generated zero readings at 00:00.", ["resets"], "SUM", []),
        topic_measure("corrections", "Correction readings",
                      "Zero readings SMARTviz inserts off the five-minute grid to correct drift.",
                      ["corrections", "adjustments"], "SUM", []),
        *hidden("room_code", "space_name", "capacity", "first_day", "expected_slots",
                "observed_slots", "longest_gap_slots"),
    ]
    return {
        "Name": "Building Occupancy",
        "Description": "Room occupancy in the NHHB building from SMARTviz door counters and "
                       "presence sensors, with data-quality coverage.",
        "UserExperienceVersion": "NEW_READER_EXPERIENCE",
        "DataSets": [
            {"DatasetArn": occupancy_arn, "DatasetName": "Room occupancy readings",
             "DatasetDescription": "One row per sensor reading: room, UK time, people count or presence, "
                                   "capacity, floor, room type and department.",
             "DataAggregation": {"DatasetRowDateGranularity": "MINUTE", "DefaultDateColumnName": "local_at"},
             "Columns": occupancy_columns},
            {"DatasetArn": coverage_arn, "DatasetName": "Occupancy data coverage",
             "DatasetDescription": "One row per door-counter room per day: readings expected and received, "
                                   "longest gap, resets and corrections.",
             "DataAggregation": {"DatasetRowDateGranularity": "DAY", "DefaultDateColumnName": "local_date"},
             "Columns": coverage_columns,
             "CalculatedFields": [{
                 "CalculatedFieldName": "Coverage %",
                 "CalculatedFieldDescription": "Share of expected five-minute door-counter readings "
                                               "that were received.",
                 "Expression": COVERAGE_EXPRESSION,
                 "CalculatedFieldSynonyms": ["coverage", "data coverage", "completeness",
                                             "data completeness"],
                 "IsIncludedInTopic": True, "ColumnDataRole": "MEASURE", "NonAdditive": True,
                 "DefaultFormatting": PERCENT_FORMAT}]},
        ],
    }


def main(out, data_source_arn, occupancy_arn, coverage_arn, catalog, namespace):
    out = Path(out)
    curated = f'"{catalog}"."{namespace}"'
    files = {
        "occupancy-table.json": physical_table(
            "room_occupancy", "room_occupancy.sql", OCCUPANCY_COLUMNS, data_source_arn, curated),
        "coverage-table.json": physical_table(
            "occupancy_coverage", "occupancy_coverage.sql", COVERAGE_COLUMNS, data_source_arn, curated),
        "occupancy-dashboard.json": dashboard(occupancy_arn, coverage_arn),
        "topic.json": topic(occupancy_arn, coverage_arn),
    }
    for name, body in files.items():
        (out / name).write_text(json.dumps(body))


if __name__ == "__main__":
    main(*sys.argv[1:])
