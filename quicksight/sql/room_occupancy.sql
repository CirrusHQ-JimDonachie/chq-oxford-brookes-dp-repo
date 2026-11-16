-- One row per SMARTviz reading, with the room's Archibus details. Feeds the Room
-- Occupancy dashboard and the Q Topic. {curated} is replaced by build.sh with the
-- quoted catalog and namespace, e.g. "s3tablescatalog/building-data-dev-curated"."building_data".
--
-- Times: local_at is the source's own UK wall-clock time (as SMARTviz exported it);
-- reading_at_utc is the same instant in UTC. Weekday, hour and core hours follow
-- local time, because that is the time people book and use rooms by.
--
-- Counts: SMARTviz resets every room to zero at 00:00 and inserts zero readings off
-- the five-minute grid to correct drift. people_count_scheduled and utilisation_pct
-- leave the off-grid corrections out, so averages are over the regular readings;
-- reading_kind labels each row so they can be shown or counted.
WITH rooms_latest AS (
  SELECT * FROM {curated}."rooms"
  WHERE source_file = (SELECT max_by(source_file, ingested_at) FROM {curated}."rooms")
), room AS (
  -- Archibus splits some rooms into -a/-b halves; SMARTviz reports the whole room.
  SELECT room_code,
         max(floor_code) AS floor_code,
         array_join(array_sort(array_agg(DISTINCT room_type)), ' / ') AS room_type,
         array_join(array_sort(array_agg(DISTINCT department_name)), ' / ') AS department,
         array_join(array_sort(array_agg(room_id)), ' / ') AS archibus_rooms
  FROM rooms_latest
  GROUP BY room_code
), o AS (
  SELECT o.*,
         date_parse(o.local_date || ' ' || o.local_time, '%Y-%m-%d %H:%i') AS local_at,
         CAST(o.reading_at AT TIME ZONE 'UTC' AS timestamp(6)) AS reading_at_utc,
         o.people_count IS NOT NULL AND minute(o.reading_at) % 5 = 0 AS on_grid_count
  FROM {curated}."smartvis_occupancy" o
), labelled AS (
  SELECT o.*,
         day_of_week(o.local_at) <= 5 AND hour(o.local_at) BETWEEN 8 AND 17 AS is_core
  FROM o
)
SELECT o.room_id,
       o.room_code,
       o.space_name,
       CASE WHEN r.floor_code IS NULL THEN 'Not in room list'
            WHEN r.floor_code = 'G' THEN 'Ground floor'
            ELSE 'Floor ' || r.floor_code END AS floor,
       coalesce(r.room_type, 'Unknown') AS room_type,
       coalesce(r.department, 'Unknown') AS department,
       r.archibus_rooms,
       CASE WHEN o.people_count IS NOT NULL THEN 'Door counter' ELSE 'Presence sensor' END AS sensor_type,
       o.local_at,
       o.reading_at_utc,
       CAST(o.local_date AS date) AS local_date,
       format('%d %s', day_of_week(o.local_at), date_format(o.local_at, '%a')) AS weekday,
       hour(o.local_at) AS hour_of_day,
       CASE WHEN o.is_core THEN 'Yes' ELSE 'No' END AS core_hours,
       o.people_count,
       CASE WHEN o.on_grid_count THEN o.people_count END AS people_count_scheduled,
       CASE WHEN o.on_grid_count AND o.is_core THEN o.people_count END AS people_count_core,
       o.presence,
       o.capacity,
       o.area_m2,
       CASE WHEN o.on_grid_count AND o.capacity > 0 THEN 100.0 * o.people_count / o.capacity END AS utilisation_pct,
       CASE WHEN o.on_grid_count AND o.is_core AND o.capacity > 0 THEN 100.0 * o.people_count / o.capacity END AS utilisation_pct_core,
       CASE WHEN o.people_count IS NULL THEN 'Presence event'
            WHEN NOT o.on_grid_count THEN 'Off-grid correction'
            WHEN o.local_time = '00:00' THEN 'Midnight reset'
            ELSE 'Scheduled' END AS reading_kind,
       o.source_file
FROM labelled o
LEFT JOIN room r ON r.room_code = o.room_code
