-- One row per TimeEdit booking in an NHHB door-counter room, within the dates the
-- Smart-Vis data covers, with what the door counter saw during it.
--
--   observed_slots / booked_slots: five-minute slots in the booking with an on-grid
--       reading, of all its slots (start inclusive, end exclusive).
--   peak_people, avg_people: over the observed slots.
--   outcome: "No sensor data" when under half the slots have a reading, otherwise
--       "Used" if anyone was counted, else "Empty".
--
-- A booking held in several rooms has one row per room. Room codes are compared in
-- upper case, and a booking ending "24:00:00" ends at midnight. {curated},
-- {teaching} and {non_teaching} are replaced by occupancy.py.
WITH bookings AS (
  SELECT 'Teaching' AS booking_type, booking_id, room, booking_date, start_time, end_time,
         module, module_group, faculty, school, CAST(NULL AS varchar) AS purpose
  FROM {curated}.{teaching}
  UNION ALL
  SELECT 'Non-teaching', booking_id, room, booking_date, start_time, end_time,
         CAST(NULL AS varchar), CAST(NULL AS varchar), faculty, CAST(NULL AS varchar), purpose
  FROM {curated}.{non_teaching}
), b AS (
  SELECT DISTINCT
         booking_type, booking_id, upper(room) AS room_id,
         coalesce(purpose, 'Teaching') AS purpose, module, module_group, faculty, school,
         start_time,
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
), sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people,
         max(capacity) AS capacity
  FROM {curated}."smartvis_occupancy"
  WHERE people_count IS NOT NULL AND minute(reading_at) % 5 = 0
  GROUP BY 1, 2
), span AS (
  SELECT CAST(min(slot_at) AS date) AS d0, CAST(max(slot_at) AS date) AS d1 FROM sv
), cap AS (
  SELECT room_id, max(capacity) AS capacity FROM sv GROUP BY 1
), slots AS (
  SELECT b.*, u.slot_at, sv.people
  FROM b
  CROSS JOIN span
  CROSS JOIN UNNEST(sequence(b.start_at, b.end_at - INTERVAL '5' MINUTE, INTERVAL '5' MINUTE)) AS u(slot_at)
  LEFT JOIN sv ON sv.room_id = b.room_id AND sv.slot_at = u.slot_at
  WHERE b.end_at > b.start_at
    AND CAST(b.start_at AS date) BETWEEN span.d0 AND span.d1
    AND b.room_id IN (SELECT room_id FROM cap)
)
SELECT s.booking_type, s.purpose, s.booking_id, s.room_id,
       CASE split_part(s.room_id, '.', 2) WHEN 'G' THEN 'Ground floor'
            ELSE 'Floor ' || split_part(s.room_id, '.', 2) END AS floor,
       s.module, s.module_group, s.faculty, s.school,
       CAST(s.start_at AS date) AS local_date,
       s.start_at, s.end_at, s.start_time,
       format('%d %s', day_of_week(s.start_at), date_format(s.start_at, '%a')) AS weekday,
       hour(s.start_at) AS hour_of_day,
       CASE WHEN day_of_week(s.start_at) <= 5 AND hour(s.start_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       count(*) * 5 AS booked_minutes,
       count(*) AS booked_slots,
       count(s.people) AS observed_slots,
       max(s.people) AS peak_people,
       avg(s.people) AS avg_people,
       max(c.capacity) AS capacity,
       100.0 * max(s.people) / nullif(max(c.capacity), 0) AS peak_pct_capacity,
       100.0 * avg(s.people) / nullif(max(c.capacity), 0) AS avg_pct_capacity,
       CASE WHEN count(s.people) < 0.5 * count(*) THEN 'No sensor data'
            WHEN max(s.people) >= 1 THEN 'Used' ELSE 'Empty' END AS outcome
FROM slots s
JOIN cap c ON c.room_id = s.room_id
GROUP BY s.booking_type, s.purpose, s.booking_id, s.room_id, s.module, s.module_group,
         s.faculty, s.school, s.start_at, s.end_at, s.start_time
