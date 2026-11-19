-- Data-quality indicators for the door-counter rooms: one row per room per UK day,
-- following the definitions in SMARTviz's handover notes (2026-11-13).
--
--   coverage % = sum(observed_slots) / sum(expected_slots), worked out in QuickSight
--                over whatever rooms and days are selected, never an average of
--                per-room percentages.
--   expected_slots: five-minute slots in that UK day, timezone-aware, so 300 on the
--                day the clocks go back and 276 on the day they go forward.
--   observed_slots: distinct on-grid slots with a people count. Off-grid correction
--                readings are not counted; the 00:00 reset is (it is a scheduled slot)
--                and is also counted separately in midnight_resets.
--   longest_gap_slots: the longest run of missing slots within the day, including
--                before the first and after the last reading. A gap running over
--                midnight shows as two gaps, one in each day.
--   in_service: the day is on or after the room's first reading in the data, so
--                "reporting-period" and "in-service" coverage can both be shown.
--
-- Every room-day from the first to the last day in the data is listed, including
-- days with no readings at all (observed_slots 0). Presence sensors report when the
-- state changes, not on a five-minute schedule, so the five-minute indicators do not
-- apply to them: their rooms are listed, one row per day, with the indicators left
-- empty, which the dashboard shows as N/A (agreed with Oxford Brookes, 18 Nov).
-- corrections are inferred from their timing; SMARTviz does not mark them.
-- {curated} is replaced by build.sh, as in room_occupancy.sql.
WITH o AS (
  SELECT room_id, room_code, space_name, capacity, reading_at, local_time,
         CAST(local_date AS date) AS d
  FROM {curated}."smartvis_occupancy"
  WHERE people_count IS NOT NULL
), rooms AS (
  SELECT room_id, room_code, max(space_name) AS space_name, max(capacity) AS capacity,
         min(d) AS first_day
  FROM o GROUP BY room_id, room_code
), days AS (
  -- This engine's sequence() takes timestamps, not dates.
  SELECT CAST(t AS date) AS d
  FROM (SELECT min(d) AS d0, max(d) AS d1 FROM o)
  CROSS JOIN UNNEST(sequence(CAST(d0 AS timestamp), CAST(d1 AS timestamp), INTERVAL '1' DAY)) AS u(t)
), grid AS (
  SELECT r.room_id, r.room_code, r.space_name, r.capacity, r.first_day, days.d,
         with_timezone(CAST(days.d AS timestamp), 'Europe/London') AS day_start,
         with_timezone(CAST(days.d + INTERVAL '1' DAY AS timestamp), 'Europe/London') AS day_end
  FROM rooms r CROSS JOIN days
), slots AS (
  SELECT DISTINCT o.room_id, o.d, date_diff('minute', g.day_start, o.reading_at) / 5 AS slot
  FROM o JOIN grid g ON g.room_id = o.room_id AND g.d = o.d
  WHERE minute(o.reading_at) % 5 = 0
), gaps AS (
  SELECT room_id, d, slot,
         slot - coalesce(lag(slot) OVER (PARTITION BY room_id, d ORDER BY slot), -1) - 1 AS gap_before
  FROM slots
), observed AS (
  SELECT room_id, d, count(*) AS observed_slots, max(gap_before) AS longest_inner_gap,
         max(slot) AS last_slot
  FROM gaps GROUP BY room_id, d
), events AS (
  SELECT room_id, d,
         count_if(local_time = '00:00') AS midnight_resets,
         count_if(minute(reading_at) % 5 <> 0) AS corrections
  FROM o GROUP BY room_id, d
), scored AS (
  SELECT g.*, date_diff('minute', g.day_start, g.day_end) / 5 AS expected_slots,
         p.observed_slots, p.longest_inner_gap, p.last_slot,
         e.midnight_resets, e.corrections
  FROM grid g
  LEFT JOIN observed p ON p.room_id = g.room_id AND p.d = g.d
  LEFT JOIN events e ON e.room_id = g.room_id AND e.d = g.d
)
SELECT room_id,
       room_code,
       space_name,
       'Door counter' AS sensor_type,
       capacity,
       d AS local_date,
       first_day,
       CASE WHEN d >= first_day THEN 'Yes' ELSE 'No' END AS in_service,
       expected_slots,
       coalesce(observed_slots, 0) AS observed_slots,
       CASE WHEN observed_slots IS NULL THEN expected_slots
            ELSE greatest(longest_inner_gap, expected_slots - 1 - last_slot) END AS longest_gap_slots,
       (CASE WHEN observed_slots IS NULL THEN expected_slots
             ELSE greatest(longest_inner_gap, expected_slots - 1 - last_slot) END) * 5 / 60.0 AS longest_gap_hours,
       coalesce(midnight_resets, 0) AS midnight_resets,
       coalesce(corrections, 0) AS corrections
FROM scored
UNION ALL
SELECT pr.room_id, pr.room_code, pr.space_name, 'Presence sensor', pr.capacity, days.d,
       pr.first_day, CASE WHEN days.d >= pr.first_day THEN 'Yes' ELSE 'No' END,
       CAST(NULL AS bigint), CAST(NULL AS bigint), CAST(NULL AS bigint), CAST(NULL AS double),
       CAST(NULL AS bigint), CAST(NULL AS bigint)
FROM (
  SELECT room_id, max(room_code) AS room_code, max(space_name) AS space_name,
         max(capacity) AS capacity, min(CAST(local_date AS date)) AS first_day
  FROM {curated}."smartvis_occupancy"
  GROUP BY room_id
  HAVING count(people_count) = 0
) pr
CROSS JOIN days
