#!/usr/bin/env bash
# Builds the QuickSight content that sits on top of AnalyticsStack's Athena data source:
# the RLS rules dataset, the SPICE dataset over the flat table (with RLS attached), and
# the two dashboards. Not CloudFormation-managed - run once per account after the stack
# is deployed and SPICE capacity exists. Safe to re-run: each resource is created, or
# updated if it already exists.
#
# Usage: ACCOUNT_ID=552969575108 QS_USER=CirrusHQSupportAccess/Jim_Donachie ./quicksight/build.sh
# (QS_USER is the QuickSight user name; an IAM-federated user is "<role>/<session name>".)
set -euo pipefail

ACCOUNT_ID=${ACCOUNT_ID:?set ACCOUNT_ID}
QS_USER=${QS_USER:?set QS_USER (QuickSight user name that will own the content)}
REGION=${REGION:-eu-west-2}
PREFIX=${PREFIX:-building-data-dev}
VIEWS_DB=${VIEWS_DB:-building_data_views}

DS_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:datasource/${PREFIX}-athena"
OWNER="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:user/default/${QS_USER}"
RLS_ID="${PREFIX}-rls-rules"
DATA_ID="${PREFIX}-bms-environmental"
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
qs() { aws quicksight "$@" --aws-account-id "$ACCOUNT_ID" --region "$REGION"; }

cat > "$TMP/dataset-perms.json" <<EOF
[{"Principal":"$OWNER","Actions":["quicksight:DescribeDataSet","quicksight:DescribeDataSetPermissions","quicksight:PassDataSet","quicksight:DescribeIngestion","quicksight:ListIngestions","quicksight:UpdateDataSet","quicksight:DeleteDataSet","quicksight:CreateIngestion","quicksight:CancelIngestion","quicksight:UpdateDataSetPermissions"]}]
EOF
cat > "$TMP/dashboard-perms.json" <<EOF
[{"Principal":"$OWNER","Actions":["quicksight:DescribeDashboard","quicksight:ListDashboardVersions","quicksight:UpdateDashboardPermissions","quicksight:QueryDashboard","quicksight:UpdateDashboard","quicksight:DeleteDashboard","quicksight:DescribeDashboardPermissions","quicksight:UpdateDashboardPublishedVersion"]}]
EOF

# create-or-update for datasets: the update call takes the same arguments minus permissions.
upsert_dataset() {
  local id=$1; shift
  if qs describe-data-set --data-set-id "$id" >/dev/null 2>&1; then
    echo "updating dataset $id"
    qs update-data-set --data-set-id "$id" "$@" --query Status --output text
  else
    echo "creating dataset $id"
    qs create-data-set --data-set-id "$id" "$@" --permissions "file://$TMP/dataset-perms.json" --query Status --output text
  fi
}

# --- Row-level security rules -------------------------------------------------------
# GroupName -> room_id. A NULL room_id grants every row (estates-all). Real rules replace
# these once the Customer agrees who sees which rooms.
for g in estates-all estates-floor2; do
  qs create-group --namespace default --group-name "$g" >/dev/null 2>&1 || true
done
qs create-group-membership --namespace default --group-name estates-all --member-name "$QS_USER" >/dev/null 2>&1 || true

cat > "$TMP/rls-table.json" <<EOF
{"rls":{"CustomSql":{"DataSourceArn":"$DS_ARN","Name":"rls_rules",
 "SqlQuery":"SELECT GroupName, room_id FROM (VALUES ('estates-all', CAST(NULL AS varchar)), ('estates-floor2', 'NHHB-2.14')) AS t(GroupName, room_id)",
 "Columns":[{"Name":"GroupName","Type":"STRING"},{"Name":"room_id","Type":"STRING"}]}}}
EOF
upsert_dataset "$RLS_ID" --name "Building Data RLS rules" --import-mode SPICE \
  --physical-table-map "file://$TMP/rls-table.json"

# --- Main dataset over the flat table ------------------------------------------------
# The ID is fixed: AnalyticsStack's flat-refresh state machine refreshes it by this name.
cat > "$TMP/data-table.json" <<EOF
{"flat":{"RelationalTable":{"DataSourceArn":"$DS_ARN","Catalog":"AwsDataCatalog","Schema":"$VIEWS_DB",
 "Name":"bms_environmental_flat","InputColumns":[
  {"Name":"room_id","Type":"STRING"},{"Name":"reading_at","Type":"DATETIME"},
  {"Name":"temperature_c","Type":"DECIMAL"},{"Name":"humidity_pct","Type":"DECIMAL"},
  {"Name":"source_file","Type":"STRING"},{"Name":"ingested_at","Type":"DATETIME"}]}}}
EOF
cat > "$TMP/rls.json" <<EOF
{"Arn":"arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${RLS_ID}","PermissionPolicy":"GRANT_ACCESS","FormatVersion":"VERSION_1","Status":"ENABLED"}
EOF
upsert_dataset "$DATA_ID" --name "BMS environmental readings" --import-mode SPICE \
  --physical-table-map "file://$TMP/data-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"

# --- Dashboards ----------------------------------------------------------------------
DATA_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${DATA_ID}"
col() { echo "{\"DataSetIdentifier\":\"bms\",\"ColumnName\":\"$1\"}"; }
avg() { echo "{\"NumericalMeasureField\":{\"FieldId\":\"$1\",\"Column\":$(col "$2"),\"AggregationFunction\":{\"SimpleNumericalAggregation\":\"AVERAGE\"}}}"; }
title() { echo "{\"Visibility\":\"VISIBLE\",\"FormatText\":{\"PlainText\":\"$1\"}}"; }

cat > "$TMP/overview.json" <<EOF
{"DataSetIdentifierDeclarations":[{"Identifier":"bms","DataSetArn":"$DATA_ARN"}],
 "Sheets":[{"SheetId":"overview","Name":"Overview","Visuals":[
  {"LineChartVisual":{"VisualId":"temp-trend","Title":$(title "Temperature trend (hourly average, °C)"),
   "ChartConfiguration":{"FieldWells":{"LineChartAggregatedFieldWells":{
    "Category":[{"DateDimensionField":{"FieldId":"trend-hour","Column":$(col reading_at),"DateGranularity":"HOUR"}}],
    "Values":[$(avg trend-temp temperature_c)],
    "Colors":[{"CategoricalDimensionField":{"FieldId":"trend-room","Column":$(col room_id)}}]}}}}},
  {"TableVisual":{"VisualId":"latest-readings","Title":$(title "Latest readings"),
   "ChartConfiguration":{"FieldWells":{"TableUnaggregatedFieldWells":{"Values":[
     {"FieldId":"t-room","Column":$(col room_id)},{"FieldId":"t-at","Column":$(col reading_at)},
     {"FieldId":"t-temp","Column":$(col temperature_c)},{"FieldId":"t-rh","Column":$(col humidity_pct)}]}},
    "SortConfiguration":{"RowSort":[{"FieldSort":{"FieldId":"t-at","Direction":"DESC"}}]}}}}]}]}
EOF

cat > "$TMP/status.json" <<EOF
{"DataSetIdentifierDeclarations":[{"Identifier":"bms","DataSetArn":"$DATA_ARN"}],
 "Sheets":[{"SheetId":"status","Name":"Room status","Visuals":[
  {"KPIVisual":{"VisualId":"avg-temp","Title":$(title "Average temperature (°C)"),
   "ChartConfiguration":{"FieldWells":{"Values":[$(avg kpi-temp temperature_c)]}}}},
  {"BarChartVisual":{"VisualId":"room-temp","Title":$(title "Average temperature by room (°C)"),
   "ChartConfiguration":{"Orientation":"HORIZONTAL","FieldWells":{"BarChartAggregatedFieldWells":{
    "Category":[{"CategoricalDimensionField":{"FieldId":"bar-room","Column":$(col room_id)}}],
    "Values":[$(avg bar-temp temperature_c)]}}}}}]}]}
EOF

upsert_dashboard() {
  local id=$1 name=$2 def=$3
  if qs describe-dashboard --dashboard-id "$id" >/dev/null 2>&1; then
    echo "updating dashboard $id"
    local v
    v=$(qs update-dashboard --dashboard-id "$id" --name "$name" --definition "file://$def" --query VersionArn --output text)
    qs update-dashboard-published-version --dashboard-id "$id" --version-number "${v##*/}" >/dev/null
  else
    echo "creating dashboard $id"
    qs create-dashboard --dashboard-id "$id" --name "$name" --definition "file://$def" \
      --permissions "file://$TMP/dashboard-perms.json" --query CreationStatus --output text
  fi
}
upsert_dashboard "${PREFIX}-environmental-overview" "Building Environmental Overview" "$TMP/overview.json"
upsert_dashboard "${PREFIX}-room-status" "Room Environmental Status" "$TMP/status.json"
