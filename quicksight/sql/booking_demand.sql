-- Booking pressure: one row per NHHB room in the TimeEdit timetable per UK day per hour,
-- over the whole timetable (the exports run to August 2027), with how much of the hour
-- was booked and, where the room has a door counter, what the counter saw (ECS strategic
-- question 3.1).
--
-- Every hour of the grid is present, so unbooked time counts. Sensor columns are zero or
-- NULL where a room has no door counter or the date is outside the Smart-Vis data; the
-- booked-against-observed comparison uses only slots with a reading:
--   time booked %         = sum(booked_slots) / sum(slots)
--   booked, counted rooms = sum(booked_observed_slots) / sum(door_slots)
--   in use, counted rooms = sum(door_used_slots) / sum(door_slots)
-- Capacity comes from Smart-Vis, so rooms without a counter have none. Provisional
-- readings (see room_occupancy.sql) are left out.
-- {curated}, {provisional}, {teaching} and {non_teaching} are replaced by occupancy.py.
WITH bookings AS (
  SELECT 'Teaching' AS booking_type, room, booking_date, start_time, end_time FROM {curated}.{teaching}
  UNION ALL
  SELECT 'Non-teaching', room, booking_date, start_time, end_time FROM {curated}.{non_teaching}
), b AS (
  SELECT DISTINCT
         booking_type,
         upper(room) AS room_id,
         date_parse(booking_date || ' ' || start_time, '%d/%m/%Y %H:%i') AS start_at,
         CASE WHEN end_time LIKE '24:%'
              THEN date_parse(booking_date, '%d/%m/%Y') + INTERVAL '1' DAY
              ELSE date_parse(booking_date || ' ' || substr(end_time, 1, 5), '%d/%m/%Y %H:%i')
         END AS end_at
  FROM bookings
  WHERE upper(room) LIKE 'NHHB.%'
    AND regexp_like(booking_date, '^\d{2}/\d{2}/\d{4}$')
    AND regexp_like(start_time, '^\d{2}:\d{2}$')
    AND regexp_like(end_time, '^\d{2}:\d{2}')
), booked AS (
  SELECT room_id, slot_at, bool_or(booking_type = 'Teaching') AS teaching
  FROM b
  CROSS JOIN UNNEST(sequence(start_at, end_at - INTERVAL '5' MINUTE, INTERVAL '5' MINUTE)) AS u(slot_at)
  WHERE end_at > start_at
  GROUP BY 1, 2
), span AS (
  SELECT CAST(min(start_at) AS date) AS d0, CAST(max(start_at) AS date) AS d1 FROM b
), sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people,
         max(capacity) AS capacity
  FROM {curated}."smartvis_occupancy" s
  WHERE people_count IS NOT NULL AND minute(reading_at) % 5 = 0
    AND NOT EXISTS (SELECT 1 FROM {provisional}
                    WHERE p.room_id = upper(s.room_id) AND CAST(s.local_date AS date) < p.valid_from)
  GROUP BY 1, 2
), cap AS (
  SELECT room_id, max(capacity) AS capacity FROM sv GROUP BY 1
), rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), room AS (
  SELECT 'NHHB.' || upper(room_code) AS room_id,
         max(floor_code) AS floor_code,
         array_join(array_sort(array_agg(DISTINCT room_type)), ' / ') AS room_type
  FROM rooms_latest GROUP BY 1
), grid AS (
  -- Days, then the five-minute slots of each: a year of slots in one sequence would
  -- pass Athena's 50,000-entry limit.
  SELECT t.room_id, date_add('minute', m.k, d.d) AS slot_at
  FROM (SELECT DISTINCT room_id FROM b) t
  CROSS JOIN span
  CROSS JOIN UNNEST(sequence(CAST(span.d0 AS timestamp), CAST(span.d1 AS timestamp), INTERVAL '1' DAY)) AS d(d)
  CROSS JOIN UNNEST(sequence(0, 1435, 5)) AS m(k)
)
SELECT g.room_id,
       CASE WHEN r.floor_code IS NULL THEN 'Not in room list'
            WHEN r.floor_code = 'G' THEN 'Ground floor' ELSE 'Floor ' || r.floor_code END AS floor,
       coalesce(r.room_type, 'Unknown') AS room_type,
       c.capacity,
       CASE WHEN c.room_id IS NULL THEN 'No' ELSE 'Yes' END AS has_door_counter,
       CAST(g.slot_at AS date) AS local_date,
       format('%d %s', day_of_week(g.slot_at), date_format(g.slot_at, '%a')) AS weekday,
       hour(g.slot_at) AS hour_of_day,
       CASE WHEN day_of_week(g.slot_at) <= 5 AND hour(g.slot_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       count(*) AS slots,
       count(bk.slot_at) AS booked_slots,
       count_if(bk.teaching) AS teaching_slots,
       count(sv.people) AS door_slots,
       count_if(bk.slot_at IS NOT NULL AND sv.people IS NOT NULL) AS booked_observed_slots,
       count_if(sv.people >= 1) AS door_used_slots,
       max(sv.people) AS door_peak,
       sum(sv.people) AS people_sum
FROM grid g
LEFT JOIN booked bk ON bk.room_id = g.room_id AND bk.slot_at = g.slot_at
LEFT JOIN sv ON sv.room_id = g.room_id AND sv.slot_at = g.slot_at
LEFT JOIN cap c ON c.room_id = g.room_id
LEFT JOIN room r ON r.room_id = g.room_id
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
