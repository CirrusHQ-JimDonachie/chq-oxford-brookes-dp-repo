-- Do the sources agree? One row per timetabled door-counter room per UK day per hour,
-- from the room's first validated reading to the last Smart-Vis day, setting the
-- TimeEdit booking, the Smart-Vis door count and the Wi-Fi devices side by side and
-- sorting the hour into one situation (ECS strategic questions 1.1 and 1.2).
--
-- Every hour of the grid is present, so an hour without door-counter readings shows as
-- missing rather than dropping out. Within an hour:
--   booked:  at least half of its five-minute slots inside a booking (6 of 12)
--   in use:  the door counter counts anyone in a booked slot (if booked) or in an
--            unbooked slot (if not)
--   Wi-Fi active: at least 5 devices on average on the room's access points. 95% of
--            night hours (00:00-06:00) with nobody counted average under 5 (4.8 in the
--            Sep-Nov sample), so 5 separates real activity from background devices.
-- Situations, in order of precedence:
--   Door counter readings missing          fewer than 6 of 12 readings received
--   Booked and in use                      booked, in use
--   Booked, Wi-Fi active, door count zero  booked, nobody counted, Wi-Fi active
--   Booked, nobody detected                booked, nobody counted, Wi-Fi quiet or absent
--   Not booked, people counted             not booked, in use
--   Door count zero, Wi-Fi active          not booked, nobody counted, Wi-Fi active
--   Not booked and empty                   not booked, nobody counted, Wi-Fi quiet or absent
-- Provisional readings (see room_occupancy.sql) are left out, and a room's grid starts
-- on its first validated reading. Wi-Fi is trial data covering 21 Sep to 6 Nov; outside
-- that, or for rooms with no access point in the room list, wifi_devices_avg is NULL.
-- {curated}, {provisional}, {teaching} and {non_teaching} are replaced by occupancy.py.
WITH sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people,
         max(capacity) AS capacity
  FROM {curated}."smartvis_occupancy" s
  WHERE people_count IS NOT NULL AND minute(reading_at) % 5 = 0
    AND NOT EXISTS (SELECT 1 FROM {provisional}
                    WHERE p.room_id = upper(s.room_id) AND CAST(s.local_date AS date) < p.valid_from)
  GROUP BY 1, 2
), bookings AS (
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
), rm AS (
  SELECT room_id, CAST(min(slot_at) AS date) AS first_day, max(capacity) AS capacity
  FROM sv WHERE room_id IN (SELECT room_id FROM b) GROUP BY 1
), last_day AS (
  SELECT CAST(max(slot_at) AS date) AS d1 FROM sv
), grid AS (
  SELECT rm.room_id, u.slot_at
  FROM rm
  CROSS JOIN last_day
  CROSS JOIN UNNEST(sequence(CAST(rm.first_day AS timestamp), CAST(last_day.d1 AS timestamp) + INTERVAL '1435' MINUTE,
                             INTERVAL '5' MINUTE)) AS u(slot_at)
), rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), room AS (
  SELECT 'NHHB.' || upper(room_code) AS room_id,
         max(floor_code) AS floor_code,
         array_join(array_sort(array_agg(DISTINCT room_type)), ' / ') AS room_type
  FROM rooms_latest GROUP BY 1
), ap AS (
  SELECT DISTINCT access_point_id, 'NHHB.' || upper(room_code) AS room_id
  FROM rooms_latest WHERE access_point_id IS NOT NULL
), w AS (
  SELECT client_id, access_point_id,
         CAST(connected_since AT TIME ZONE 'UTC' AS timestamp(6)) AS started,
         coalesce(try_cast(regexp_extract(duration, '(\d+)w', 1) AS integer), 0) * 10080
       + coalesce(try_cast(regexp_extract(duration, '(\d+)d', 1) AS integer), 0) * 1440
       + coalesce(try_cast(regexp_extract(duration, '(\d+)h', 1) AS integer), 0) * 60
       + coalesce(try_cast(regexp_extract(duration, '(\d+)m', 1) AS integer), 0) AS minutes
  FROM {curated}."trial_wifi_client_sessions"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."trial_wifi_client_sessions")
    AND access_point_id NOT IN ('GIP-TST-002', 'HDH-NHB-WAP-060', 'HDH-NHB-WAP-061')
    AND coalesce(ssid, '') <> 'obu-iot'
    AND coalesce(device_type, '') NOT LIKE '%Printer%'
), span AS (
  SELECT min(d) AS d0, max(d) AS d1
  FROM (SELECT CAST(started AS date) AS d, count(*) AS n FROM w GROUP BY 1)
  WHERE n >= 1000
), wslots AS (
  SELECT ap.room_id, date_add('minute', u.k, w.started) AS slot_at,
         count(DISTINCT w.client_id) AS devices
  FROM w
  JOIN ap ON ap.access_point_id = w.access_point_id
  CROSS JOIN UNNEST(sequence(0, w.minutes - 5, 5)) AS u(k)
  WHERE w.minutes >= 5
  GROUP BY 1, 2
), hourly AS (
  SELECT g.room_id,
         date_trunc('hour', g.slot_at) AS hour_at,
         count(sv.people) AS door_slots,
         count_if(bk.slot_at IS NOT NULL) AS booked_slots,
         count_if(bk.slot_at IS NOT NULL AND sv.people >= 1) AS booked_used_slots,
         count_if(bk.slot_at IS NULL AND sv.people >= 1) AS unbooked_used_slots,
         max(sv.people) AS door_peak,
         CASE WHEN g.room_id IN (SELECT room_id FROM ap)
                   AND CAST(g.slot_at AS date) BETWEEN span.d0 AND span.d1
              THEN avg(coalesce(ws.devices, 0)) END AS wifi_devices_avg,
         CASE WHEN g.room_id NOT IN (SELECT room_id FROM ap) THEN 'No access point in room list'
              WHEN CAST(g.slot_at AS date) BETWEEN span.d0 AND span.d1 THEN 'Covered'
              ELSE 'Outside Wi-Fi export' END AS wifi_status
  FROM grid g
  CROSS JOIN span
  LEFT JOIN sv ON sv.room_id = g.room_id AND sv.slot_at = g.slot_at
  LEFT JOIN booked bk ON bk.room_id = g.room_id AND bk.slot_at = g.slot_at
  LEFT JOIN wslots ws ON ws.room_id = g.room_id AND ws.slot_at = g.slot_at
  GROUP BY g.room_id, date_trunc('hour', g.slot_at), CAST(g.slot_at AS date), span.d0, span.d1
), classified AS (
  SELECT h.*,
         CASE WHEN h.door_slots < 6 THEN 'Door counter readings missing'
              WHEN h.booked_slots >= 6 AND h.booked_used_slots >= 1 THEN 'Booked and in use'
              WHEN h.booked_slots >= 6 AND h.wifi_devices_avg >= 5 THEN 'Booked, Wi-Fi active, door count zero'
              WHEN h.booked_slots >= 6 THEN 'Booked, nobody detected'
              WHEN h.unbooked_used_slots >= 1 THEN 'Not booked, people counted'
              WHEN h.wifi_devices_avg >= 5 THEN 'Door count zero, Wi-Fi active'
              ELSE 'Not booked and empty' END AS situation
  FROM hourly h
)
SELECT c.room_id,
       CASE WHEN r.floor_code = 'G' THEN 'Ground floor' ELSE 'Floor ' || r.floor_code END AS floor,
       coalesce(r.room_type, 'Unknown') AS room_type,
       rm.capacity,
       CAST(c.hour_at AS date) AS local_date,
       c.hour_at,
       format('%d %s', day_of_week(c.hour_at), date_format(c.hour_at, '%a')) AS weekday,
       hour(c.hour_at) AS hour_of_day,
       CASE WHEN day_of_week(c.hour_at) <= 5 AND hour(c.hour_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       c.door_slots,
       c.booked_slots,
       c.door_peak,
       c.wifi_devices_avg,
       c.wifi_status,
       c.situation,
       CASE WHEN c.situation IN ('Booked and in use', 'Not booked and empty') THEN 'Sources agree'
            WHEN c.situation = 'Door counter readings missing' THEN 'Missing data'
            ELSE 'Sources disagree' END AS agreement,
       CASE c.situation
         WHEN 'Door counter readings missing' THEN 'Device outage or transmission gap: check the coverage sheet'
         WHEN 'Booked, Wi-Fi active, door count zero' THEN 'Suspect zero: door counter fault, or the access point serves other space'
         WHEN 'Booked, nobody detected' THEN 'Potentially unused booking, or a sensor problem'
         WHEN 'Not booked, people counted' THEN 'Unscheduled or informal use, or a booking missing from TimeEdit'
         WHEN 'Door count zero, Wi-Fi active' THEN 'Corridor or neighbouring devices, or a door counter missing people'
         ELSE '' END AS possible_explanation
FROM classified c
JOIN rm ON rm.room_id = c.room_id
LEFT JOIN room r ON r.room_id = c.room_id
