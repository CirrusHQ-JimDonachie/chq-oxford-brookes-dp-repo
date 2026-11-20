#!/usr/bin/env bash
# Builds the QuickSight content that sits on top of AnalyticsStack's Athena data source:
# the RLS rules dataset; SPICE datasets over the curated tables (synthetic BMS readings,
# and the Smart-Vis room occupancy readings and their daily coverage), each with RLS
# attached and an hourly refresh schedule; the Room Occupancy dashboard; and the Q Topic.
# Needs python3 for occupancy.py. Not CloudFormation-managed - run once per account after the stack
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
CURATED_BUCKET=${CURATED_BUCKET:-${PREFIX}-curated}
CURATED_NS=${CURATED_NS:-building_data}

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
 "SqlQuery":"SELECT GroupName, room_id FROM (VALUES ('estates-all', CAST(NULL AS varchar)), ('estates-floor2', 'NHHB-2.14'), ('estates-floor2', 'NHHB.2.14')) AS t(GroupName, room_id)",
 "Columns":[{"Name":"GroupName","Type":"STRING"},{"Name":"room_id","Type":"STRING"}]}}}
EOF
upsert_dataset "$RLS_ID" --name "Building Data RLS rules" --import-mode SPICE \
  --physical-table-map "file://$TMP/rls-table.json"

# --- Main dataset, straight over the curated Iceberg table ----------------------------
# Reads the S3 Tables federated catalog directly (confirmed working 2026-10-21; this
# replaced an intermediate flat-table copy). Refreshed by the schedule below.
cat > "$TMP/data-table.json" <<EOF
{"curated":{"RelationalTable":{"DataSourceArn":"$DS_ARN","Catalog":"s3tablescatalog/$CURATED_BUCKET","Schema":"$CURATED_NS",
 "Name":"bms_environmental","InputColumns":[
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

# Hourly SPICE refresh, run by QuickSight itself.
cat > "$TMP/schedule.json" <<EOF
{"ScheduleId":"hourly","ScheduleFrequency":{"Interval":"HOURLY"},"RefreshType":"FULL_REFRESH"}
EOF
hourly_refresh() {
  if qs describe-refresh-schedule --data-set-id "$1" --schedule-id hourly >/dev/null 2>&1; then
    qs update-refresh-schedule --data-set-id "$1" --schedule "file://$TMP/schedule.json" --query Status --output text
  else
    qs create-refresh-schedule --data-set-id "$1" --schedule "file://$TMP/schedule.json" --query Status --output text
  fi
}
hourly_refresh "$DATA_ID"

# --- Smart-Vis occupancy datasets -----------------------------------------------------
# Custom SQL in quicksight/sql/, turned into API JSON (with the dashboard and the Q Topic)
# by occupancy.py. room-occupancy has one row per reading; occupancy-coverage one row per
# door-counter room per day, for the data-quality indicators SMARTviz asked for.
OCC_ID="${PREFIX}-room-occupancy"
COV_ID="${PREFIX}-occupancy-coverage"
OCC_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${OCC_ID}"
COV_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${COV_ID}"
# Booked against actual: the TimeEdit timetable joined to the door counters, per room
# and hour (booked-vs-actual) and per booking (booking-sessions).
BVA_ID="${PREFIX}-booked-vs-actual"
SES_ID="${PREFIX}-booking-sessions"
BVA_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${BVA_ID}"
SES_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${SES_ID}"
# Wi-Fi devices against door counts per room and hour, from the trial Wi-Fi table.
WVD_ID="${PREFIX}-wifi-vs-door"
WVD_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${WVD_ID}"
# Wi-Fi devices for every room with an access point (the Q Topic's Wi-Fi questions).
WRM_ID="${PREFIX}-wifi-rooms"
WRM_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${WRM_ID}"
# ECS strategic questions (16 Nov 2026): do SMARTviz, TimeEdit and Wi-Fi agree, per room
# and hour (source-agreement); booked demand across the timetable (booking-demand).
AGR_ID="${PREFIX}-source-agreement"
AGR_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${AGR_ID}"
DEM_ID="${PREFIX}-booking-demand"
DEM_ARN="arn:aws:quicksight:${REGION}:${ACCOUNT_ID}:dataset/${DEM_ID}"
python3 -I "$(dirname "$0")/occupancy.py" "$TMP" "$DS_ARN" "$OCC_ARN" "$COV_ARN" \
  "$BVA_ARN" "$SES_ARN" "$WVD_ARN" "$WRM_ARN" "$AGR_ARN" "$DEM_ARN" \
  "s3tablescatalog/$CURATED_BUCKET" "$CURATED_NS"
upsert_dataset "$OCC_ID" --name "Room occupancy readings" --import-mode SPICE \
  --physical-table-map "file://$TMP/occupancy-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$COV_ID" --name "Occupancy data coverage" --import-mode SPICE \
  --physical-table-map "file://$TMP/coverage-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$BVA_ID" --name "Booked vs actual by hour" --import-mode SPICE \
  --physical-table-map "file://$TMP/booked-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$SES_ID" --name "Bookings and door counts" --import-mode SPICE \
  --physical-table-map "file://$TMP/sessions-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$WVD_ID" --name "Wi-Fi vs door counts by hour" --import-mode SPICE \
  --physical-table-map "file://$TMP/wifi-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$WRM_ID" --name "Wi-Fi devices by room" --import-mode SPICE \
  --physical-table-map "file://$TMP/wifi-rooms-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$AGR_ID" --name "Source agreement by hour" --import-mode SPICE \
  --physical-table-map "file://$TMP/agreement-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
upsert_dataset "$DEM_ID" --name "Booking demand by hour" --import-mode SPICE \
  --physical-table-map "file://$TMP/demand-table.json" \
  --row-level-permission-data-set "file://$TMP/rls.json"
for id in "$OCC_ID" "$COV_ID" "$BVA_ID" "$SES_ID" "$WVD_ID" "$WRM_ID" "$AGR_ID" "$DEM_ID"; do
  hourly_refresh "$id"
done

# --- Dashboards ----------------------------------------------------------------------
# The two environmental dashboards on the synthetic BMS readings (Building Environmental
# Overview, Room Environmental Status) were removed on 2026-11-16; the BMS dataset stays
# for when real BMS data arrives.
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
# Smart-Vis occupancy: the SoW's occupancy heatmap, capacity gauge and zone breakdown,
# a forecast, and a data-quality sheet with coverage.
upsert_dashboard "${PREFIX}-room-occupancy" "Room Occupancy" "$TMP/occupancy-dashboard.json"
# ECS strategic questions 1.1 and 1.2 (do the sources agree) and 3.1 (booking pressure).
upsert_dashboard "${PREFIX}-source-agreement" "Data Source Agreement" "$TMP/agreement-dashboard.json"
upsert_dashboard "${PREFIX}-booking-pressure" "Booking Pressure" "$TMP/demand-dashboard.json"

# --- Q Topic (natural-language model) -------------------------------------------------
# The SoW's one Q Topic, modelled on the Smart-Vis occupancy, coverage, booking and Wi-Fi datasets
# (occupancy.py writes topic.json). The topic id keeps its first name; the synthetic BMS data is no longer in it.
TOPIC_ID="${PREFIX}-building-environmental"
if qs describe-topic --topic-id "$TOPIC_ID" >/dev/null 2>&1; then
  echo "updating topic $TOPIC_ID"
  qs update-topic --topic-id "$TOPIC_ID" --topic "file://$TMP/topic.json" --query Status --output text
else
  echo "creating topic $TOPIC_ID"
  qs create-topic --topic-id "$TOPIC_ID" --topic "file://$TMP/topic.json" --query Status --output text
fi
# QuickSight accepts only two exact action sets on a Topic: viewer (DescribeTopic) or this owner set.
qs update-topic-permissions --topic-id "$TOPIC_ID" --grant-permissions "[{\"Principal\":\"$OWNER\",\"Actions\":[
  \"quicksight:DescribeTopic\",\"quicksight:DescribeTopicRefresh\",\"quicksight:ListTopicRefreshSchedules\",
  \"quicksight:DescribeTopicRefreshSchedule\",\"quicksight:DeleteTopic\",\"quicksight:UpdateTopic\",
  \"quicksight:CreateTopicRefreshSchedule\",\"quicksight:DeleteTopicRefreshSchedule\",\"quicksight:UpdateTopicRefreshSchedule\",
  \"quicksight:DescribeTopicPermissions\",\"quicksight:UpdateTopicPermissions\"]}]" \
  --query Status --output text
# Re-index the Topic whenever each dataset's SPICE refreshes, rather than on a separate
# clock, and drop the schedule of any dataset no longer in the Topic.
scheduled=$(qs list-topic-refresh-schedules --topic-id "$TOPIC_ID" --query 'RefreshSchedules[].DatasetArn' --output text 2>/dev/null || true)
for arn in $scheduled; do
  case " $OCC_ARN $COV_ARN $BVA_ARN $SES_ARN $WRM_ARN " in
    *" $arn "*) ;;
    *) qs delete-topic-refresh-schedule --topic-id "$TOPIC_ID" --dataset-id "${arn##*/}" --query Status --output text ;;
  esac
done
for arn in "$OCC_ARN" "$COV_ARN" "$BVA_ARN" "$SES_ARN" "$WRM_ARN"; do
  case " $scheduled " in
    *" $arn "*) ;;
    *) qs create-topic-refresh-schedule --topic-id "$TOPIC_ID" --dataset-arn "$arn" \
         --refresh-schedule '{"IsEnabled":true,"BasedOnSpiceSchedule":true}' --query Status --output text ;;
  esac
done
