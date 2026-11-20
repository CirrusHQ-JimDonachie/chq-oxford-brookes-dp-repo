-- Wi-Fi devices by room: one row per room with an access point in the room list, per UK
-- day per hour, over the days the Wi-Fi export covers (see span). Unlike wifi_vs_door.sql
-- it covers every mapped room, not only the door-counter rooms, so Q can answer
-- questions about the cafe, workshops and labs from Wi-Fi alone.
--
-- Every room gets all twelve five-minute slots of every hour, so an hour with no devices
-- counts as zero rather than dropping out. A slot's devices are the distinct Wi-Fi clients
-- connected to any of the room's access points, built as in wifi_vs_door.sql, with the
-- same exclusions (obu-iot, printers, the test unit and the two access points outside
-- NHHB). Access points also pick up corridors and neighbouring rooms, so devices are an
-- indication of activity, not a head count.
--
--   average devices = sum(devices_sum) / sum(slots)
--   booked time with Wi-Fi activity % = sum(booked_wifi_slots) / sum(booked_slots)
--
-- Wi-Fi is trial data until Oxford Brookes' DPO signs off the pseudonymised export.
-- {curated}, {teaching} and {non_teaching} are replaced by occupancy.py.
WITH rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), ap AS (
  SELECT DISTINCT access_point_id, 'NHHB.' || upper(room_code) AS room_id
  FROM rooms_latest WHERE access_point_id IS NOT NULL
), room AS (
  -- Archibus splits some rooms into -a/-b halves, each with its own access point;
  -- the room is the whole room code.
  SELECT 'NHHB.' || upper(room_code) AS room_id,
         max(floor_code) AS floor_code,
         array_join(array_sort(array_agg(DISTINCT room_type)), ' / ') AS room_type,
         array_join(array_sort(array_agg(DISTINCT
           CASE WHEN room_use LIKE 'T%' THEN 'Teaching' WHEN room_use LIKE 'S%' THEN 'Support'
                WHEN room_use LIKE 'R%' THEN 'Research' WHEN room_use LIKE 'O%' THEN 'Other'
                ELSE 'Unknown' END)), ' / ') AS room_use
  FROM rooms_latest
  WHERE 'NHHB.' || upper(room_code) IN (SELECT room_id FROM ap)
  GROUP BY 1
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
  -- Days with at least 1,000 sessions after the exclusions: 21 Sep to 6 Nov in the
  -- 2026-11-16 export (see wifi_vs_door.sql).
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
), grid AS (
  SELECT r.room_id, u.slot_at
  FROM room r
  CROSS JOIN span
  CROSS JOIN UNNEST(sequence(CAST(span.d0 AS timestamp), CAST(span.d1 AS timestamp) + INTERVAL '1435' MINUTE,
                             INTERVAL '5' MINUTE)) AS u(slot_at)
)
SELECT g.room_id,
       CASE WHEN r.floor_code = 'G' THEN 'Ground floor' ELSE 'Floor ' || r.floor_code END AS floor,
       r.room_type,
       r.room_use,
       CASE WHEN g.room_id IN (SELECT room_id FROM b) THEN 'Yes' ELSE 'No' END AS timetabled,
       CAST(g.slot_at AS date) AS local_date,
       format('%d %s', day_of_week(g.slot_at), date_format(g.slot_at, '%a')) AS weekday,
       hour(g.slot_at) AS hour_of_day,
       CASE WHEN day_of_week(g.slot_at) <= 5 AND hour(g.slot_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       count(*) AS slots,
       sum(coalesce(ws.devices, 0)) AS devices_sum,
       max(coalesce(ws.devices, 0)) AS devices_peak,
       count_if(coalesce(ws.devices, 0) >= 1) AS wifi_in_use_slots,
       count_if(bk.slot_at IS NOT NULL) AS booked_slots,
       count_if(bk.slot_at IS NOT NULL AND coalesce(ws.devices, 0) >= 1) AS booked_wifi_slots
FROM grid g
JOIN room r ON r.room_id = g.room_id
LEFT JOIN wslots ws ON ws.room_id = g.room_id AND ws.slot_at = g.slot_at
LEFT JOIN booked bk ON bk.room_id = g.room_id AND bk.slot_at = g.slot_at
GROUP BY 1, 2, 3, 4, 5, 6, 7, 8, 9
