-- 05_components.sql
-- Food names → components → grams, written to one table the scoring reads: component_intake (D14).
-- Expects food_entries (04), the reference tables (03), and two tables created by gut_finder/engine.py (empty
-- if not given): my_foods(pattern, component, grams_per_100g), the user's own label values, and
-- my_servings(pattern, unit, grams), the user's own answers for what one unit weighs.

-- 1. Match each entry to one source (D14). The user's own label values win over the reference table.
--    Whole words only, so "pear" doesn't match "pearl barley"; the longest match wins.
--    Each food name is split into its runs of 1–5 consecutive words, which are looked up exactly
--    (accents removed, lower case, punctuation as spaces). Work grows with the log, not with the
--    number of aliases.
CREATE OR REPLACE TABLE entry_phrases AS
WITH w AS (
    SELECT entry_id,
           string_split(trim(regexp_replace(strip_accents(lower(food_name)), '[^a-z0-9]+', ' ', 'g')), ' ') AS words
    FROM food_entries
),
idx AS (SELECT entry_id, words, unnest(generate_series(1, len(words))) AS i FROM w),
grams AS (SELECT entry_id, words, i, unnest(generate_series(1, 5)) AS n FROM idx)
SELECT DISTINCT entry_id, array_to_string(words[i:i + n - 1], ' ') AS phrase
FROM grams
WHERE i + n - 1 <= len(words);

CREATE OR REPLACE TABLE entry_match AS
WITH mine AS (
    SELECT p.entry_id, m.pattern,
           row_number() OVER (PARTITION BY p.entry_id ORDER BY length(m.pattern) DESC, m.pattern) AS rk
    FROM entry_phrases p
    JOIN (SELECT DISTINCT pattern FROM my_foods) m ON m.pattern = p.phrase
),
ref AS (
    SELECT p.entry_id, a.alias, a.food,
           row_number() OVER (PARTITION BY p.entry_id ORDER BY length(a.alias) DESC, a.alias) AS rk
    FROM entry_phrases p
    JOIN food_aliases a ON a.alias = p.phrase
)
-- Third source (D8): AI matches of whole food names to the published tables, made by gut_finder/matching.py
-- and loaded into ai_matches(food_name, food) by the engine (empty when AI matching didn't run).
SELECT
    e.entry_id,
    CASE WHEN mi.pattern IS NOT NULL THEN 'my_foods'
         WHEN mm.food_key IS NOT NULL AND mm.food IS NULL THEN 'unknown'   -- the user says none of these is right
         WHEN mm.food IS NOT NULL    THEN 'corrected'
         WHEN r.food IS NOT NULL     THEN 'reference'
         WHEN ai.food IS NOT NULL    THEN 'ai'
         ELSE 'unknown' END              AS match_source,
    mi.pattern                           AS my_pattern,
    r.alias                              AS ref_alias,
    CASE WHEN mi.pattern IS NULL AND mm.food_key IS NULL THEN coalesce(r.food, ai.food)
         WHEN mi.pattern IS NULL THEN mm.food END AS ref_food
FROM food_entries e
LEFT JOIN mine mi ON mi.entry_id = e.entry_id AND mi.rk = 1
LEFT JOIN ref r   ON r.entry_id  = e.entry_id AND r.rk  = 1
LEFT JOIN ai_matches ai ON ai.food_name = e.food_name
LEFT JOIN my_matches mm ON mm.food_key = lower(e.food_name);

-- 2. Grams eaten (D21). Best source first: weighed; the user's own answer; the density published for the matched
--    food; the density of that type of liquid; a typical size. Otherwise NULL, never a guess. A thick, dried or
--    whipped form isn't the ordinary liquid, and an Australian food with no density is a solid. A typical size
--    applies only to a food low enough in every component for a wrong guess not to matter.
CREATE OR REPLACE TABLE entry_amounts AS
WITH mine AS (
    SELECT p.entry_id, s.grams
    FROM entry_phrases p JOIN food_entries e USING (entry_id)
    JOIN my_servings s ON s.pattern = p.phrase AND s.unit = e.unit
    QUALIFY row_number() OVER (PARTITION BY p.entry_id ORDER BY length(s.pattern) DESC, s.pattern) = 1
),
typical AS (
    SELECT p.entry_id, t.grams
    FROM entry_phrases p JOIN food_entries e USING (entry_id)
    JOIN entry_match m USING (entry_id)
    JOIN typical_portions t ON t.pattern = p.phrase AND t.unit = e.unit
    WHERE NOT EXISTS (SELECT 1 FROM food_components fc
                      WHERE fc.food = m.ref_food AND fc.amount_per_100g > t.max_per_100g
                        AND fc.component NOT IN ('fructans', 'gos', 'wheat_gluten', 'coffee'))   -- counted by whole weight
      AND NOT EXISTS (SELECT 1 FROM my_foods mf
                      WHERE mf.pattern = m.my_pattern AND mf.grams_per_100g > t.max_per_100g)
    QUALIFY row_number() OVER (PARTITION BY p.entry_id ORDER BY length(t.pattern) DESC, t.pattern) = 1
),
liquid AS (
    SELECT f.food, l.density
    FROM reference_foods f
    JOIN liquid_densities l ON regexp_full_match(lower(trim(split_part(f.name, ',', 1))), l.first_part)
    WHERE f.tbl <> 'au'
      AND NOT regexp_matches(lower(f.name), '\b(condensed|evaporated|dry|dried|powder|powdered|whipped|frozen|concentrate|concentrated|clotted|spray|imitation)\b')
    QUALIFY row_number() OVER (PARTITION BY f.food ORDER BY length(l.first_part) DESC, l.first_part) = 1
),
sourced AS (
    SELECT e.entry_id, e.grams AS logged, e.qty * mi.grams AS answer, e.ml * d.density AS published,
           e.ml * l.density AS liquid, e.qty * t.grams AS typical
    FROM food_entries e
    JOIN entry_match m USING (entry_id)
    LEFT JOIN mine mi USING (entry_id)
    LEFT JOIN typical t USING (entry_id)
    LEFT JOIN food_density d ON d.food = m.ref_food
    LEFT JOIN liquid l ON l.food = m.ref_food
)
SELECT entry_id,
       coalesce(logged, answer, published, liquid, typical) AS grams,
       CASE WHEN logged    IS NOT NULL THEN 'logged'
            WHEN answer    IS NOT NULL THEN 'your answer'
            WHEN published IS NOT NULL THEN 'published density'
            WHEN liquid    IS NOT NULL THEN 'typical density'
            WHEN typical   IS NOT NULL THEN 'typical size' END AS amount_source
FROM sourced;

-- 3. Entries that are plainly a food that can't contain milk (D22). The log's own name has to agree with the
--    match: no word saying milk may be there, anywhere in the name, and every word explained by the table name
--    or by the list of plain words. A word nobody listed leaves the entry unknown, so a new dish fails safe.
--    A first part sharing no word with the table name is taken as a brand and skipped. What's left has to name
--    the food: a shared word that isn't just a plain word like "raw", or the whole of the table's first part
--    ("salt" for "Salt, table").
CREATE OR REPLACE TABLE milk_free_entries AS
WITH pairs AS (
    SELECT DISTINCT e.food_name, m.ref_food, mf.description, mf.row_words, mf.head_words
    FROM food_entries e
    JOIN entry_match m USING (entry_id)
    JOIN milk_free_foods mf ON mf.food = m.ref_food
    WHERE m.match_source IN ('reference', 'ai', 'corrected')
),
named AS (
    SELECT *, name_words(food_name) AS all_words,
           CASE WHEN contains(food_name, ',')
                 AND NOT list_has_any(name_words(split_part(food_name, ',', 1)), row_words)
                THEN name_words(substr(food_name, strpos(food_name, ',') + 1))
                ELSE name_words(food_name) END AS food_words
    FROM pairs
)
SELECT n.food_name, n.ref_food, n.description
FROM named n, lactose_word_lists l
WHERE NOT list_has_any(n.all_words, l.milk)
  AND (NOT list_has_all(l.plain, list_intersect(n.food_words, n.row_words)) OR list_has_all(n.food_words, n.head_words))
  AND list_has_all(list_concat(n.row_words, l.plain), n.food_words);

-- 4. Grams of each component per entry. grams stays NULL when the amount couldn't be read:
--    unknown is never treated as zero (the scoring leaves that time out, unless known grams reach the dose).
CREATE OR REPLACE TABLE component_intake AS
WITH direct AS (
    SELECT e.entry_id, e.day, e.ts, e.time_source, mf.component,
           CASE WHEN mf.grams_per_100g = 0 THEN 0 ELSE a.grams * mf.grams_per_100g / 100 END AS grams,  -- 0 is 0 at any amount
           'my foods: ' || mf.pattern        AS source
    FROM food_entries e
    JOIN entry_amounts a USING (entry_id)
    JOIN entry_match m USING (entry_id)
    JOIN my_foods mf ON mf.pattern = m.my_pattern
    WHERE m.match_source = 'my_foods'
    UNION ALL
    SELECT e.entry_id, e.day, e.ts, e.time_source, fc.component,
           CASE WHEN fc.amount_per_100g = 0 THEN 0 ELSE a.grams * fc.amount_per_100g / 100 END,
           fc.source
    FROM food_entries e
    JOIN entry_amounts a USING (entry_id)
    JOIN entry_match m USING (entry_id)
    JOIN food_components fc ON fc.food = m.ref_food AND fc.amount_per_100g IS NOT NULL
    WHERE m.match_source IN ('reference', 'ai', 'corrected')
    UNION ALL
    SELECT e.entry_id, e.day, e.ts, e.time_source, 'lactose', 0,      -- 0 is 0 at any amount
           'No milk in this food (D22): ' || mf.description
    FROM food_entries e
    JOIN entry_match m USING (entry_id)
    JOIN milk_free_entries mf ON mf.food_name = e.food_name AND mf.ref_food = m.ref_food
),
-- Parent totals (e.g. all polyols combined) = sum of the children, unless the source gave the total
-- itself (e.g. a label's "polyols" line). Never both, so nothing is counted twice.
-- "At least" rule (D19): when some children are unknown, the known ones still add up and the
-- row is marked at_least. The scoring counts it as an exposure only if the known part alone reaches the dose
-- (unknown parts can only add more); below the dose it stays unknown, never zero (D14).
-- A child with no row for that entry, or with an amount that couldn't be read, is unknown.
parent_totals AS (
    SELECT d.entry_id, any_value(d.day) AS day, any_value(d.ts) AS ts, any_value(d.time_source) AS time_source,
           c.parent AS component,
           sum(d.grams) AS grams,                                           -- known children only
           string_agg(DISTINCT d.component, ' + ' ORDER BY d.component)
               FILTER (WHERE d.grams IS NOT NULL) AS known,
           count(DISTINCT d.component) FILTER (WHERE d.grams IS NOT NULL) AS n_known
    FROM direct d
    JOIN component_rules c ON c.component = d.component
    WHERE c.parent IS NOT NULL
      AND NOT EXISTS (SELECT 1 FROM direct x WHERE x.entry_id = d.entry_id AND x.component = c.parent)
    GROUP BY d.entry_id, c.parent
),
n_children AS (SELECT parent AS component, count(*) AS n FROM component_rules WHERE parent IS NOT NULL GROUP BY 1)
SELECT entry_id, day, ts, time_source, component, grams, source, false AS at_least FROM direct
UNION ALL
SELECT p.entry_id, p.day, p.ts, p.time_source, p.component, p.grams,
       CASE WHEN p.n_known = n.n THEN 'sum of ' ELSE 'at least: sum of ' END || coalesce(p.known, 'nothing known'),
       p.n_known < n.n AS at_least
FROM parent_totals p
JOIN n_children n USING (component);
