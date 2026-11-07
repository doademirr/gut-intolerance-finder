-- 04_clean.sql
-- Clean layer: turn the raw uploads into food entries with grams and a best-available time,
-- symptom episodes, and a coverage label per day (D6, D7, D15, D16).
-- Expects (created by gut_finder/engine.py, all text columns):
--   raw_food_log(Day, Time, Group, "Food Name", Amount, Category)   Cronometer "Food & Recipe Entries"
--   raw_symptoms(date, time, symptom, severity)                     one row per symptom onset
--   raw_exclude_days(date, reason)                                  optional; may be empty
--   run_settings(symptoms_from, symptoms_to)                        one row; NULL = use the food log's range

-- 1. Food entries: grams and best-available time ------------------------------------------------
CREATE OR REPLACE TABLE food_entries AS
WITH parsed AS (
    SELECT
        row_number() OVER (ORDER BY "Day", "Time", "Group", "Food Name", Amount) AS entry_id,
        CAST("Day" AS DATE)                                            AS day,
        try_strptime(upper(trim("Time")), '%I:%M %p')                  AS logged_dt,  -- "2:21 PM"
        coalesce(nullif(trim("Group"), ''), '(no meal)')               AS meal,
        trim("Food Name")                                              AS food_name,
        try_cast(regexp_extract(trim(Amount), '^([0-9]*\.?[0-9]+)\s*(.*)$', 1) AS DOUBLE) AS qty,
        lower(trim(regexp_extract(trim(Amount), '^([0-9]*\.?[0-9]+)\s*(.*)$', 2)))       AS unit
    FROM raw_food_log
),
with_minutes AS (
    SELECT *,
        CASE WHEN logged_dt IS NOT NULL
             THEN hour(logged_dt) * 60 + minute(logged_dt) END AS logged_min,
        -- Weights convert exactly. Volumes and servings depend on the food, so 05 turns them into grams
        -- after matching (D21); ml keeps the volume for that. A plain "cup" has no stated size.
        CASE unit
            WHEN 'g'    THEN qty
            WHEN '100g' THEN qty * 100
            WHEN 'kg'   THEN qty * 1000
            WHEN 'oz'   THEN qty * 28.349523125
        END AS grams,
        CASE
            WHEN unit = 'ml'                 THEN qty
            WHEN unit IN ('l', 'litre', 'liter') THEN qty * 1000
            WHEN unit = 'fl oz'              THEN qty * 29.5735295625
            WHEN regexp_full_match(unit, 'x [0-9.]+ ?ml')
                 THEN qty * try_cast(regexp_extract(unit, '[0-9.]+') AS DOUBLE)
            WHEN regexp_full_match(unit, 'x [0-9.]+ ?fl oz')
                 THEN qty * try_cast(regexp_extract(unit, '[0-9.]+') AS DOUBLE) * 29.5735295625
            WHEN regexp_full_match(unit, '[a-z ]+\([0-9.]+ ?ml\)')       -- "cup (250ml)"
                 THEN qty * try_cast(regexp_extract(unit, '[0-9.]+') AS DOUBLE)
        END AS ml
    FROM parsed
),
same_meal AS (   -- tier 2: other foods in the same meal on the same day
    SELECT day, meal, round(median(logged_min)) AS m
    FROM with_minutes WHERE logged_min IS NOT NULL AND meal <> '(no meal)'
    GROUP BY day, meal
),
usual AS (       -- tier 3: this user's usual time for that meal
    SELECT meal, round(median(logged_min)) AS m
    FROM with_minutes WHERE logged_min IS NOT NULL AND meal <> '(no meal)'
    GROUP BY meal
)
SELECT
    e.entry_id, e.day, e.meal, e.food_name, e.qty, e.unit, e.grams, e.ml,
    CASE
        WHEN e.logged_min IS NOT NULL THEN 'logged'
        WHEN s.m IS NOT NULL          THEN 'same_meal'
        WHEN u.m IS NOT NULL          THEN 'usual_time'
        ELSE 'day_only'
    END AS time_source,
    CASE   -- day_only entries keep their day but get no time, so they never enter scoring
        WHEN coalesce(e.logged_min, s.m, u.m) IS NOT NULL
        THEN e.day + to_minutes(CAST(coalesce(e.logged_min, s.m, u.m) AS BIGINT))
    END AS ts
FROM with_minutes e
LEFT JOIN same_meal s ON s.day = e.day AND s.meal = e.meal
LEFT JOIN usual u     ON u.meal = e.meal;

-- 2. Symptom episodes: onsets less than episode_merge hours apart are one episode (G17) ---------
CREATE OR REPLACE TABLE symptom_onsets AS
SELECT
    CAST(date AS DATE) + CAST(strptime(trim(time), '%H:%M') AS TIME) AS onset_ts,
    lower(trim(symptom)) AS symptom,
    try_cast(severity AS INTEGER) AS severity
FROM raw_symptoms;

CREATE OR REPLACE TABLE symptom_episodes AS
WITH ordered AS (
    SELECT *,
        CASE WHEN onset_ts - lag(onset_ts) OVER (ORDER BY onset_ts)
                  < to_minutes(CAST((SELECT value FROM global_rules WHERE name = 'episode_merge') * 60 AS BIGINT))
             THEN 0 ELSE 1 END AS starts_new
    FROM symptom_onsets
),
numbered AS (
    SELECT *, sum(starts_new) OVER (ORDER BY onset_ts ROWS UNBOUNDED PRECEDING) AS episode_id
    FROM ordered
)
SELECT episode_id, min(onset_ts) AS onset_ts, count(*) AS rows_merged,
       string_agg(DISTINCT symptom, ', ' ORDER BY symptom) AS symptoms,
       list({'symptom': symptom, 'onset': onset_ts}) AS parts     -- a card may only count some of these (sql/06)
FROM numbered
GROUP BY episode_id;

-- 3. Coverage per day (D7, D16, G11) ----------------------------------------------------------
-- A day is usable if any food was logged, it's inside the declared symptom-logging period, and the
-- user didn't exclude it (illness, travel). Within a logged day, a meal that isn't logged counts as
-- "ate nothing": the tool can't assume food on the user's behalf. Days with no
-- food logged at all are left out.
CREATE OR REPLACE TABLE day_coverage AS
WITH bounds AS (
    SELECT min(day) AS first_day, max(day) AS last_day FROM food_entries
),
period AS (
    SELECT coalesce(CAST(r.symptoms_from AS DATE), b.first_day) AS sym_from,
           coalesce(CAST(r.symptoms_to   AS DATE), b.last_day)  AS sym_to
    FROM bounds b, run_settings r
),
days AS (
    SELECT CAST(d AS DATE) AS day
    FROM bounds, range(first_day, last_day + INTERVAL 1 DAY, INTERVAL 1 DAY) t(d)
)
SELECT
    d.day,
    d.day IN (SELECT day FROM food_entries)                                     AS food_logged,
    d.day BETWEEN p.sym_from AND p.sym_to                                       AS symptoms_logged,
    d.day IN (SELECT CAST(date AS DATE) FROM raw_exclude_days)                  AS excluded,
    (d.day IN (SELECT day FROM food_entries)
     AND d.day BETWEEN p.sym_from AND p.sym_to
     AND d.day NOT IN (SELECT CAST(date AS DATE) FROM raw_exclude_days))        AS usable
FROM days d, period p;
