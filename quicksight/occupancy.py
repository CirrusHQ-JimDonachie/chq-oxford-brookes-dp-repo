"""Write the QuickSight definitions for the Smart-Vis occupancy content as JSON files.

build.sh calls this, then passes the files to the QuickSight API. It writes:

- occupancy-table.json, coverage-table.json: the two datasets' physical tables
  (custom SQL from sql/, over the curated catalog);
- booked-table.json, sessions-table.json: the booked-against-actual datasets, joining
  the TimeEdit timetable to the door counters;
- wifi-table.json: Wi-Fi devices against door counts per room and hour (trial Wi-Fi);
- occupancy-dashboard.json: the Room Occupancy dashboard;
- topic.json: the Q Topic, modelled on the occupancy, coverage and booking datasets.

    python3 quicksight/occupancy.py <out dir> <data source arn> <occupancy dataset arn> \
        <coverage dataset arn> <booked dataset arn> <sessions dataset arn> \
        <wifi dataset arn> <curated catalog> <namespace>
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
    ("reading_kind", "STRING"), ("data_status", "STRING"), ("provisional_reason", "STRING"),
    ("source_file", "STRING"),
]
COVERAGE_COLUMNS = [
    ("room_id", "STRING"), ("room_code", "STRING"), ("space_name", "STRING"), ("sensor_type", "STRING"),
    ("capacity", "INTEGER"), ("local_date", "DATETIME"), ("first_day", "DATETIME"),
    ("in_service", "STRING"), ("expected_slots", "INTEGER"), ("observed_slots", "INTEGER"),
    ("longest_gap_slots", "INTEGER"), ("longest_gap_hours", "DECIMAL"),
    ("midnight_resets", "INTEGER"), ("corrections", "INTEGER"),
]

BOOKED_COLUMNS = [
    ("room_id", "STRING"), ("floor", "STRING"), ("room_type", "STRING"), ("capacity", "INTEGER"),
    ("local_date", "DATETIME"), ("weekday", "STRING"), ("hour_of_day", "INTEGER"),
    ("core_hours", "STRING"), ("observed_slots", "INTEGER"), ("booked_slots", "INTEGER"),
    ("booked_used_slots", "INTEGER"), ("unbooked_slots", "INTEGER"),
    ("unbooked_used_slots", "INTEGER"), ("booked_people_sum", "DECIMAL"),
]
SESSION_COLUMNS = [
    ("booking_type", "STRING"), ("purpose", "STRING"), ("booking_id", "STRING"),
    ("room_id", "STRING"), ("floor", "STRING"), ("module", "STRING"), ("module_group", "STRING"),
    ("faculty", "STRING"), ("school", "STRING"), ("local_date", "DATETIME"),
    ("start_at", "DATETIME"), ("end_at", "DATETIME"), ("start_time", "STRING"),
    ("weekday", "STRING"), ("hour_of_day", "INTEGER"), ("core_hours", "STRING"),
    ("booked_minutes", "INTEGER"), ("booked_slots", "INTEGER"), ("observed_slots", "INTEGER"),
    ("peak_people", "DECIMAL"), ("avg_people", "DECIMAL"), ("capacity", "INTEGER"),
    ("peak_pct_capacity", "DECIMAL"), ("avg_pct_capacity", "DECIMAL"), ("outcome", "STRING"),
]
WIFI_COLUMNS = [
    ("room_id", "STRING"), ("floor", "STRING"), ("local_date", "DATETIME"), ("weekday", "STRING"),
    ("hour_of_day", "INTEGER"), ("core_hours", "STRING"), ("slots", "INTEGER"),
    ("people_sum", "DECIMAL"), ("devices_sum", "INTEGER"), ("people_in_use_slots", "INTEGER"),
    ("wifi_in_use_slots", "INTEGER"), ("agree_slots", "INTEGER"),
]
# Room periods whose Smart-Vis readings Oxford Brookes and SMARTviz could not verify:
# kept as raw counts, flagged Provisional, and left out of every derived measure.
# NHHB 1.15's September readings came from earlier sensors, not identified, that
# stopped around 24 Sep; the current sensors were installed on 14 Oct (ECS, 18 Nov).
PROVISIONAL_PERIODS = [
    ("NHHB.1.15", "2026-10-14",
     "Before the current sensors were installed on 14 Oct 2026; earlier devices not verified"),
]


def provisional_values():
    rows = ", ".join(f"('{room}', DATE '{until}', '{reason}')"
                     for room, until, reason in PROVISIONAL_PERIODS)
    return f"(VALUES {rows}) AS p(room_id, valid_from, reason)"


# TimeEdit tables the booked-vs-actual SQL reads. The trial tables until Oxford Brookes
# sign off the format; then building_data.timetable_teaching / timetable_non_teaching.
TIMETABLE_TABLES = {
    "{teaching}": '"trial_timetable_teaching"',
    "{non_teaching}": '"trial_timetable_non_teaching"',
}

COVERAGE_EXPRESSION = "sum({observed_slots}) / sum({expected_slots}) * 100"
CAVEAT = (
    "Derived from available sensor observations; gaps and system-generated "
    "corrections may affect interpretation."
)


def physical_table(name, sql_file, columns, data_source_arn, curated):
    sql = (HERE / "sql" / sql_file).read_text().replace("{curated}", curated)
    sql = sql.replace("{provisional}", provisional_values())
    for placeholder, table in TIMETABLE_TABLES.items():
        sql = sql.replace(placeholder, table)
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


def number_format(decimals=1, suffix=None, na=False):
    cfg = {"DecimalPlacesConfiguration": {"DecimalPlaces": decimals}}
    if suffix:
        cfg["Suffix"] = suffix
    if na:
        # Presence-sensor rooms have no five-minute indicators: show N/A, not blank.
        cfg["NullValueFormatConfiguration"] = {"NullString": "N/A"}
    # A numerical field's FormatConfiguration is itself a NumberFormatConfiguration.
    return {"FormatConfiguration": {"NumberDisplayFormatConfiguration": cfg}}


PCT = number_format(1, "%")
ONE_DP = number_format(1)
PCT_NA = number_format(1, "%", na=True)
ONE_DP_NA = number_format(1, na=True)
WHOLE_NA = number_format(0, na=True)


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


PEOPLE = "Door count (people)"
DEVICES = "Wi-Fi devices"
TRIAL = "Trial: Aruba sample, 21 Sep to 6 Nov, pending Oxford Brookes' DPO sign-off."


def wifi_sheet(wvd):
    """Wi-Fi devices against door counts, per room. Calculated fields carry readable
    names because legends show field names."""
    def pair(prefix):
        return [measure(f"{prefix}-p", wvd, PEOPLE, fmt=ONE_DP),
                measure(f"{prefix}-d", wvd, DEVICES, fmt=ONE_DP)]

    def two_line(visual_id, text, subtitle, category, axis_label):
        return {"LineChartVisual": {"VisualId": visual_id, **title(text, subtitle),
            "ChartConfiguration": {
                "FieldWells": {"LineChartAggregatedFieldWells": {
                    "Category": [category], "Values": pair(visual_id), "Colors": []}},
                "XAxisLabelOptions": labels(wvd, (category[next(iter(category))]["FieldId"],
                                                  category[next(iter(category))]["Column"]["ColumnName"],
                                                  axis_label)),
                "PrimaryYAxisLabelOptions": labels(wvd, (f"{visual_id}-p", PEOPLE, "Average per room")),
                "Legend": {"Visibility": "VISIBLE", "Title": {"Visibility": "HIDDEN"}},
                "Tooltip": tooltip((f"{visual_id}-p", PEOPLE), (f"{visual_id}-d", DEVICES))}}}

    return {
        "SheetId": "wifi", "Name": "Wi-Fi vs door counts",
        "FilterControls": [
            {"DateTimePicker": {"FilterControlId": "ctl-date-w", "Title": "Dates",
                                "SourceFilterId": "f-date-w", "Type": "DATE_RANGE"}},
            {"Dropdown": {"FilterControlId": "ctl-room-w", "Title": "Room",
                          "SourceFilterId": "f-room-w", "Type": "MULTI_SELECT"}},
        ],
        "Visuals": [
            kpi("w-people", "Door count", measure("w-people-v", wvd, PEOPLE, fmt=ONE_DP),
                "Average people per room, weekdays 08:00-18:00"),
            kpi("w-devices", "Wi-Fi devices", measure("w-devices-v", wvd, DEVICES, fmt=ONE_DP),
                "Average devices on each room's access points, weekdays 08:00-18:00. " + TRIAL),
            kpi("w-ratio", "Devices per person", measure("w-ratio-v", wvd, "devices_per_person",
                                                          fmt=number_format(2)),
                "Wi-Fi devices divided by door-counted people, weekdays 08:00-18:00"),
            kpi("w-agree", "Agree on whether the room is in use",
                measure("w-agree-v", wvd, "wifi_agreement_pct", fmt=PCT),
                "Five-minute readings where both or neither show someone, weekdays 08:00-18:00"),
            two_line("w-day", "Door count and Wi-Fi devices by day",
                     "Average per room, weekdays 08:00-18:00. " + TRIAL,
                     date_dim("w-day-x", wvd, "local_date"), "Date"),
            two_line("w-hour", "Door count and Wi-Fi devices by hour of the day",
                     "Average per room, Monday to Friday, UK local time",
                     {"NumericalDimensionField": {"FieldId": "w-hour-x",
                                                  "Column": col(wvd, "hour_of_day")}},
                     "Hour starting"),
            {"BarChartVisual": {"VisualId": "w-rooms", **title(
                "Door count and Wi-Fi devices by room",
                "Average per five-minute reading, weekdays 08:00-18:00; rooms with an access point in the room list"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "BarsArrangement": "CLUSTERED",
                    "FieldWells": {"BarChartAggregatedFieldWells": {
                        # Bars colour their measures in the opposite order to the lines, so
                        # devices go first here to keep each measure's colour the same.
                        "Category": [dim("w-rooms-r", wvd, "room_id")],
                        "Values": pair("w-rooms")[::-1]}},
                    "SortConfiguration": {"CategorySort": [
                        {"FieldSort": {"FieldId": "w-rooms-r", "Direction": "ASC"}}]},
                    "CategoryLabelOptions": labels(wvd, ("w-rooms-r", "room_id", "Room")),
                    "ValueLabelOptions": labels(wvd, ("w-rooms-p", PEOPLE, "Average per reading")),
                    "Legend": {"Visibility": "VISIBLE", "Title": {"Visibility": "HIDDEN"}},
                    "Tooltip": tooltip(("w-rooms-r", "Room"), ("w-rooms-p", PEOPLE),
                                       ("w-rooms-d", DEVICES))}}},
            {"TableVisual": {"VisualId": "w-table", **title(
                "How closely Wi-Fi follows the door counter, by room",
                "Weekdays 08:00-18:00. Few devices in a full room, or many in an empty one, "
                "suggests the access point serves other space or the room list maps it wrongly"),
                "ChartConfiguration": {
                    "FieldWells": {"TableAggregatedFieldWells": {
                        "GroupBy": [dim("wt-room", wvd, "room_id")],
                        "Values": [measure("wt-p", wvd, PEOPLE, fmt=ONE_DP),
                                   measure("wt-d", wvd, DEVICES, fmt=ONE_DP),
                                   measure("wt-r", wvd, "devices_per_person", fmt=number_format(2)),
                                   measure("wt-a", wvd, "wifi_agreement_pct", fmt=PCT)]}},
                    "SortConfiguration": {"RowSort": [
                        {"FieldSort": {"FieldId": "wt-a", "Direction": "ASC"}}]},
                    "FieldOptions": {"SelectedFieldOptions": [
                        {"FieldId": "wt-room", "CustomLabel": "Room"},
                        {"FieldId": "wt-p", "CustomLabel": "Door count"},
                        {"FieldId": "wt-d", "CustomLabel": "Wi-Fi devices"},
                        {"FieldId": "wt-r", "CustomLabel": "Devices per person"},
                        {"FieldId": "wt-a", "CustomLabel": "Agree on in use"},
                    ]}}}},
        ],
        "Layouts": grid(("w-people", 9, 6), ("w-devices", 9, 6), ("w-ratio", 9, 6),
                        ("w-agree", 9, 6), ("w-day", 36, 10), ("w-hour", 18, 12),
                        ("w-rooms", 18, 12), ("w-table", 36, 12)),
    }


def dashboard(occupancy_arn, coverage_arn, booked_arn, session_arn, wifi_arn):
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
            kpi("q-corr", "Correction readings (inferred)", measure("q-cor", cov, "corrections", "SUM"),
                "Zero readings off the five-minute grid, taken to be drift corrections; SMARTviz does not mark them"),
            {"TableVisual": {"VisualId": "q-rooms", **title(
                "Coverage by room", CAVEAT + " Presence-sensor rooms report on change, "
                "so the five-minute indicators are N/A for them."),
                "ChartConfiguration": {
                    "FieldWells": {"TableAggregatedFieldWells": {
                        "GroupBy": [dim("qr-room", cov, "room_id")],
                        "Values": [
                            measure("qr-cov", cov, "coverage_pct", fmt=PCT_NA),
                            measure("qr-svc", cov, "in_service_coverage_pct", fmt=PCT_NA),
                            measure("qr-obs", cov, "observed_slots", "SUM", WHOLE_NA),
                            measure("qr-exp", cov, "expected_slots", "SUM", WHOLE_NA),
                            measure("qr-gap", cov, "longest_gap_hours", "MAX", ONE_DP_NA),
                            measure("qr-res", cov, "midnight_resets", "SUM", WHOLE_NA),
                            measure("qr-cor", cov, "corrections", "SUM", WHOLE_NA),
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
                        {"FieldId": "qr-cor", "CustomLabel": "Corrections (inferred)"},
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

    # --- Booked vs actual: TimeEdit bookings against the door counters -------------
    # Calculated fields carry readable names because legends show field names.
    bva, ses, wvd = "bva", "ses", "wvd"
    used_booked = "Booked time in use"
    used_unbooked = "Unbooked time in use"
    booked_sheet = {
        "SheetId": "booked", "Name": "Booked vs actual",
        "FilterControls": [
            {"DateTimePicker": {"FilterControlId": "ctl-date-b", "Title": "Dates",
                                "SourceFilterId": "f-date-b", "Type": "DATE_RANGE"}},
            {"Dropdown": {"FilterControlId": "ctl-room-b", "Title": "Room",
                          "SourceFilterId": "f-room-b", "Type": "MULTI_SELECT"}},
            {"Dropdown": {"FilterControlId": "ctl-outcome", "Title": "Booking outcome",
                          "SourceFilterId": "f-outcome", "Type": "MULTI_SELECT"}},
        ],
        "Visuals": [
            kpi("b-empty", "Bookings with nobody counted",
                measure("b-empty-v", ses, "empty_bookings_pct", fmt=PCT),
                "Of bookings with sensor data for at least half their length"),
            kpi("b-used", "Booked time in use",
                measure("b-used-v", bva, used_booked, fmt=PCT),
                "Share of booked five-minute readings counting anyone; weekdays 08:00-18:00"),
            kpi("b-unbooked", "Occupied time with no booking",
                measure("b-unb-v", bva, "occupied_unbooked_pct", fmt=PCT),
                "Share of readings counting anyone that fall outside a booking; weekdays 08:00-18:00"),
            kpi("b-booked", "Time booked",
                measure("b-bk-v", bva, "booked_time_pct", fmt=PCT),
                "Share of readings inside a booking; weekdays 08:00-18:00"),
            {"BarChartVisual": {"VisualId": "b-rooms", **title(
                "Booked and unbooked time in use, by room",
                "Weekdays 08:00-18:00; rooms in the TimeEdit timetable with a door counter"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "BarsArrangement": "CLUSTERED",
                    "FieldWells": {"BarChartAggregatedFieldWells": {
                        "Category": [dim("br-room", bva, "room_id")],
                        "Values": [measure("br-b", bva, used_booked, fmt=PCT),
                                   measure("br-u", bva, used_unbooked, fmt=PCT)],
                    }},
                    "SortConfiguration": {"CategorySort": [
                        {"FieldSort": {"FieldId": "br-room", "Direction": "ASC"}}]},
                    "CategoryLabelOptions": labels(bva, ("br-room", "room_id", "Room")),
                    "ValueLabelOptions": labels(bva, ("br-b", used_booked, "% of readings counting anyone")),
                    "Legend": {"Visibility": "VISIBLE", "Title": {"Visibility": "HIDDEN"}},
                    "Tooltip": tooltip(("br-room", "Room"), ("br-b", used_booked),
                                       ("br-u", used_unbooked))}}},
            {"BarChartVisual": {"VisualId": "b-outcomes", **title(
                "Bookings by room and outcome",
                "Used: anyone counted. Empty: nobody counted. No sensor data: readings for under half the booking"),
                "ChartConfiguration": {"Orientation": "HORIZONTAL", "BarsArrangement": "STACKED",
                    "FieldWells": {"BarChartAggregatedFieldWells": {
                        "Category": [dim("bo-room", ses, "room_id")],
                        "Values": [{"CategoricalMeasureField": {
                            "FieldId": "bo-n", "Column": col(ses, "booking_id"),
                            "AggregationFunction": "COUNT"}}],
                        "Colors": [dim("bo-out", ses, "outcome")],
                    }},
                    "SortConfiguration": {"CategorySort": [
                        {"FieldSort": {"FieldId": "bo-room", "Direction": "ASC"}}]},
                    "CategoryLabelOptions": labels(ses, ("bo-room", "room_id", "Room")),
                    "ValueLabelOptions": labels(ses, ("bo-n", "booking_id", "Bookings")),
                    "ColorLabelOptions": labels(ses, ("bo-out", "outcome", "Outcome")),
                    "Tooltip": tooltip(("bo-room", "Room"), ("bo-out", "Outcome"),
                                       ("bo-n", "Bookings"))}}},
            {"HeatMapVisual": {"VisualId": "b-heat", **title(
                "Unbooked time in use, by weekday and hour",
                "Share of unbooked readings counting anyone, timetabled rooms, UK local time"),
                "ChartConfiguration": {
                    "FieldWells": {"HeatMapAggregatedFieldWells": {
                        "Rows": [dim("bh-day", bva, "weekday")],
                        "Columns": [{"NumericalDimensionField": {
                            "FieldId": "bh-hour", "Column": col(bva, "hour_of_day")}}],
                        "Values": [measure("bh-val", bva, used_unbooked, fmt=PCT)],
                    }},
                    "RowLabelOptions": labels(bva, ("bh-day", "weekday", "Weekday")),
                    "ColumnLabelOptions": labels(bva, ("bh-hour", "hour_of_day", "Hour starting")),
                    "Legend": {"Visibility": "VISIBLE", "Title": {
                        "Visibility": "VISIBLE", "CustomLabel": "% in use"}},
                    "Tooltip": tooltip(("bh-day", "Weekday"), ("bh-hour", "Hour starting"),
                                       ("bh-val", used_unbooked))}}},
            {"TableVisual": {"VisualId": "b-table", **title(
                "Bookings and what the door counter saw",
                "One row per booking per room; use the Booking outcome control to list the empty ones"),
                "ChartConfiguration": {
                    "FieldWells": {"TableUnaggregatedFieldWells": {"Values": [
                        {"FieldId": "bt-start", "Column": col(ses, "start_at")},
                        {"FieldId": "bt-room", "Column": col(ses, "room_id")},
                        {"FieldId": "bt-type", "Column": col(ses, "booking_type")},
                        {"FieldId": "bt-purpose", "Column": col(ses, "purpose")},
                        # Non-teaching bookings have no module: show a dash, not "null".
                        {"FieldId": "bt-module", "Column": col(ses, "module"),
                         "FormatConfiguration": {"StringFormatConfiguration": {
                             "NullValueFormatConfiguration": {"NullString": "-"}}}},
                        {"FieldId": "bt-min", "Column": col(ses, "booked_minutes")},
                        {"FieldId": "bt-cap", "Column": col(ses, "capacity")},
                        {"FieldId": "bt-peak", "Column": col(ses, "peak_people")},
                        {"FieldId": "bt-avg", "Column": col(ses, "avg_people"),
                         "FormatConfiguration": {"NumberFormatConfiguration": ONE_DP}},
                        {"FieldId": "bt-out", "Column": col(ses, "outcome")},
                    ]}},
                    "SortConfiguration": {"RowSort": [
                        {"FieldSort": {"FieldId": "bt-start", "Direction": "DESC"}}]},
                    "FieldOptions": {"SelectedFieldOptions": [
                        {"FieldId": "bt-start", "CustomLabel": "Start"},
                        {"FieldId": "bt-room", "CustomLabel": "Room"},
                        {"FieldId": "bt-type", "CustomLabel": "Type"},
                        {"FieldId": "bt-purpose", "CustomLabel": "Purpose"},
                        {"FieldId": "bt-module", "CustomLabel": "Module"},
                        {"FieldId": "bt-min", "CustomLabel": "Minutes"},
                        {"FieldId": "bt-cap", "CustomLabel": "Capacity"},
                        {"FieldId": "bt-peak", "CustomLabel": "Peak count"},
                        {"FieldId": "bt-avg", "CustomLabel": "Average count"},
                        {"FieldId": "bt-out", "CustomLabel": "Outcome"},
                    ]}}}},
        ],
        "Layouts": grid(("b-empty", 9, 6), ("b-used", 9, 6), ("b-unbooked", 9, 6),
                        ("b-booked", 9, 6), ("b-rooms", 18, 14), ("b-outcomes", 18, 14),
                        ("b-heat", 36, 10), ("b-table", 36, 14)),
    }
    # Unbooked use only means something while rooms are in demand, so the hourly
    # booked-vs-actual visuals are limited to weekdays 08:00-18:00, except the heatmap,
    # which shows every hour so evening use is visible.
    core_only = {"FilterGroupId": "fg-booked-core", "CrossDataset": "SINGLE_DATASET",
                 "Status": "ENABLED",
                 "Filters": [{"CategoryFilter": {
                     "FilterId": "f-booked-core", "Column": col(bva, "core_hours"),
                     "Configuration": {"FilterListConfiguration": {
                         "MatchOperator": "CONTAINS", "CategoryValues": ["Yes"]}}}}],
                 "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                     {"SheetId": "booked", "Scope": "SELECTED_VISUALS",
                      "VisualIds": ["b-used", "b-unbooked", "b-booked", "b-rooms"]}]}}}
    outcome_filter = {"FilterGroupId": "fg-outcome", "CrossDataset": "SINGLE_DATASET",
                      "Status": "ENABLED",
                      "Filters": [{"CategoryFilter": {
                          "FilterId": "f-outcome", "Column": col(ses, "outcome"),
                          "Configuration": {"FilterListConfiguration": {
                              "MatchOperator": "CONTAINS", "SelectAllOptions": "FILTER_ALL_VALUES",
                              "NullOption": "NON_NULLS_ONLY"}}}}],
                      "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                          {"SheetId": "booked", "Scope": "SELECTED_VISUALS",
                           "VisualIds": ["b-table"]}]}}}

    return {
        "DataSetIdentifierDeclarations": [
            {"Identifier": occ, "DataSetArn": occupancy_arn},
            {"Identifier": cov, "DataSetArn": coverage_arn},
            {"Identifier": bva, "DataSetArn": booked_arn},
            {"Identifier": ses, "DataSetArn": session_arn},
            {"Identifier": wvd, "DataSetArn": wifi_arn},
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
            {"DataSetIdentifier": bva, "Name": used_booked,
             "Expression": "sum({booked_used_slots}) / sum({booked_slots}) * 100"},
            {"DataSetIdentifier": bva, "Name": used_unbooked,
             "Expression": "sum({unbooked_used_slots}) / sum({unbooked_slots}) * 100"},
            {"DataSetIdentifier": bva, "Name": "occupied_unbooked_pct",
             "Expression": "sum({unbooked_used_slots}) / "
                           "(sum({booked_used_slots}) + sum({unbooked_used_slots})) * 100"},
            {"DataSetIdentifier": bva, "Name": "booked_time_pct",
             "Expression": "sum({booked_slots}) / sum({observed_slots}) * 100"},
            {"DataSetIdentifier": wvd, "Name": PEOPLE,
             "Expression": "sum({people_sum}) / sum({slots})"},
            {"DataSetIdentifier": wvd, "Name": DEVICES,
             "Expression": "sum({devices_sum}) / sum({slots})"},
            {"DataSetIdentifier": wvd, "Name": "devices_per_person",
             "Expression": "sum({devices_sum}) / sum({people_sum})"},
            {"DataSetIdentifier": wvd, "Name": "wifi_agreement_pct",
             "Expression": "sum({agree_slots}) / sum({slots}) * 100"},
            {"DataSetIdentifier": ses, "Name": "empty_bookings_pct",
             "Expression": "sum(ifelse({outcome} = 'Empty', 1, 0)) / "
                           "sum(ifelse({outcome} = 'No sensor data', 0, 1)) * 100"},
        ],
        "FilterGroups": [
            *sheet_filters("occupancy", "f-date", "f-room", occ),
            *sheet_filters("quality", "f-date-q", "f-room-q", occ),
            *sheet_filters("booked", "f-date-b", "f-room-b", bva),
            core_only,
            outcome_filter,
            *sheet_filters("wifi", "f-date-w", "f-room-w", wvd),
            # Averages over every hour would be mostly empty nights; the hour-of-day
            # line shows all hours, Monday to Friday.
            {"FilterGroupId": "fg-wifi-core", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "Filters": [{"CategoryFilter": {
                 "FilterId": "f-wifi-core", "Column": col(wvd, "core_hours"),
                 "Configuration": {"FilterListConfiguration": {
                     "MatchOperator": "CONTAINS", "CategoryValues": ["Yes"]}}}}],
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "wifi", "Scope": "SELECTED_VISUALS",
                  "VisualIds": ["w-people", "w-devices", "w-ratio", "w-agree", "w-day",
                                "w-rooms", "w-table"]}]}}},
            {"FilterGroupId": "fg-wifi-weekdays", "CrossDataset": "SINGLE_DATASET", "Status": "ENABLED",
             "Filters": [{"CategoryFilter": {
                 "FilterId": "f-wifi-weekdays", "Column": col(wvd, "weekday"),
                 "Configuration": {"FilterListConfiguration": {
                     "MatchOperator": "CONTAINS",
                     "CategoryValues": ["1 Mon", "2 Tue", "3 Wed", "4 Thu", "5 Fri"]}}}}],
             "ScopeConfiguration": {"SelectedSheets": {"SheetVisualScopingConfigurations": [
                 {"SheetId": "wifi", "Scope": "SELECTED_VISUALS", "VisualIds": ["w-hour"]}]}}},
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
        "Sheets": [occupancy_sheet, booked_sheet, wifi_sheet(wvd), quality_sheet],
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


def topic_calc(name, description, expression, synonyms, percent=True):
    field = {"CalculatedFieldName": name, "CalculatedFieldDescription": description,
             "Expression": expression, "CalculatedFieldSynonyms": synonyms,
             "IsIncludedInTopic": True, "ColumnDataRole": "MEASURE", "NonAdditive": True}
    if percent:
        field["DefaultFormatting"] = PERCENT_FORMAT
    return field


CORE_HOURS = dimension("core_hours", "Core hours", "Yes for weekdays 08:00 to 18:00 UK time.",
                       ["working hours", "teaching hours", "office hours", "daytime"])


def topic(occupancy_arn, coverage_arn, booked_arn, session_arn):
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
                  "Scheduled, Midnight reset, Off-grid correction (inferred, system generated) or Presence event.",
                  ["reading type"]),
        dimension("data_status", "Data status",
                  "Validated, or Provisional for readings Oxford Brookes and SMARTviz could not verify "
                  "(NHHB 1.15 before 14 Oct 2026). Provisional readings are left out of people counts "
                  "and occupancy %.", ["provisional", "validated", "verified", "data status"]),
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
                "people_count_core", "utilisation_pct_core", "provisional_reason", "source_file"),
    ]
    coverage_columns = [
        dimension("room_id", "Room", "Room, e.g. NHHB.2.14.", ROOM_SYNONYMS),
        dimension("local_date", "Date", "UK date.", ["day", "date"], TimeGranularity="DAY"),
        dimension("in_service", "In service", "Yes on and after the room's first reading.",
                  ["monitored", "commissioned"]),
        dimension("sensor_type", "Sensor type", "Door counter, or presence sensor: coverage, gaps, "
                  "resets and corrections do not apply to presence sensors.", ["sensor", "device type"]),
        topic_measure("longest_gap_hours", "Longest gap (hours)",
                      "Longest run of missing door-counter readings within a day, in hours.",
                      ["gap", "outage", "longest gap", "missing data"], "MAX", ["SUM", "AVERAGE"]),
        topic_measure("midnight_resets", "Midnight resets",
                      "System-generated zero readings at 00:00.", ["resets"], "SUM", []),
        topic_measure("corrections", "Correction readings (inferred)",
                      "Zero readings off the five-minute grid, taken to be SMARTviz drift corrections; "
                      "SMARTviz does not mark them, so they are inferred from their timing.",
                      ["corrections", "adjustments"], "SUM", []),
        *hidden("room_code", "space_name", "capacity", "first_day", "expected_slots",
                "observed_slots", "longest_gap_slots"),
    ]
    session_columns = [
        dimension("booking_type", "Booking type", "Teaching (timetabled classes) or Non-teaching "
                  "(events, meetings, inductions, open days and other room bookings).",
                  ["type of booking", "booking kind", "class or event"],
                  CellValueSynonyms=[{"CellValue": "Teaching", "Synonyms": ["class", "lecture", "timetabled", "teaching"]},
                                     {"CellValue": "Non-teaching", "Synonyms": ["event", "meeting", "non teaching"]}]),
        dimension("purpose", "Purpose", "What a non-teaching booking was for, e.g. Induction, "
                  "Open Day, Meeting; Teaching for timetabled classes.", ["reason", "event type", "purpose"]),
        dimension("booking_id", "Booking", "TimeEdit booking id. A booking held in several rooms "
                  "has one row per room.", ["booking", "reservation", "booking id"]),
        dimension("room_id", "Room", "Booked room, e.g. NHHB.2.14.", ROOM_SYNONYMS),
        dimension("floor", "Floor", "Floor of the NHHB building.", ["floor", "level", "storey"]),
        dimension("module", "Module", "Module code of a teaching booking, e.g. ENGR5002_S12_1_202609.",
                  ["module", "course", "class", "subject"]),
        dimension("faculty", "Faculty", "Faculty or directorate that made the booking.",
                  ["faculty", "directorate"]),
        dimension("school", "School", "School or department of a teaching booking.",
                  ["school", "department"]),
        dimension("local_date", "Date", "UK date of the booking.", ["day", "date"], TimeGranularity="DAY"),
        dimension("start_at", "Start time", "When the booking starts, UK local time.",
                  ["start", "starting at", "time"], TimeGranularity="MINUTE"),
        dimension("weekday", "Weekday", "Day of the week the booking is on, 1 Mon to 7 Sun.",
                  ["day of week", "weekday"]),
        dimension("hour_of_day", "Start hour", "Hour the booking starts, 0 to 23, UK local time.",
                  ["hour", "time of day"]),
        CORE_HOURS,
        dimension("outcome", "Outcome", "What the door counter saw during the booking: Used if anyone "
                  "was counted, Empty if nobody was, No sensor data if readings cover under half of it.",
                  ["outcome", "attendance", "result", "was it used"],
                  CellValueSynonyms=[{"CellValue": "Empty", "Synonyms": ["empty", "unused", "no-show", "no show",
                                                                          "nobody came", "not used", "unattended"]},
                                     {"CellValue": "Used", "Synonyms": ["used", "attended", "occupied"]},
                                     {"CellValue": "No sensor data", "Synonyms": ["no data", "unknown"]}]),
        topic_measure("peak_people", "Peak count", "Most people the door counter saw during the booking.",
                      ["peak", "maximum people", "most people", "peak attendance"], "MAX", ["SUM"]),
        topic_measure("avg_people", "Average count", "Average people the door counter saw during the booking.",
                      ["attendance", "average people", "people", "how many people"], "AVERAGE", ["SUM"]),
        topic_measure("capacity", "Capacity", "Seats or people the room is designed for.",
                      ["capacity", "seats", "size"], "MAX", ["SUM", "AVERAGE"]),
        topic_measure("peak_pct_capacity", "Peak % of capacity",
                      "Peak count during the booking as a percentage of the room's capacity.",
                      ["how full", "fill rate", "peak utilisation"], "AVERAGE", ["SUM"],
                      DefaultFormatting=PERCENT_FORMAT),
        topic_measure("booked_minutes", "Booked minutes", "Length of the booking in minutes.",
                      ["length", "duration", "minutes booked"], "SUM", []),
        *hidden("module_group", "end_at", "start_time", "booked_slots", "observed_slots",
                "avg_pct_capacity"),
    ]
    booked_columns = [
        dimension("room_id", "Room", "Room, e.g. NHHB.2.14.", ROOM_SYNONYMS),
        dimension("floor", "Floor", "Floor of the NHHB building.", ["floor", "level", "storey"]),
        dimension("room_type", "Room type", "Archibus room type, e.g. COMPUTERS, LECTURE.",
                  ["type of room", "space type"]),
        dimension("local_date", "Date", "UK date.", ["day", "date"], TimeGranularity="DAY"),
        dimension("weekday", "Weekday", "Day of the week, 1 Mon to 7 Sun.", ["day of week", "weekday"]),
        dimension("hour_of_day", "Hour", "Hour of the day, 0 to 23, UK local time.",
                  ["hour", "time of day"]),
        CORE_HOURS,
        *hidden("capacity", "observed_slots", "booked_slots", "booked_used_slots", "unbooked_slots",
                "unbooked_used_slots", "booked_people_sum"),
    ]
    return {
        "Name": "Building Occupancy",
        "Description": "Room occupancy in the NHHB building from SMARTviz door counters and "
                       "presence sensors, with data-quality coverage, and TimeEdit bookings "
                       "compared with what the door counters saw.",
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
            {"DatasetArn": session_arn, "DatasetName": "Bookings and door counts",
             "DatasetDescription": "One row per TimeEdit booking in an NHHB door-counter room, with "
                                   "the peak and average people counted during it and whether it "
                                   "was used, empty or had no sensor data.",
             "DataAggregation": {"DatasetRowDateGranularity": "MINUTE", "DefaultDateColumnName": "start_at"},
             "Columns": session_columns,
             "CalculatedFields": [topic_calc(
                 "Empty bookings %", "Share of bookings with sensor data in which nobody was counted.",
                 "sum(ifelse({outcome} = 'Empty', 1, 0)) / sum(ifelse({outcome} = 'No sensor data', 0, 1)) * 100",
                 ["no-show rate", "empty rate", "unused bookings", "share of empty bookings"])]},
            {"DatasetArn": booked_arn, "DatasetName": "Booked vs actual by hour",
             "DatasetDescription": "One row per timetabled door-counter room per hour: how much of the "
                                   "time was booked, and how much booked and unbooked time had anyone in.",
             "DataAggregation": {"DatasetRowDateGranularity": "HOUR", "DefaultDateColumnName": "local_date"},
             "Columns": booked_columns,
             "CalculatedFields": [
                 topic_calc("Booked time in use %", "Share of booked time in which anyone was counted.",
                            "sum({booked_used_slots}) / sum({booked_slots}) * 100",
                            ["booked utilisation", "booked rooms in use", "use of booked time"]),
                 topic_calc("Unbooked time in use %", "Share of unbooked time in which anyone was counted.",
                            "sum({unbooked_used_slots}) / sum({unbooked_slots}) * 100",
                            ["unbooked use", "use without a booking", "unbooked utilisation"]),
                 topic_calc("Occupied time with no booking %",
                            "Share of the time anyone was counted that fell outside a booking.",
                            "sum({unbooked_used_slots}) / (sum({booked_used_slots}) + sum({unbooked_used_slots})) * 100",
                            ["unbooked occupancy", "use outside bookings"]),
                 topic_calc("Time booked %", "Share of the time a room was booked.",
                            "sum({booked_slots}) / sum({observed_slots}) * 100",
                            ["booking rate", "how much booked", "booked share"])]},
        ],
    }


def main(out, data_source_arn, occupancy_arn, coverage_arn, booked_arn, session_arn,
         wifi_arn, catalog, namespace):
    out = Path(out)
    curated = f'"{catalog}"."{namespace}"'
    files = {
        "occupancy-table.json": physical_table(
            "room_occupancy", "room_occupancy.sql", OCCUPANCY_COLUMNS, data_source_arn, curated),
        "coverage-table.json": physical_table(
            "occupancy_coverage", "occupancy_coverage.sql", COVERAGE_COLUMNS, data_source_arn, curated),
        "booked-table.json": physical_table(
            "booked_vs_actual", "booked_vs_actual.sql", BOOKED_COLUMNS, data_source_arn, curated),
        "sessions-table.json": physical_table(
            "booking_sessions", "booking_sessions.sql", SESSION_COLUMNS, data_source_arn, curated),
        "wifi-table.json": physical_table(
            "wifi_vs_door", "wifi_vs_door.sql", WIFI_COLUMNS, data_source_arn, curated),
        "occupancy-dashboard.json": dashboard(occupancy_arn, coverage_arn, booked_arn,
                                              session_arn, wifi_arn),
        "topic.json": topic(occupancy_arn, coverage_arn, booked_arn, session_arn),
    }
    for name, body in files.items():
        (out / name).write_text(json.dumps(body))


if __name__ == "__main__":
    main(*sys.argv[1:])
