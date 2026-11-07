-- 01_load_raw.sql
-- Raw layer: load the Cronometer "Food & Recipe Entries" export exactly as it is.
-- Nothing is cleaned or changed here, so any later result can be traced back to source (D3).
-- Run from the project root. The CSV lives in data/private/ (gitignored, never committed).
-- Development tool for exploring a log in the DuckDB UI; the app itself loads files through gut_finder/engine.py.

CREATE OR REPLACE TABLE raw_food_log AS
SELECT *
FROM read_csv('data/private/cronometer_food_log.csv', header = true, all_varchar = true);  -- every column as text: types are fixed in the clean layer
