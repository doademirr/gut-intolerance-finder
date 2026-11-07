-- 03_load_reference.sql
-- Reference layer: load the rule cards (SCIENCE.md) from the CSVs in reference/ (D11–D12).
-- The CSVs are the numbers; SCIENCE.md is the why. Change a card first, then its CSV row.
-- Each table has checks, so a typo in a hand-edited CSV stops the load instead of slipping into results.
-- An empty cell means the value is still Open (NULL).
-- Run from the project root. Safe to re-run: each table is rebuilt from its CSV.

-- food_components points at component_rules, so it has to go first (food_aliases is dropped with it, for a clean rebuild).
DROP TABLE IF EXISTS food_aliases;
DROP TABLE IF EXISTS card_symptom_rules;
DROP TABLE IF EXISTS food_components;

CREATE OR REPLACE TABLE global_rules (
    rule_id VARCHAR NOT NULL,                -- the rule's number
    name    VARCHAR PRIMARY KEY,             -- what the code looks up
    value   DOUBLE,
    unit    VARCHAR,
    basis   VARCHAR CHECK (basis IN ('Measured', 'Estimated')),
    status  VARCHAR NOT NULL CHECK (status IN ('Agreed', 'Proposed', 'Open'))
);
INSERT INTO global_rules
SELECT * FROM read_csv('reference/global_rules.csv', header = true, all_varchar = true);

CREATE OR REPLACE TABLE component_rules (
    component         VARCHAR PRIMARY KEY,   -- one row per card in SCIENCE.md
    display_name      VARCHAR NOT NULL,
    umbrella          VARCHAR,               -- level 1 group (G10)
    parent            VARCHAR,               -- e.g. each polyol also adds up into polyols_total
    window_start_h    DOUBLE CHECK (window_start_h >= 0),
    window_end_h      DOUBLE CHECK (window_end_h <= 24),  -- G1: never look back more than 24 h
    window_basis      VARCHAR CHECK (window_basis IN ('Measured', 'Estimated')),
    dose_threshold    DOUBLE CHECK (dose_threshold > 0),
    dose_unit         VARCHAR,               -- g, cup, high_histamine_food
    dose_basis        VARCHAR CHECK (dose_basis IN ('Measured', 'Estimated')),
    dose_scope        VARCHAR NOT NULL CHECK (dose_scope IN ('window', 'day')),  -- sum within the window, or per day
    evidence_strength VARCHAR CHECK (evidence_strength IN ('Strong', 'Moderate', 'Weak', 'Very weak')),  -- G18
    flag_mode         VARCHAR CHECK (flag_mode IN ('suspect', 'soft', 'watch')),                         -- G18
    clinical_test     VARCHAR CHECK (clinical_test IN ('yes', 'partial', 'no')),
    start_probability DOUBLE CHECK (start_probability > 0 AND start_probability < 1),  -- §1.1 starting point
    status            VARCHAR NOT NULL CHECK (status IN ('Agreed', 'Proposed', 'Open')),
    CHECK (window_start_h < window_end_h)
);
INSERT INTO component_rules
SELECT * FROM read_csv('reference/component_rules.csv', header = true, all_varchar = true);

-- Food → component amounts (level 2). Values come from published composition data only, never AI (G7).
-- Either an amount (grams per 100 g) or a level (high/medium/low, for components like histamine
-- where amounts vary too much to be precise).
CREATE OR REPLACE TABLE food_components (
    food            VARCHAR NOT NULL,
    component       VARCHAR NOT NULL REFERENCES component_rules (component),
    amount_per_100g DOUBLE CHECK (amount_per_100g >= 0 AND amount_per_100g <= 100),  -- can't outweigh the food
    unit            VARCHAR,
    level           VARCHAR CHECK (level IN ('high', 'medium', 'low')),
    basis           VARCHAR CHECK (basis IN ('Measured', 'Estimated')),
    source          VARCHAR NOT NULL,        -- where the number came from, e.g. "USDA FDC 171265" (D18)
    status          VARCHAR NOT NULL CHECK (status IN ('Agreed', 'Proposed', 'Open')),
    PRIMARY KEY (food, component),
    CHECK (amount_per_100g IS NOT NULL OR level IS NOT NULL),
    CHECK (amount_per_100g IS NULL OR coalesce(unit, '') = 'g')   -- the engine reads amounts as grams per 100 g
);
INSERT INTO food_components
SELECT * FROM read_csv('reference/food_components.csv', header = true, all_varchar = true);

-- Published food composition tables (D14, D18), built by gut_finder/build_food_tables.py and kept exactly
-- as published. Each food is "<table>:<id>", e.g. "uk:12-345". The rules for reading values are here only:
--   a number          → that many grams per 100 g
--   "Tr" (trace)      → 0: too small to reach any dose
--   "N" or blank      → unknown: no row, so that time is left out, never read as zero
--   per 100 ml        → unknown: millilitres aren't grams (UK alcoholic drinks)
-- Any other value stops the load.
CREATE OR REPLACE TABLE reference_foods (
    food       VARCHAR PRIMARY KEY,
    tbl        VARCHAR NOT NULL CHECK (tbl IN ('usda', 'uk', 'au', 'own')),
    source_id  VARCHAR NOT NULL,
    name       VARCHAR NOT NULL,
    food_group VARCHAR NOT NULL,             -- as the table publishes it: USDA category, CoFID group, AFCD classification
    per        VARCHAR NOT NULL CHECK (per IN ('100g', '100ml'))
);
-- Every column is named for every file, so a missing or misspelt column stops the load instead of
-- quietly becoming blank (unknown) or letting the fresh-food rule fill it with 0.
CREATE OR REPLACE TEMP TABLE food_table_raw AS
SELECT 'usda' AS tbl, food_id, name, food_group, lactose, sorbitol, mannitol, maltitol, xylitol, erythritol,
       fructose, glucose, per
FROM read_csv('reference/food_tables/usda_sr_legacy.csv', header = true, all_varchar = true, delim = ',')
UNION ALL
SELECT 'uk', food_id, name, food_group, lactose, sorbitol, mannitol, maltitol, xylitol, erythritol,
       fructose, glucose, per
FROM read_csv('reference/food_tables/uk_cofid_2021.csv', header = true, all_varchar = true, delim = ',')
UNION ALL
SELECT 'au', food_id, name, food_group, lactose, sorbitol, mannitol, maltitol, xylitol, erythritol,
       fructose, glucose, per
FROM read_csv('reference/food_tables/au_afcd_r3.csv', header = true, all_varchar = true, delim = ',');

INSERT INTO reference_foods
SELECT tbl || ':' || food_id, tbl, food_id, name, coalesce(food_group, ''), per FROM food_table_raw;

-- Turning amounts into grams (D21). food_density: the Australian table's published specific gravity, per liquid.
-- liquid_densities: the same figures by type of liquid, for foods matched to a table that publishes none.
-- first_part is a pattern for the part of the table name before the first comma.
-- typical_portions: labelled estimates, used only for foods at or below max_per_100g on every component.
CREATE OR REPLACE TABLE food_density (
    food    VARCHAR PRIMARY KEY,
    density DOUBLE NOT NULL CHECK (density > 0 AND density <= 2)
);
INSERT INTO food_density
SELECT 'au:' || food_id, CAST(specific_gravity AS DOUBLE)
FROM read_csv('reference/food_tables/au_afcd_r3_density.csv', header = true, all_varchar = true, delim = ',');
SELECT CASE WHEN count(*) > 0 THEN error('au_afcd_r3_density.csv: a density for a food not in the table') END
FROM food_density d WHERE d.food NOT IN (SELECT food FROM reference_foods);

CREATE OR REPLACE TABLE liquid_densities (
    first_part VARCHAR PRIMARY KEY,
    density    DOUBLE NOT NULL CHECK (density > 0 AND density <= 2),
    based_on   VARCHAR NOT NULL
);
INSERT INTO liquid_densities
SELECT first_part, CAST(density AS DOUBLE), based_on
FROM read_csv('reference/liquid_densities.csv', header = true, all_varchar = true, delim = ',');

CREATE OR REPLACE TABLE typical_portions (
    pattern VARCHAR NOT NULL,
    unit    VARCHAR NOT NULL,
    grams   DOUBLE NOT NULL CHECK (isfinite(grams) AND grams > 0 AND grams <= 5000),
    max_per_100g DOUBLE NOT NULL CHECK (max_per_100g >= 0 AND max_per_100g <= 100),  -- above this a guess could matter
    basis   VARCHAR NOT NULL,
    PRIMARY KEY (pattern, unit)
);
INSERT INTO typical_portions
SELECT pattern, unit, CAST(grams AS DOUBLE), CAST(max_per_100g AS DOUBLE), basis
FROM read_csv('reference/typical_portions.csv', header = true, all_varchar = true, delim = ',');

CREATE OR REPLACE TEMP TABLE food_table_values (
    food      VARCHAR NOT NULL,
    component VARCHAR NOT NULL,
    value     VARCHAR NOT NULL CHECK (value IN ('Tr', 'N') OR (length(value) <= 30
                                      AND regexp_full_match(value, '[0-9]+(\.[0-9]+)?')
                                      AND try_cast(value AS DOUBLE) <= 100))   -- no more than the food weighs
);
INSERT INTO food_table_values
SELECT tbl || ':' || food_id, component, value
FROM (UNPIVOT food_table_raw ON lactose, sorbitol, mannitol, maltitol, xylitol, erythritol, fructose, glucose
      INTO NAME component VALUE value);

INSERT INTO food_components
SELECT v.food, v.component,
       CASE WHEN v.value = 'Tr' THEN 0 ELSE CAST(v.value AS DOUBLE) END,
       'g', NULL, NULL,
       CASE f.tbl WHEN 'usda' THEN 'USDA FoodData Central SR Legacy '
                  WHEN 'uk'   THEN 'UK CoFID 2021 '
                  ELSE 'Australian Food Composition Database Release 3 ' END || f.source_id,
       'Agreed'
FROM food_table_values v
JOIN reference_foods f USING (food)
WHERE v.value <> 'N' AND f.per = '100g'
  AND v.component NOT IN ('fructose', 'glucose');   -- not cards themselves: read below

-- Excess fructose (SCIENCE.md 3.9): fructose minus glucose, per food, never below 0. Both figures are needed,
-- so a missing one leaves the food unknown. Glucose in one food doesn't cancel fructose in another.
INSERT INTO food_components
WITH sugars AS (
    SELECT food,
           max(CASE WHEN component = 'fructose' THEN CASE WHEN value = 'Tr' THEN 0 ELSE CAST(value AS DOUBLE) END END) AS fructose,
           max(CASE WHEN component = 'glucose'  THEN CASE WHEN value = 'Tr' THEN 0 ELSE CAST(value AS DOUBLE) END END) AS glucose
    FROM food_table_values WHERE component IN ('fructose', 'glucose') AND value <> 'N'
    GROUP BY food
)
SELECT s.food, 'excess_fructose', round(greatest(s.fructose - s.glucose, 0), 3), 'g', NULL, 'Estimated',
       CASE f.tbl WHEN 'usda' THEN 'USDA FoodData Central SR Legacy '
                  WHEN 'uk'   THEN 'UK CoFID 2021 '
                  ELSE 'Australian Food Composition Database Release 3 ' END || f.source_id
       || ': fructose minus glucose',
       'Proposed'
FROM sugars s JOIN reference_foods f USING (food)
WHERE s.fructose IS NOT NULL AND s.glucose IS NOT NULL AND f.per = '100g';

DROP TABLE food_table_values;
DROP TABLE food_table_raw;

-- Maltitol and isomalt in fresh food (D14 update). They are made in factories and added
-- to sugar-free and reduced-sugar products (UK/EU and Australian rules tie them to those foods), so fresh
-- food, plain dried fruit and 100% juice count as 0 g. "Fresh" = the published food groups listed in
-- reference/fresh_food_groups.csv (mixed dishes and processed products are left out). A name that says it
-- may be sweetened (sugar-free, no added sugar, diet, lite, reduced sugar or energy, nectar, a named
-- sweetener…) stays unknown, and so does a name showing a food that typically carries sugar alcohols when
-- sold sugar-free (chocolate, sweets, gum, biscuits, desserts, yoghurt, bars, cereals, jams, spreads…) or a
-- drink, sauce, mix or flavoured product (e.g. "juice drink", "pumpkin pie mix", "peanuts and chocolate
-- chips"). Simple dishes made from fresh foods (fishcakes, potato pancakes) keep the 0: the rule is about
-- added sweeteners, not about cooking. A value the table itself gives always wins.
CREATE OR REPLACE TABLE fresh_food_groups (
    tbl          VARCHAR NOT NULL CHECK (tbl IN ('usda', 'uk', 'au')),
    group_prefix VARCHAR NOT NULL CHECK (length(group_prefix) BETWEEN 1 AND 80),
    description  VARCHAR NOT NULL,
    PRIMARY KEY (tbl, group_prefix)
);
INSERT INTO fresh_food_groups
SELECT * FROM read_csv('reference/fresh_food_groups.csv', header = true, all_varchar = true);

-- Each table must have groups listed, and a listed group that matches no food is a typo: stop rather
-- than quietly cover less.
SELECT CASE WHEN count(DISTINCT tbl) < 3
            THEN error('fresh_food_groups.csv: every table (usda, uk, au) needs at least one group') END
FROM fresh_food_groups;

SELECT CASE WHEN count(*) > 0
            THEN error('fresh_food_groups.csv: ' || count(*) || ' group(s) match no food, e.g. '
                       || min(g.tbl || ' ' || g.group_prefix)) END
FROM fresh_food_groups g
WHERE NOT EXISTS (SELECT 1 FROM reference_foods f
                  WHERE f.tbl = g.tbl AND starts_with(f.food_group, g.group_prefix));

INSERT INTO food_components
SELECT f.food, z.component, 0, 'g', NULL, 'Estimated',
       'Fresh food has no ' || z.component || ' (D14): ' || g.description,
       'Agreed'
FROM reference_foods f
JOIN fresh_food_groups g
  ON g.tbl = f.tbl AND starts_with(f.food_group, g.group_prefix)
CROSS JOIN (VALUES ('maltitol'), ('isomalt')) z(component)
WHERE f.per = '100g'
  AND NOT regexp_matches(
        replace(' ' || trim(regexp_replace(strip_accents(lower(f.name)), '[^a-z0-9]+', ' ', 'g')) || ' ',
                ' not sweetened ', ' '),      -- "not sweetened" says the opposite of "sweetened"
        -- words that say the food may be sweetened
        ' (sugar free|sugarfree|sugarless|no added sugars?|no sugars? added|zero sugars?|sugars? zero|'
        || 'diet|dietetic|lite|reduced (sugars?|calories?|energy|carbs?|carbohydrates?)|'
        || '(sugars?|calories?|energy|kilojoules?) reduced|low (calories?|cal|energy|joules?|kilojoules?|sugars?)|'
        || 'lower sugars?|sweeteners?|sweetened|intense|artificial|artificially|saccharin|aspartame|'
        || 'sucralose|stevia|steviol|acesulfame|cyclamates?|neotame|advantame|monk fruit|nectars?|polyols?|'
        || 'maltitol|isomalt|sorbitol|xylitol|erythritol|mannitol|lactitol|'
        -- foods that typically carry sugar alcohols when sold sugar-free, and drinks, sauces, mixes and
        -- flavoured products (where sweeteners are added)
        || 'chocolates?|choc|carob|confectionery|candy|candies|sweets|toffees?|fudge|gum|mints|lozenges?|'
        || 'biscuits?|cookies?|cakes?|muffins?|brownies?|pastry|pastries|desserts?|puddings?|custards?|'
        || 'ice cream|yogh?urts?|bars?|cereals?|granola|muesli|protein|jams?|jelly|jellies|marmalade|spreads?|'
        || 'drinks?|cocktails?|smoothies?|punch|sauces?|mix|mixes|relish|glazed|flavou?red|flavou?rs?|seasoning|'
        || 'marzipan|candied|glace|pie|sausages?|(potato|tuna|chicken|egg|ham|pasta|macaroni) salad) ')
  AND NOT EXISTS (SELECT 1 FROM food_components c WHERE c.food = f.food AND c.component = z.component)
QUALIFY row_number() OVER (PARTITION BY f.food, z.component ORDER BY length(g.group_prefix) DESC) = 1;

-- Lactose in foods that can't contain milk (D22). Milk is the only natural source, so plain plant foods, meat,
-- fish, eggs and oils have none; it gets in when milk products are added. So: the published groups in
-- reference/milk_free_groups.csv (name_prefix narrows a group to one food), minus any food whose table name has
-- a word from reference/lactose_name_words.csv saying milk may be there. A value the table gives always wins.
-- This only lists the foods. Whether an entry really is that food is decided per entry in sql/05, from the
-- name in the log: "chicken wrap" matched to "chicken" stays unknown.
-- name_words: a name as a list of words, singular, so names can be compared. Percentages and pack weights go;
-- any other number stays, because it can name a dish ("chicken 65").
CREATE OR REPLACE MACRO name_clean(s) AS trim(
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(regexp_replace(
        regexp_replace(strip_accents(lower(s)), '\b\d+(\.\d+)?\s*(%|mg|kg|g|ml|l|oz)(\W|$)', ' ', 'g'),
        '[^a-z0-9]+', ' ', 'g'),
        '\bhalf (and )?half\b', 'cream', 'g'),            -- a dairy product, whichever way it's written
        '\bcacao\b', 'cocoa', 'g'),                      -- the same food; the tables say cocoa
        '\b[a-z]\b', ' ', 'g'),                          -- the s of mckenzie's
        '\b(\w{2,})ies\b', '\1y', 'g'),                  -- berries
        '\b(\w+)oes\b', '\1o', 'g'),                     -- tomatoes
        '\b(\w+[sxzh])es\b', '\1', 'g'),                 -- sandwiches
        '\b(\w*[^s\W])s\b', '\1', 'g'),                  -- pies, but not glass
        '\s+', ' ', 'g'));
CREATE OR REPLACE MACRO name_words(s) AS list_distinct(string_split(name_clean(s), ' '));

CREATE OR REPLACE TABLE milk_free_groups (
    tbl          VARCHAR NOT NULL CHECK (tbl IN ('usda', 'uk', 'au')),
    group_prefix VARCHAR NOT NULL CHECK (length(group_prefix) BETWEEN 1 AND 80),
    name_prefix  VARCHAR NOT NULL,
    description  VARCHAR NOT NULL,
    PRIMARY KEY (tbl, group_prefix, name_prefix)
);
INSERT INTO milk_free_groups
SELECT tbl, group_prefix, coalesce(name_prefix, ''), description
FROM read_csv('reference/milk_free_groups.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0
            THEN error('milk_free_groups.csv: ' || count(*) || ' group(s) match no food, e.g. '
                       || min(g.tbl || ' ' || g.group_prefix)) END
FROM milk_free_groups g
WHERE NOT EXISTS (SELECT 1 FROM reference_foods f
                  WHERE f.tbl = g.tbl AND starts_with(f.food_group, g.group_prefix)
                    AND starts_with(f.name, g.name_prefix));

CREATE OR REPLACE TABLE lactose_name_words (
    word VARCHAR PRIMARY KEY CHECK (regexp_full_match(word, '[a-z]{2,}')),
    kind VARCHAR NOT NULL CHECK (kind IN ('milk', 'plain'))   -- milk: may be there. plain: says nothing about milk
);
INSERT INTO lactose_name_words
SELECT word, kind FROM read_csv('reference/lactose_name_words.csv', header = true, all_varchar = true, delim = ',');

CREATE OR REPLACE TABLE lactose_word_lists AS
SELECT coalesce(list(DISTINCT w) FILTER (WHERE kind = 'milk'), [])  AS milk,
       coalesce(list(DISTINCT w) FILTER (WHERE kind = 'plain'), []) AS plain
FROM (SELECT unnest(name_words(word)) AS w, kind FROM lactose_name_words);

SELECT CASE WHEN list_has_any(milk, plain)
            THEN error('lactose_name_words.csv: a word is listed as both milk and plain') END
FROM lactose_word_lists;

CREATE OR REPLACE TABLE milk_free_foods AS
SELECT f.food, g.description, name_words(f.name) AS row_words, name_words(split_part(f.name, ',', 1)) AS head_words
FROM reference_foods f
JOIN milk_free_groups g
  ON g.tbl = f.tbl AND starts_with(f.food_group, g.group_prefix) AND starts_with(f.name, g.name_prefix)
CROSS JOIN lactose_word_lists l
WHERE f.per = '100g'
  AND NOT list_has_any(name_words(f.name), l.milk)
  AND NOT EXISTS (SELECT 1 FROM food_components c WHERE c.food = f.food AND c.component = 'lactose')
QUALIFY row_number() OVER (PARTITION BY f.food ORDER BY length(g.group_prefix) DESC, length(g.name_prefix) DESC) = 1;

-- Own table (D24, D25): foods and dishes the published tables lack, and what common dishes contain. A plain food
-- gives its lactose with a source. A dish gives its recipe, one published table food per ingredient, and its figures
-- are worked out here, so nobody types a dish's number in. A component is only worked out when every ingredient's
-- figure is known: a gap must never quietly count as 0, so that component stays unknown for that dish. A dish with a
-- `published` food keeps that food's figures: the recipe only supplies what it contains.
CREATE OR REPLACE TEMP TABLE own_foods AS
SELECT * FROM read_csv('reference/own_foods.csv', header = true, all_varchar = true, delim = ',');
CREATE OR REPLACE TEMP TABLE own_dishes AS
SELECT * FROM read_csv('reference/own_dishes.csv', header = true, all_varchar = true, delim = ',');
CREATE OR REPLACE TABLE own_dish_ingredients (
    dish          VARCHAR NOT NULL,
    ingredient    VARCHAR NOT NULL,
    grams         DOUBLE NOT NULL CHECK (isfinite(grams) AND grams > 0 AND grams <= 10000),
    food          VARCHAR NOT NULL,
    also_contains VARCHAR,                    -- what a stand-in food can't carry, e.g. the coating on coated pork
    PRIMARY KEY (dish, ingredient)            -- a repeated line would quietly change the dish's figure
);
INSERT INTO own_dish_ingredients
SELECT 'own:' || dish_id, ingredient, CAST(grams AS DOUBLE), food, also_contains
FROM read_csv('reference/own_dish_ingredients.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0 THEN error('own_foods.csv or own_dishes.csv: a row has no source') END
FROM (SELECT source FROM own_foods UNION ALL SELECT source FROM own_dishes) WHERE coalesce(trim(source), '') = '';

SELECT CASE WHEN count(*) > 0 THEN error('own_foods.csv or own_dishes.csv: a row has no valid status') END
FROM (SELECT status FROM own_foods UNION ALL SELECT status FROM own_dishes)
WHERE status IS NULL OR status NOT IN ('Agreed', 'Proposed', 'Open');

SELECT CASE WHEN count(*) > 0 THEN error('own_dish_ingredients.csv: a dish that isn''t in own_dishes.csv: ' || min(dish)) END
FROM own_dish_ingredients WHERE dish NOT IN (SELECT 'own:' || food_id FROM own_dishes);

SELECT CASE WHEN count(*) > 0 THEN error('own_dishes.csv: a dish with no recipe: ' || min(food_id)) END
FROM own_dishes WHERE 'own:' || food_id NOT IN (SELECT dish FROM own_dish_ingredients);

SELECT CASE WHEN count(*) > 0 THEN error('own_dishes.csv: a weight change that isn''t a percentage: ' || min(food_id)) END
FROM own_dishes
WHERE coalesce(weight_change_pct, '') <> ''
  AND coalesce(try_cast(weight_change_pct AS DOUBLE) NOT BETWEEN -90 AND 300, true);

SELECT CASE WHEN count(*) > 0 THEN error('own_dishes.csv: published food not in any table: ' || min(published)) END
FROM own_dishes WHERE coalesce(published, '') <> '' AND published NOT IN (SELECT food FROM reference_foods);

INSERT INTO reference_foods
SELECT 'own:' || food_id, 'own', food_id, name, food_group, '100g' FROM own_foods
UNION ALL
SELECT 'own:' || food_id, 'own', food_id, name, 'Own: dish', '100g' FROM own_dishes WHERE coalesce(published, '') = '';

SELECT CASE WHEN count(*) > 0
            THEN error('own_dish_ingredients.csv: a food that isn''t in any table: ' || min(food) || ' (' || min(ingredient) || ')') END
FROM own_dish_ingredients WHERE food NOT IN (SELECT food FROM reference_foods);

INSERT INTO food_components
SELECT 'own:' || food_id, 'lactose', CAST(lactose AS DOUBLE), 'g', NULL, 'Estimated', 'Own table: ' || source, status
FROM own_foods;

-- Lactose for table foods that have none published, so a blank row doesn't leave a common food unknown. same_as
-- copies the figure from the same kind of food in another table, so nobody types it. A typed figure needs a source.
-- A published figure always wins: giving a food that already has one stops the load.
CREATE OR REPLACE TEMP TABLE own_figures AS
SELECT * FROM read_csv('reference/own_figures.csv', header = true, all_varchar = true, delim = ',',
                       columns = {'food': 'VARCHAR', 'lactose': 'VARCHAR', 'same_as': 'VARCHAR', 'source': 'VARCHAR',
                                  'note': 'VARCHAR', 'status': 'VARCHAR'});

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: a row has no source or no valid status: ' || min(food)) END
FROM own_figures WHERE coalesce(trim(source), '') = '' OR coalesce(status, '') NOT IN ('Agreed', 'Proposed', 'Open');

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: a food that isn''t in any table: ' || min(food)) END
FROM own_figures WHERE food NOT IN (SELECT food FROM reference_foods);

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: not a per-100 g row: ' || min(f.food)) END
FROM own_figures o JOIN reference_foods f ON f.food IN (o.food, o.same_as) WHERE f.per <> '100g';

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: a food already counted as having no lactose: ' || min(food)) END
FROM own_figures WHERE food IN (SELECT food FROM milk_free_foods);

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: give a figure or same_as, not both: ' || min(food)) END
FROM own_figures WHERE (coalesce(lactose, '') = '') = (coalesce(same_as, '') = '');

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: a figure that isn''t a percentage: ' || min(food)) END
FROM own_figures
WHERE coalesce(lactose, '') <> '' AND coalesce(try_cast(lactose AS DOUBLE) NOT BETWEEN 0 AND 100, true);

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: same_as has no figure to copy: ' || min(o.food)) END
FROM own_figures o LEFT JOIN food_components c ON c.food = o.same_as AND c.component = 'lactose'
WHERE coalesce(o.same_as, '') <> ''
  AND (c.amount_per_100g IS NULL OR o.same_as LIKE 'own:%');     -- only a published figure can be copied

SELECT CASE WHEN count(*) > 0 THEN error('own_figures.csv: this food already has a figure: ' || min(o.food)) END
FROM own_figures o JOIN food_components c ON c.food = o.food AND c.component = 'lactose';

SELECT CASE WHEN count(*) > count(DISTINCT food) THEN error('own_figures.csv: a food is listed twice') END
FROM own_figures;

INSERT INTO food_components
SELECT o.food, 'lactose', coalesce(try_cast(o.lactose AS DOUBLE), c.amount_per_100g), 'g', NULL, 'Estimated',
       'Own figure: ' || o.source, o.status
FROM own_figures o LEFT JOIN food_components c ON c.food = o.same_as AND c.component = 'lactose';

INSERT INTO food_components
WITH known AS (
    SELECT food, component, amount_per_100g AS amount
    FROM food_components WHERE amount_per_100g IS NOT NULL AND unit = 'g'
    UNION ALL
    SELECT m.food, 'lactose', 0 FROM milk_free_foods m
    WHERE m.food NOT IN (SELECT food FROM food_components WHERE component = 'lactose' AND amount_per_100g IS NOT NULL)
), recipe AS (
    -- cooking changes a dish's weight, so the figure is per 100 g of the finished dish
    SELECT i.dish, count(*) AS lines,
           sum(i.grams) * (1 + coalesce(try_cast(any_value(d.weight_change_pct) AS DOUBLE), 0) / 100) AS total
    FROM own_dish_ingredients i JOIN own_dishes d ON 'own:' || d.food_id = i.dish
    GROUP BY i.dish
)
SELECT i.dish, k.component, round(sum(i.grams * k.amount) / any_value(r.total), 2), 'g', NULL, 'Estimated',
       'Own table, typical recipe: ' || any_value(d.source), any_value(d.status)
FROM own_dish_ingredients i
JOIN recipe r USING (dish)
JOIN own_dishes d ON 'own:' || d.food_id = i.dish
JOIN known k ON k.food = i.food
WHERE coalesce(d.published, '') = ''
GROUP BY i.dish, k.component
HAVING count(*) = any_value(r.lines);

-- What each ingredient contains (D25): allergens from the UK, Australian and US lists, triggers from the science
-- cards. Tagged once per ingredient, and every dish using it inherits the tag, so a new tag never means redoing
-- dishes. A tag means "typically contains". No tag means "not known to contain", never "free from".
CREATE OR REPLACE TABLE tag_list (
    tag    VARCHAR PRIMARY KEY,
    kind   VARCHAR NOT NULL CHECK (kind IN ('allergen', 'trigger', 'lead')),
    source VARCHAR NOT NULL
);
INSERT INTO tag_list
SELECT tag, kind, source FROM read_csv('reference/tag_list.csv', header = true, all_varchar = true, delim = ',');

CREATE OR REPLACE TEMP TABLE ingredient_tags AS
SELECT * FROM read_csv('reference/ingredient_tags.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0 THEN error('ingredient_tags.csv: an ingredient nobody has reviewed: ' || min(food)) END
FROM own_dish_ingredients WHERE food NOT IN (SELECT food FROM ingredient_tags);

CREATE OR REPLACE TABLE food_tags (
    food VARCHAR NOT NULL,
    tag  VARCHAR NOT NULL,
    PRIMARY KEY (food, tag)
);
INSERT INTO food_tags
SELECT DISTINCT food, trim(unnest(string_split(tags, ';'))) FROM ingredient_tags WHERE coalesce(tags, '') <> '';

CREATE OR REPLACE TABLE dish_tags (
    food VARCHAR NOT NULL,                    -- the dish's own row, or the published food it describes
    tag  VARCHAR NOT NULL,
    basis VARCHAR NOT NULL CHECK (basis IN ('recipe', 'often')),
    PRIMARY KEY (food, tag)
);
INSERT INTO dish_tags
WITH line_tags AS (
    SELECT i.dish, t.tag FROM own_dish_ingredients i JOIN food_tags t USING (food)
    UNION
    SELECT dish, trim(unnest(string_split(also_contains, ';')))
    FROM own_dish_ingredients WHERE coalesce(also_contains, '') <> ''
)
SELECT DISTINCT coalesce(nullif(d.published, ''), 'own:' || d.food_id), l.tag, 'recipe'
FROM line_tags l JOIN own_dishes d ON 'own:' || d.food_id = l.dish;

-- Lactose only comes from milk, so a published dish with lactose contains milk, even if a typical recipe doesn't show it.
INSERT OR IGNORE INTO dish_tags
SELECT d.published, 'milk', 'recipe'
FROM own_dishes d JOIN food_components c ON c.food = d.published AND c.component = 'lactose'
WHERE c.amount_per_100g > 0;

-- What a dish often holds beyond its one recipe: versions that vary, and what it's usually served with. Kept apart
-- from recipe tags because it's a weaker claim, and a tag the recipe already gives stays 'recipe'.
CREATE OR REPLACE TEMP TABLE own_dish_often AS
SELECT * FROM read_csv('reference/own_dish_often.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0 THEN error('own_dish_often.csv: a row with no dish, or a dish that isn''t in own_dishes.csv') END
FROM own_dish_often WHERE dish_id IS NULL OR dish_id NOT IN (SELECT food_id FROM own_dishes);

SELECT CASE WHEN count(*) > 0 THEN error('own_dish_often.csv: a row has no tags') END
FROM own_dish_often WHERE coalesce(trim(tags), '') = '';

SELECT CASE WHEN count(*) > 0 THEN error('own_dish_often.csv: a row has no reason') END
FROM own_dish_often WHERE coalesce(trim(why), '') = '';

INSERT OR IGNORE INTO dish_tags
SELECT DISTINCT coalesce(nullif(d.published, ''), 'own:' || d.food_id), trim(unnest(string_split(o.tags, ';'))), 'often'
FROM own_dish_often o JOIN own_dishes d ON d.food_id = o.dish_id;

SELECT CASE WHEN count(*) > 0 THEN error('a tag that isn''t in tag_list.csv: ' || min(tag)) END
FROM (SELECT tag FROM food_tags UNION SELECT tag FROM dish_tags) WHERE tag NOT IN (SELECT tag FROM tag_list);

-- Lactose only comes from milk, so an ingredient with lactose has to carry the milk tag. USDA reports lab traces
-- in raw broccoli and eggs, which contain no milk: those three are named, so a real miss still stops the load.
SELECT CASE WHEN count(*) > 0 THEN error('ingredient_tags.csv: lactose but no milk tag: ' || min(c.food)) END
FROM food_components c
JOIN ingredient_tags g ON g.food = c.food
WHERE c.component = 'lactose' AND c.amount_per_100g > 0
  AND c.food NOT IN ('usda:170379', 'usda:172183', 'usda:172184')
  AND c.food NOT IN (SELECT food FROM food_tags WHERE tag = 'milk');

-- Cards with no table figure: fructans, GOS, wheat and gluten, coffee (SCIENCE.md 3.10, 3.11, 3.16, 3.12). No table prints these,
-- so a food's whole weight counts if it's tagged: 100 per 100 g, against a dose in grams of tagged food (the portion
-- rule). Best source first: a reviewed ingredient (ingredient_tags.csv); then words in the table name
-- (trigger_words.csv); then a plain food group or a word for a plain food, which counts as 0 (trigger_free_groups.csv,
-- or 'clear' in trigger_words.csv).
-- Anything else stays unknown, never 0. A dish counts only its tagged ingredients' grams, from its recipe.
CREATE OR REPLACE TEMP TABLE trigger_words AS
SELECT phrase, tags, ' ' || name_clean(phrase) || ' ' AS words
FROM read_csv('reference/trigger_words.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0 THEN error('trigger_words.csv: a tag that isn''t a card: ' || min(phrase)) END
FROM (SELECT phrase, trim(unnest(string_split(tags, ';'))) AS tag FROM trigger_words)
WHERE tag NOT IN ('fructans', 'gos', 'wheat_gluten', 'coffee', 'none', 'no_grain', 'clear');

CREATE OR REPLACE TABLE trigger_free_groups (
    tbl          VARCHAR NOT NULL CHECK (tbl IN ('usda', 'uk', 'au', 'own')),
    group_prefix VARCHAR NOT NULL CHECK (length(group_prefix) BETWEEN 1 AND 80),
    description  VARCHAR NOT NULL,
    PRIMARY KEY (tbl, group_prefix)
);
INSERT INTO trigger_free_groups
SELECT tbl, group_prefix, description
FROM read_csv('reference/trigger_free_groups.csv', header = true, all_varchar = true, delim = ',');

SELECT CASE WHEN count(*) > 0 THEN error('trigger_free_groups.csv: a group that matches no food: ' || min(g.tbl || ' ' || g.group_prefix)) END
FROM trigger_free_groups g
WHERE NOT EXISTS (SELECT 1 FROM reference_foods f WHERE f.tbl = g.tbl AND starts_with(f.food_group, g.group_prefix));

CREATE OR REPLACE TEMP TABLE tag_cards AS
SELECT * FROM (VALUES ('fructans', 'fructans'), ('gos', 'gos'), ('wheat', 'wheat_gluten'), ('rye', 'wheat_gluten'),
                      ('barley', 'wheat_gluten'), ('coffee', 'coffee')) t(tag, component);

CREATE OR REPLACE TEMP TABLE food_trigger AS
WITH cards AS (SELECT DISTINCT component FROM tag_cards),
reviewed AS (
    SELECT g.food, c.component,
           CASE WHEN EXISTS (SELECT 1 FROM food_tags t JOIN tag_cards m USING (tag)
                             WHERE t.food = g.food AND m.component = c.component) THEN 100.0 ELSE 0.0 END AS amount
    FROM ingredient_tags g CROSS JOIN cards c
),
named AS (
    SELECT f.food, f.tbl, f.food_group,
           regexp_replace(' ' || name_clean(f.name) || ' ',
                          (SELECT ' (' || string_agg(trim(words), '|') || ') ' FROM trigger_words WHERE tags = 'none'),
                          ' ', 'g') AS words,
           EXISTS (SELECT 1 FROM trigger_words w
                   WHERE w.tags = 'no_grain' AND contains(' ' || name_clean(f.name) || ' ', w.words)) AS no_grain
    FROM reference_foods f
    WHERE f.per = '100g' AND f.food_group <> 'Own: dish' AND f.food NOT IN (SELECT food FROM ingredient_tags)
),
hits AS (
    SELECT DISTINCT n.food, trim(unnest(string_split(w.tags, ';'))) AS component
    FROM named n JOIN trigger_words w ON contains(n.words, w.words)
    WHERE w.tags NOT IN ('none', 'no_grain', 'clear')
      AND NOT (n.no_grain AND contains(w.tags, 'wheat_gluten')     -- a named grain still counts in "rice cakes with rye"
               AND w.phrase NOT IN ('wheat', 'rye', 'barley', 'spelt'))
),
known AS (          -- a food with a word, or in a plain group, is known for every card
    SELECT food FROM hits
    UNION
    SELECT n.food FROM named n JOIN trigger_free_groups g ON g.tbl = n.tbl AND starts_with(n.food_group, g.group_prefix)
    UNION
    SELECT n.food FROM named n JOIN trigger_words w ON w.tags IN ('clear', 'no_grain') AND contains(n.words, w.words)
)
SELECT food, component, amount FROM reviewed
UNION ALL
SELECT k.food, c.component, CASE WHEN h.food IS NULL THEN 0.0 ELSE 100.0 END
FROM known k CROSS JOIN cards c LEFT JOIN hits h ON h.food = k.food AND h.component = c.component;

INSERT OR IGNORE INTO food_components       -- a figure the generic recipe step already gave stands
WITH line AS (      -- what a stand-in ingredient can't carry counts too, e.g. the coating on coated pork
    SELECT i.dish, i.ingredient, i.grams, t.component,
           CASE WHEN EXISTS (SELECT 1 FROM tag_cards m
                             WHERE m.component = t.component
                               AND contains(';' || replace(coalesce(i.also_contains, ''), ' ', '') || ';', ';' || m.tag || ';'))
                THEN 100.0 ELSE t.amount END AS amount
    FROM own_dish_ingredients i JOIN food_trigger t USING (food)
),
recipe AS (
    SELECT i.dish, count(*) AS lines,
           sum(i.grams) * (1 + coalesce(try_cast(any_value(d.weight_change_pct) AS DOUBLE), 0) / 100) AS total
    FROM own_dish_ingredients i JOIN own_dishes d ON 'own:' || d.food_id = i.dish
    GROUP BY i.dish
)
SELECT coalesce(nullif(any_value(d.published), ''), l.dish), l.component,
       least(round(sum(l.grams * l.amount) / any_value(r.total), 2), 100), 'g', NULL, 'Estimated',
       'Tagged ingredients in a typical recipe: ' || any_value(d.source), 'Proposed'
FROM line l JOIN recipe r USING (dish) JOIN own_dishes d ON 'own:' || d.food_id = l.dish
GROUP BY l.dish, l.component
HAVING count(*) = any_value(r.lines);       -- every ingredient known, or the dish stays unknown

INSERT INTO food_components
SELECT t.food, t.component, t.amount, 'g', NULL, 'Estimated', 'Tagged by ingredient review, name or food group', 'Proposed'
FROM food_trigger t
WHERE NOT EXISTS (SELECT 1 FROM food_components c WHERE c.food = t.food AND c.component = t.component);

-- The only symptoms that count for some cards: wind alone is normal after fructans or GOS, and so is an urge to go
-- after coffee (SCIENCE.md 3.10–3.12). A card with no rows here counts every symptom.
CREATE OR REPLACE TABLE card_symptom_rules (
    component VARCHAR NOT NULL REFERENCES component_rules (component),
    symptom   VARCHAR NOT NULL CHECK (symptom = lower(trim(symptom)) AND length(symptom) > 0),
    PRIMARY KEY (component, symptom)
);
INSERT INTO card_symptom_rules
SELECT component, symptom FROM read_csv('reference/card_symptom_rules.csv', header = true, all_varchar = true, delim = ',');

-- Food names → reference foods (D14). AI matching comes later (D8, D11); v1 matches whole words.
-- Aliases are 1–5 lower-case words (letters and digits), matched exactly against runs of words in food names.
CREATE OR REPLACE TABLE food_aliases (
    alias VARCHAR PRIMARY KEY CHECK (regexp_full_match(alias, '[a-z0-9]+( [a-z0-9]+){0,4}')),  -- 1–5 words
    food  VARCHAR NOT NULL
);
INSERT INTO food_aliases
SELECT * FROM read_csv('reference/food_aliases.csv', header = true, all_varchar = true);
