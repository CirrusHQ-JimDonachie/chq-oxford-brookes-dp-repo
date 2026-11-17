-- Wi-Fi devices against door-counted people: one row per door-counter room per UK day
-- per hour, for rooms with an access point in the room list, over the days the Wi-Fi
-- export covers (see span).
--
-- The unit is the five-minute Smart-Vis reading (off-grid corrections left out). Its
-- devices are the distinct Wi-Fi clients connected to any of the room's access points
-- in that slot: each session fills the slots from its start for its duration, as in
-- the trial Wi-Fi dataset. obu-iot, printers, the test unit and the two access points
-- outside NHHB are left out, and only the newest Wi-Fi export is read, because a new
-- export repeats the earlier one's dates. The curated table keeps the client name,
-- not the MAC address, so a device whose name changes within a slot counts twice.
--
--   average people / devices = sum(people_sum or devices_sum) / sum(slots)
--   agreement % = sum(agree_slots) / sum(slots): both show someone, or both show no one
--
-- Wi-Fi is trial data until Oxford Brookes' DPO signs off the pseudonymised export.
-- {curated} is replaced by occupancy.py.
WITH rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), ap AS (
  SELECT DISTINCT access_point_id, 'NHHB.' || upper(room_code) AS room_id
  FROM rooms_latest WHERE access_point_id IS NOT NULL
), sv AS (
  SELECT upper(room_id) AS room_id,
         date_parse(local_date || ' ' || local_time, '%Y-%m-%d %H:%i') AS slot_at,
         max(people_count) AS people
  FROM {curated}."smartvis_occupancy"
  WHERE people_count IS NOT NULL AND minute(reading_at) % 5 = 0
  GROUP BY 1, 2
), room_ap AS (
  SELECT * FROM ap WHERE room_id IN (SELECT DISTINCT room_id FROM sv)
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
  -- The export's real coverage: days with at least 1,000 sessions after the exclusions
  -- above. The 2026-11-16 export holds a handful of stray sessions from 11 to 19
  -- September, which would read as rooms with people and no Wi-Fi, so with it the
  -- comparison runs from 21 September to 6 November.
  SELECT min(d) AS d0, max(d) AS d1
  FROM (SELECT CAST(started AS date) AS d, count(*) AS n FROM w GROUP BY 1)
  WHERE n >= 1000
), wslots AS (
  SELECT ra.room_id, date_add('minute', u.k, w.started) AS slot_at,
         count(DISTINCT w.client_id) AS devices
  FROM w
  JOIN room_ap ra ON ra.access_point_id = w.access_point_id
  CROSS JOIN UNNEST(sequence(0, w.minutes - 5, 5)) AS u(k)
  WHERE w.minutes >= 5
  GROUP BY 1, 2
)
SELECT sv.room_id,
       CASE split_part(sv.room_id, '.', 2) WHEN 'G' THEN 'Ground floor'
            ELSE 'Floor ' || split_part(sv.room_id, '.', 2) END AS floor,
       CAST(sv.slot_at AS date) AS local_date,
       format('%d %s', day_of_week(sv.slot_at), date_format(sv.slot_at, '%a')) AS weekday,
       hour(sv.slot_at) AS hour_of_day,
       CASE WHEN day_of_week(sv.slot_at) <= 5 AND hour(sv.slot_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       count(*) AS slots,
       sum(sv.people) AS people_sum,
       sum(coalesce(ws.devices, 0)) AS devices_sum,
       count_if(sv.people >= 1) AS people_in_use_slots,
       count_if(coalesce(ws.devices, 0) >= 1) AS wifi_in_use_slots,
       count_if((sv.people >= 1) = (coalesce(ws.devices, 0) >= 1)) AS agree_slots
FROM sv
CROSS JOIN span
LEFT JOIN wslots ws ON ws.room_id = sv.room_id AND ws.slot_at = sv.slot_at
WHERE sv.room_id IN (SELECT room_id FROM room_ap)
  AND CAST(sv.slot_at AS date) BETWEEN span.d0 AND span.d1
GROUP BY 1, 2, 3, 4, 5, 6
