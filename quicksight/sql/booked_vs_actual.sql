-- Booked against actual occupancy: one row per room per UK day per hour, for the
-- door-counter rooms that appear in the TimeEdit timetable.
--
-- The unit is the five-minute Smart-Vis reading (off-grid correction readings are
-- left out). A reading is "booked" when any teaching or non-teaching booking for
-- that room covers its slot, start inclusive and end exclusive, and "used" when it
-- counts at least one person. Only slots with a reading count, so a sensor gap
-- leaves both booked and unbooked time out rather than reading as empty.
--
--   booked time used % = sum(booked_used_slots) / sum(booked_slots)
--   unbooked use %     = sum(unbooked_used_slots) / sum(unbooked_slots)
--
-- Room codes are compared in upper case: TimeEdit writes split rooms in lower case
-- (NHHB.2.03a) where Smart-Vis writes NHHB.2.03A. A booking ending "24:00:00" ends
-- at midnight. {curated}, {teaching} and {non_teaching} are replaced by occupancy.py.
WITH bookings AS (
  SELECT room, booking_date, start_time, end_time FROM {curated}.{teaching}
  UNION ALL
  SELECT room, booking_date, start_time, end_time FROM {curated}.{non_teaching}
), b AS (
  SELECT DISTINCT
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
  SELECT DISTINCT room_id, slot_at
  FROM b
  CROSS JOIN UNNEST(sequence(start_at, end_at - INTERVAL '5' MINUTE, INTERVAL '5' MINUTE)) AS u(slot_at)
  WHERE end_at > start_at
), sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people,
         max(capacity) AS capacity
  FROM {curated}."smartvis_occupancy"
  WHERE people_count IS NOT NULL AND minute(reading_at) % 5 = 0
  GROUP BY 1, 2
), rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), room AS (
  SELECT upper(room_code) AS room_code,
         array_join(array_sort(array_agg(DISTINCT room_type)), ' / ') AS room_type
  FROM rooms_latest GROUP BY 1
)
SELECT sv.room_id,
       CASE split_part(sv.room_id, '.', 2) WHEN 'G' THEN 'Ground floor'
            ELSE 'Floor ' || split_part(sv.room_id, '.', 2) END AS floor,
       coalesce(r.room_type, 'Unknown') AS room_type,
       max(sv.capacity) AS capacity,
       CAST(sv.slot_at AS date) AS local_date,
       format('%d %s', day_of_week(sv.slot_at), date_format(sv.slot_at, '%a')) AS weekday,
       hour(sv.slot_at) AS hour_of_day,
       CASE WHEN day_of_week(sv.slot_at) <= 5 AND hour(sv.slot_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       count(*) AS observed_slots,
       count_if(bk.slot_at IS NOT NULL) AS booked_slots,
       count_if(bk.slot_at IS NOT NULL AND sv.people >= 1) AS booked_used_slots,
       count_if(bk.slot_at IS NULL) AS unbooked_slots,
       count_if(bk.slot_at IS NULL AND sv.people >= 1) AS unbooked_used_slots,
       sum(CASE WHEN bk.slot_at IS NOT NULL THEN sv.people END) AS booked_people_sum
FROM sv
LEFT JOIN booked bk ON bk.room_id = sv.room_id AND bk.slot_at = sv.slot_at
LEFT JOIN room r ON r.room_code = substr(sv.room_id, 6)
WHERE sv.room_id IN (SELECT room_id FROM b)
GROUP BY 1, 2, 3, 5, 6, 7, 8
