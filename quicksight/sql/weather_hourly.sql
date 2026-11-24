-- Hourly weather at Headington Campus (Open-Meteo, supplied by Oxford Brookes): one row
-- per site per hour, in UK local time so it lines up with the occupancy and booking
-- datasets. Feeds the Q Topic. {curated} is replaced by occupancy.py.
--
-- The curated table only appends, so an hour delivered in two files (1 to 2 Nov 2026
-- arrived twice) appears twice there; here each site-hour keeps only its most recently
-- ingested reading.
--
-- raining is Yes for any measurable precipitation in the hour (Open-Meteo reports it to
-- 0.1 mm). Weather is site-wide, so this dataset has no room and no row-level security.
WITH latest AS (
  SELECT w.*,
         row_number() OVER (PARTITION BY location_id, reading_at ORDER BY ingested_at DESC) AS rn
  FROM {curated}."weather_hourly" w
), w AS (
  SELECT latest.*,
         CAST(reading_at AT TIME ZONE 'Europe/London' AS timestamp(6)) AS local_at
  FROM latest
  WHERE rn = 1
)
SELECT replace(location_id, '_', ' ') AS location,
       local_at,
       CAST(local_at AS date) AS local_date,
       format('%d %s', day_of_week(local_at), date_format(local_at, '%a')) AS weekday,
       hour(local_at) AS hour_of_day,
       CASE WHEN day_of_week(local_at) <= 5 AND hour(local_at) BETWEEN 8 AND 17
            THEN 'Yes' ELSE 'No' END AS core_hours,
       CASE WHEN precipitation_mm > 0 THEN 'Yes' ELSE 'No' END AS raining,
       air_temperature_c,
       feels_like_temperature_c,
       humidity_pct,
       precipitation_mm,
       wind_speed_kmh,
       solar_radiation_wm2,
       cloud_cover_pct
FROM w
