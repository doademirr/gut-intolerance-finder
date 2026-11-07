-- 06_score.sql
-- Scoring (§1.1, D15–D16). Time is checked in ticks (D15: 15 minutes).
-- For each component and each tick T:
--   dose(T)  = grams eaten at times τ with τ + window start <= T < τ + window end (doses expire)
--   state    = X  excluded: the tick or its look-back touches an unusable day or the time before the log
--              E  the known grams alone reach the card's dose. Unknown food in the window can only add more,
--                 so this holds beside unknown food too (the "at least" rule, every card; D19)
--              U  below the dose on known grams, but the dose there is unknown (a food not recognised,
--                 a value missing, an amount not in grams, no usable time)
--              B  below the dose (background). This holds beside unknown food too if it can't reach the dose
--                 even if it were nothing but that component: known grams + the weight of every unknown food
--                 in the window stay under the dose ("at most" rule, D23)
-- Window edges are rounded inwards for doses (a symptom is never credited outside the window) and
-- outwards for unknowns (unknown time is never counted as known).
-- An exposure is one unbroken run of E with B or U on both sides. A run touching X is left out: X can cut
-- a window short, so part of the stretch was never observed. U can't, because E outranks U.
-- A symptom episode starting inside an exposure → evidence for; none → counter-example.
-- Background: B runs are cut into back-to-back blocks one window long; the share of blocks with an
-- episode onset is the user's background rate.

-- 0. Settings and the components that can be scored -----------------------------------------
CREATE OR REPLACE TABLE settings AS
SELECT
    (SELECT value FROM global_rules WHERE name = 'tick')                   AS tick_min,
    (SELECT value FROM global_rules WHERE name = 'trigger_rate')           AS t,
    (SELECT value FROM global_rules WHERE name = 'confidence_cap')         AS cap,
    (SELECT value FROM global_rules WHERE name = 'cap_weak')               AS cap_weak,
    (SELECT value FROM global_rules WHERE name = 'min_clear_hours')        AS min_clear_hours,
    (SELECT min(day) FROM day_coverage)                                    AS first_day,
    (SELECT count(*) FROM day_coverage)                                    AS n_days;

-- Only cards with every number they need, counted in grams within a window (v1).
CREATE OR REPLACE TABLE scored_components AS
SELECT c.component, c.display_name, c.flag_mode, c.evidence_strength, c.status,
       c.dose_threshold, c.start_probability,
       c.window_start_h * 60 AS s_min,
       c.window_end_h * 60   AS e_min,
       CAST(round((c.window_end_h - c.window_start_h) * 60 / s.tick_min) AS INTEGER) AS block_len
FROM component_rules c, settings s
WHERE c.window_start_h IS NOT NULL AND c.window_end_h IS NOT NULL
  AND c.dose_threshold IS NOT NULL AND c.start_probability IS NOT NULL
  AND c.flag_mode IS NOT NULL AND c.evidence_strength IS NOT NULL   -- unfinished card: never silently scored
  AND c.dose_unit = 'g' AND c.dose_scope = 'window';

-- 1. Days: how many usable days in a row end on each day ---------------------------------------
CREATE OR REPLACE TABLE day_streak AS
WITH d AS (
    SELECT dc.day, dc.usable, date_diff('day', s.first_day, dc.day) AS day_no
    FROM day_coverage dc, settings s
)
SELECT day_no, usable,
       CASE WHEN usable
            THEN day_no - coalesce(max(CASE WHEN NOT usable THEN day_no END)
                                   OVER (ORDER BY day_no ROWS UNBOUNDED PRECEDING), -1)
            ELSE 0 END AS streak
FROM d;

-- 2. Every timed entry × component: known grams, or unknown ---------------------------------------
CREATE OR REPLACE TABLE entry_component AS
SELECT e.entry_id, e.day, e.ts, e.time_source, c.component,
       ci.grams,                                             -- for an "at least" total: the known part
       ci.entry_id IS NULL OR ci.grams IS NULL OR ci.at_least AS unknown,   -- no value, an amount that couldn't
                                                                             -- be read, or only partly known
       date_diff('minute', CAST(s.first_day AS TIMESTAMP), e.ts) AS m
FROM food_entries e
CROSS JOIN scored_components c
CROSS JOIN settings s
LEFT JOIN component_intake ci ON ci.entry_id = e.entry_id AND ci.component = c.component;

-- Known doses, on the tick grid, rounded inwards. The known part of an "at least" total counts here too.
CREATE OR REPLACE TABLE intake_ticks AS
SELECT x.component, x.grams, x.time_source,
       CAST(ceil((x.m + c.s_min) / s.tick_min) AS BIGINT)  AS from_k,
       CAST(floor((x.m + c.e_min) / s.tick_min) AS BIGINT) AS to_k     -- exclusive
FROM entry_component x JOIN scored_components c USING (component), settings s
WHERE x.ts IS NOT NULL AND x.grams > 0;

-- Unknown doses, rounded outwards. An entry with no usable time makes its whole day unknown.
-- at_most: the most this entry could add, which is what the food weighs. NULL when that can't be said.
CREATE OR REPLACE TABLE unknown_spans AS
SELECT x.component,
       CAST(floor((x.m + c.s_min) / s.tick_min) AS BIGINT) AS from_k,
       CAST(ceil((x.m + c.e_min) / s.tick_min) AS BIGINT)  AS to_k,
       a.grams                                             AS at_most
FROM entry_component x JOIN scored_components c USING (component) JOIN entry_amounts a USING (entry_id), settings s
WHERE x.ts IS NOT NULL AND x.unknown
UNION ALL
SELECT x.component,
       CAST(floor((date_diff('day', s.first_day, x.day) * 1440 + c.s_min) / s.tick_min) AS BIGINT),
       CAST(ceil(((date_diff('day', s.first_day, x.day) + 1) * 1440 + c.e_min) / s.tick_min) AS BIGINT),
       NULL
FROM entry_component x JOIN scored_components c USING (component), settings s
WHERE x.ts IS NULL AND (x.unknown OR x.grams > 0);

-- One row per card and episode. A card with a list of symptoms that count only sees episodes with one of them, and
-- the episode starts for that card at the first symptom that counts, so a symptom that doesn't count can't pull
-- it into the window.
CREATE OR REPLACE TABLE episode_ticks AS
WITH counting AS (SELECT component, list(symptom) AS symptoms FROM card_symptom_rules GROUP BY component),
parts AS (SELECT episode_id, unnest(parts) AS p FROM symptom_episodes)
SELECT c.component, e.episode_id,
       CAST(floor(date_diff('minute', CAST(s.first_day AS TIMESTAMP), min(e.p.onset)) / s.tick_min) AS BIGINT) AS k
FROM parts e CROSS JOIN settings s CROSS JOIN scored_components c
LEFT JOIN counting i ON i.component = c.component
WHERE i.symptoms IS NULL OR list_contains(i.symptoms, e.p.symptom)
GROUP BY c.component, e.episode_id, s.first_day, s.tick_min;

-- 3. State of every tick for every component -------------------------------------------------------
CREATE OR REPLACE TABLE tick_state AS
WITH ticks AS (
    SELECT t.k, CAST(floor(t.k * s.tick_min / 1440) AS BIGINT) AS day_no
    FROM settings s, range(0, CAST(s.n_days * 1440 / s.tick_min AS BIGINT)) t(k)
),
dose AS (
    SELECT t.k, t.day_no, c.component, c.dose_threshold, c.e_min,
           coalesce(sum(i.grams), 0) AS dose
    FROM ticks t
    CROSS JOIN scored_components c
    LEFT JOIN intake_ticks i
           ON i.component = c.component AND t.k >= i.from_k AND t.k < i.to_k
    GROUP BY ALL
),
unknown AS (
    SELECT u.component, t.k,
           CASE WHEN count(*) = count(u.at_most) THEN sum(u.at_most) END AS at_most   -- every weight is needed
    FROM unknown_spans u JOIN ticks t ON t.k >= u.from_k AND t.k < u.to_k
    GROUP BY 1, 2
)
SELECT d.component, d.k, d.day_no, d.dose, ds.usable AS day_usable,
       CASE
           WHEN floor((d.k * s.tick_min - d.e_min) / 1440) < 0 THEN 'X'                     -- look-back before the log
           WHEN ds.streak < d.day_no - floor((d.k * s.tick_min - d.e_min) / 1440) + 1 THEN 'X'  -- touches an unusable day
           WHEN d.dose >= d.dose_threshold - 1e-9 THEN 'E'                                   -- known grams reach it
           WHEN u.k IS NOT NULL
            AND coalesce(d.dose + u.at_most >= d.dose_threshold - 1e-9, true) THEN 'U'       -- could be under or over
           ELSE 'B'
       END AS state
FROM dose d
JOIN day_streak ds ON ds.day_no = d.day_no
LEFT JOIN unknown u ON u.component = d.component AND u.k = d.k
CROSS JOIN settings s;

-- 4. Runs of the same state (gaps and islands) -----------------------------------------------------
CREATE OR REPLACE TABLE runs AS
WITH grouped AS (
    SELECT component, k, state,
           k - row_number() OVER (PARTITION BY component, state ORDER BY k) AS grp
    FROM tick_state
),
r AS (
    SELECT component, state, min(k) AS start_k, max(k) AS end_k, count(*) AS n_ticks
    FROM grouped GROUP BY component, state, grp
)
SELECT *,
       lag(state)  OVER (PARTITION BY component ORDER BY start_k) AS prev_state,
       lead(state) OVER (PARTITION BY component ORDER BY start_k) AS next_state
FROM r;

-- 5. Exposures: fully observed E runs ----------------------------------------------------------------
CREATE OR REPLACE TABLE exposures AS
SELECT r.component, r.start_k, r.end_k, r.n_ticks,
       EXISTS (SELECT 1 FROM episode_ticks e
               WHERE e.component = r.component AND e.k BETWEEN r.start_k AND r.end_k) AS followed_by_symptom,
       EXISTS (SELECT 1 FROM intake_ticks i
               WHERE i.component = r.component AND i.time_source IN ('same_meal', 'usual_time')
                 AND i.from_k <= r.end_k AND i.to_k > r.start_k)                     AS uses_estimated_time
FROM runs r
WHERE r.state = 'E' AND r.prev_state IN ('B', 'U') AND r.next_state IN ('B', 'U');

-- 6. Background: B runs cut into whole blocks one window long -------------------------------------------
CREATE OR REPLACE TABLE background AS
WITH b_ticks AS (
    SELECT t.component, t.k, r.start_k, c.block_len
    FROM tick_state t
    JOIN runs r ON r.component = t.component AND r.state = 'B' AND t.k BETWEEN r.start_k AND r.end_k
    JOIN scored_components c ON c.component = t.component
),
blocks AS (
    SELECT component, start_k, floor((k - start_k) / block_len) AS block_no,
           min(k) AS from_k, max(k) AS to_k, count(*) AS n_ticks, any_value(block_len) AS block_len
    FROM b_ticks GROUP BY component, start_k, block_no
)
SELECT b.component,
       count(*)                                                                    AS n_blocks,
       count(*) FILTER (WHERE EXISTS (SELECT 1 FROM episode_ticks e
                                      WHERE e.component = b.component AND e.k BETWEEN b.from_k AND b.to_k))      AS k_blocks
FROM blocks b
WHERE b.n_ticks = b.block_len
GROUP BY b.component;

-- 7. Scores (log-odds points, §1.1 (each clue is worth more when symptoms are rare for that user) and §1.2) ------------------------------------------------------------
-- A stretch m windows long gives m chances: both the real-trigger rate and the background rate are
-- compounded over the same length, so a longer stretch never turns evidence backwards.
--   evidence for:      ln( (1 − (1 − t)^m) / (1 − (1 − b)^m) )
--   counter-example:   m · ln( (1 − t) / (1 − b) )
CREATE OR REPLACE TABLE component_results AS
WITH base AS (
    SELECT c.*, s.t, s.min_clear_hours,
           CASE c.flag_mode WHEN 'suspect' THEN s.cap ELSE s.cap_weak END AS cap,   -- G18 ceiling
           coalesce(bg.n_blocks, 0) AS n_blocks,
           coalesce(bg.n_blocks, 0) * c.block_len * s.tick_min / 60.0 AS clear_hours,   -- the same bar whatever the window
           coalesce(bg.k_blocks, 0) AS k_blocks,
           (coalesce(bg.k_blocks, 0) + 1.0) / (coalesce(bg.n_blocks, 0) + 2.0) AS b
    FROM scored_components c CROSS JOIN settings s
    LEFT JOIN background bg USING (component)
),
evidence AS (
    SELECT x.component,
           count(*)                                            AS n_exposures,
           count(*) FILTER (WHERE x.followed_by_symptom)       AS clues,
           count(*) FILTER (WHERE NOT x.followed_by_symptom)   AS counters,
           count(*) FILTER (WHERE x.uses_estimated_time)       AS estimated_time_exposures,
           sum(CASE WHEN x.followed_by_symptom
                    THEN ln((1 - power(1 - b.t, x.n_ticks / b.block_len))
                            / (1 - power(1 - b.b, x.n_ticks / b.block_len)))
                    ELSE (x.n_ticks / b.block_len) * ln((1 - b.t) / (1 - b.b)) END) AS points
    FROM exposures x JOIN base b USING (component)
    GROUP BY x.component
),
eligible_episodes AS (
    SELECT t.component, count(*) AS n_episodes
    FROM episode_ticks e JOIN tick_state t ON t.k = e.k AND t.component = e.component AND t.state IN ('E', 'B')
    GROUP BY t.component
),
checked AS (   -- share of usable logged time whose dose could be known
    SELECT component, avg(CASE WHEN state IN ('E', 'B') THEN 1.0 ELSE 0 END) AS checked_share
    FROM tick_state WHERE day_usable GROUP BY component
),
has_values AS (
    SELECT DISTINCT component FROM component_intake WHERE grams IS NOT NULL
    UNION SELECT DISTINCT component FROM food_components WHERE amount_per_100g IS NOT NULL
    UNION SELECT DISTINCT component FROM my_foods
)
SELECT
    b.component, b.display_name, b.flag_mode, b.evidence_strength, b.status, b.start_probability,
    coalesce(ev.n_exposures, 0) AS n_exposures,
    coalesce(ev.clues, 0)       AS clues,
    coalesce(ev.counters, 0)    AS counters,
    coalesce(ev.estimated_time_exposures, 0) AS estimated_time_exposures,
    coalesce(ee.n_episodes, 0)  AS eligible_episodes,
    coalesce(ck.checked_share, 0) AS checked_share,
    b.n_blocks, b.clear_hours, b.k_blocks, b.b AS background_rate,
    CASE
        WHEN b.component NOT IN (SELECT component FROM has_values) THEN 'no_data'
        WHEN coalesce(ev.n_exposures, 0) = 0 THEN 'no_exposures'
        WHEN b.clear_hours < b.min_clear_hours THEN 'not_enough_data'
        WHEN b.b >= b.t                      THEN 'cannot_separate'
        ELSE 'scored'
    END AS result_status,
    CASE WHEN b.component IN (SELECT component FROM has_values)
          AND coalesce(ev.n_exposures, 0) > 0 AND b.clear_hours >= b.min_clear_hours AND b.b < b.t
         THEN least(1 / (1 + exp(-(ln(b.start_probability / (1 - b.start_probability)) + ev.points))), b.cap)
    END AS score
FROM base b
LEFT JOIN evidence ev USING (component)
LEFT JOIN eligible_episodes ee USING (component)
LEFT JOIN checked ck USING (component);

-- 8. Suspects that happen together (D26). For each pair of cards, how many of one card's clues (exposures followed by
-- a symptom) share a symptom episode with a clue of the other. The scores stay as they are: this only lets the report
-- say the two can't be told apart yet. A polyol and the polyols total are the same thing counted twice, so they're left out.
CREATE OR REPLACE TABLE suspect_overlap AS
WITH clues AS (
    SELECT DISTINCT x.component, x.start_k, e.episode_id
    FROM exposures x JOIN episode_ticks e ON e.component = x.component AND e.k BETWEEN x.start_k AND x.end_k
)
SELECT a.component, b.component AS other, count(DISTINCT a.start_k) AS shared
FROM clues a JOIN clues b ON a.episode_id = b.episode_id AND a.component <> b.component
JOIN component_rules ca ON ca.component = a.component
JOIN component_rules cb ON cb.component = b.component
WHERE coalesce(ca.parent, '') <> b.component AND coalesce(cb.parent, '') <> a.component
GROUP BY 1, 2;

-- 9. Too frequent to find a pattern (G21) ---------------------------------------------------------------
-- Symptoms a week over the usable days, and what the log repeats: foods on most days that hold something a
-- card checks. The report moves to the second pathway when symptoms are frequent and nothing reached the
-- testing point.
CREATE OR REPLACE TABLE symptom_frequency AS
WITH usable AS (SELECT day FROM day_coverage WHERE usable),
counts AS (
    SELECT (SELECT count(*) FROM usable) AS usable_days,
           (SELECT count(*) FROM symptom_episodes e JOIN usable u ON u.day = CAST(e.onset_ts AS DATE)) AS episodes,
           (SELECT count(DISTINCT i.day) FROM component_intake i JOIN usable u USING (day)
            WHERE i.component = 'wheat_gluten' AND i.grams > 0) AS wheat_days
)
SELECT usable_days, episodes,
       episodes * 7.0 / nullif(usable_days, 0) AS per_week,
       usable_days >= (SELECT value FROM global_rules WHERE name = 'frequent_min_days')
           AND episodes * 7.0 / nullif(usable_days, 0)
               >= (SELECT value FROM global_rules WHERE name = 'frequent_per_week') AS too_frequent,
       wheat_days > usable_days * (SELECT value FROM global_rules WHERE name = 'staple_share') AS wheat_on_most_days
FROM counts;

CREATE OR REPLACE TABLE staple_foods AS
WITH usable AS (SELECT day FROM day_coverage WHERE usable),
foods AS (
    SELECT lower(e.food_name) AS food_key, min(e.food_name) AS food_name, count(DISTINCT e.day) AS days
    FROM food_entries e
    JOIN usable u ON u.day = e.day
    JOIN component_intake i ON i.entry_id = e.entry_id AND i.grams > 0
    GROUP BY 1
)
SELECT food_name, days
FROM foods
WHERE days > (SELECT count(*) FROM usable) * (SELECT value FROM global_rules WHERE name = 'staple_share');
