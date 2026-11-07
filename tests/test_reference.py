"""Checks for the reference layer (sql/03_load_reference.sql + reference/*.csv).

Run from the project root:  .venv/bin/python tests/test_reference.py
Uses an in-memory DuckDB, so it never touches data/gut.duckdb.
"""
import duckdb

LOAD_SQL = open("sql/03_load_reference.sql").read()


def fresh():
    con = duckdb.connect()  # in-memory
    con.execute(LOAD_SQL)
    return con


def must_fail(con, sql, why):
    try:
        con.execute(sql)
    except duckdb.Error:
        return
    raise AssertionError(f"not caught: {why}")


def test_loads_and_reruns():
    con = fresh()
    con.execute(LOAD_SQL)  # second run must also work


def test_every_card_is_loaded():
    con = fresh()
    n = con.execute("SELECT count(*) FROM component_rules").fetchone()[0]
    assert n == 15, n


def test_parents_exist():
    # e.g. each polyol adds up into polyols_total, so that row must exist
    con = fresh()
    orphans = con.execute("""
        SELECT c.component FROM component_rules c
        LEFT JOIN component_rules p ON c.parent = p.component
        WHERE c.parent IS NOT NULL AND p.component IS NULL
    """).fetchall()
    assert orphans == [], orphans


def test_polyols_share_the_combined_window():
    # a child's window must sit inside its parent's, or the combined total misses grams
    con = fresh()
    bad = con.execute("""
        SELECT c.component FROM component_rules c
        JOIN component_rules p ON c.parent = p.component
        WHERE c.window_start_h < p.window_start_h OR c.window_end_h > p.window_end_h
    """).fetchall()
    assert bad == [], bad


def test_numbers_only_on_settled_rows():
    # a suspect can't be flagged from a card with no dose or no window
    con = fresh()
    rows = con.execute("""
        SELECT component FROM component_rules
        WHERE status <> 'Open' AND (dose_threshold IS NULL OR window_start_h IS NULL OR window_end_h IS NULL)
    """).fetchall()
    assert rows == [], rows


def test_typos_are_caught():
    con = fresh()
    must_fail(con, "INSERT INTO component_rules (component, display_name, dose_scope, status) "
                   "VALUES ('x', 'X', 'window', 'proposed')", "lower-case status")
    must_fail(con, "INSERT INTO component_rules (component, display_name, window_start_h, window_end_h, dose_scope, status) "
                   "VALUES ('y', 'Y', 8, 2, 'window', 'Open')", "window ends before it starts")
    must_fail(con, "INSERT INTO component_rules (component, display_name, window_start_h, window_end_h, dose_scope, status) "
                   "VALUES ('z', 'Z', 0, 72, 'window', 'Open')", "window past the 24 h limit (G1)")
    must_fail(con, "INSERT INTO food_components (food, component, amount_per_100g, unit, source, status) "
                   "VALUES ('milk', 'lactos', 4.7, 'g', 'test', 'Open')", "unknown component")
    must_fail(con, "INSERT INTO food_components (food, component, source, status) "
                   "VALUES ('milk', 'lactose', 'test', 'Open')", "food row with neither amount nor level")


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} passed")
