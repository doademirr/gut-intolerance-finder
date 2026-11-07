"""Checks for the engine (sql/04–06 + gut_finder/engine.py).

Run from the project root:  .venv/bin/python tests/test_engine.py
Uses the test-only reference in tests/fixtures/ref and in-memory DuckDB; never touches real data.
"""
import csv
import math
import random
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gut_finder import engine, synthetic  # noqa: E402

REF = Path(__file__).resolve().parent / "fixtures" / "ref"
# The fixture's cards copy reference/ except maltitol, which is kept Weak (soft flag) on purpose so the
# weak-evidence cap and wording can be tested with a component the engine can score.
TMP = Path(tempfile.mkdtemp(prefix="gut_finder_tests_"))

import gut_finder.__main__ as _cli   # noqa: E402
_cli.CONSENT_FILE = TMP / "agreed.json"    # the command-line tests run as someone who has already agreed
_cli.CONSENT_FILE.write_text('{"version": %d}' % _cli.CONSENT_VERSION)


def write_csv(name, header, rows):
    path = TMP / name
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    return path


def filler(days, start_day=1, skip=()):
    """Plain rice at lunch and dinner every day, so days count as logged."""
    rows = []
    for d in range(start_day, start_day + days):
        if d in skip:
            continue
        day = f"2026-01-{d:02d}"
        rows += [[day, "12:00 PM", "Lunch", "Rice, white", "200.00 g", "x"],
                 [day, "7:00 PM", "Dinner", "Rice, white", "200.00 g", "x"]]
    return rows


def run(food, symptoms, my_foods=None, exclude=None, symptoms_from=None, symptoms_to=None, my_servings=None, ref=REF,
        my_matches=None, checked=None):
    """Run the engine on in-test rows. The symptom-logging period defaults to the food log's dates."""
    f = write_csv("food.csv", engine.FOOD_COLUMNS, food)
    s = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, symptoms)
    m = write_csv("my_foods.csv", engine.MY_FOOD_COLUMNS, my_foods) if my_foods else None
    x = write_csv("exclude.csv", engine.EXCLUDE_COLUMNS, exclude) if exclude else None
    v = write_csv("my_servings.csv", engine.MY_SERVING_COLUMNS, my_servings) if my_servings else None
    mm = write_csv("my_matches.csv", ["your_food", "correct_food", "matched_to"], my_matches) if my_matches else None
    c = write_csv("checked.csv", engine.CHECKED_SIGN_COLUMNS, checked) if checked else None
    days = sorted(r[0] for r in food)
    return engine.run(f, s, m, x, symptoms_from or days[0], symptoms_to or days[-1], reference_dir=ref,
                      my_servings_csv=v, my_matches_csv=mm, checked_signs_csv=c)


def result(con, component):
    cur = con.execute("SELECT * FROM component_results WHERE component = ?", [component])
    return dict(zip([d[0] for d in cur.description], cur.fetchone()))


SWEET = [["test sweet", "sorbitol", "100"]]   # 1 g of "test sweet" = 1 g sorbitol, to set exact doses


# --- Clean layer ------------------------------------------------------------------------------------

def test_amounts_only_convert_weights():
    food = [["2026-01-01", "8:00 AM", "Breakfast", f, a, "x"] for f, a in
            [("A", "150.00 g"), ("B", "1.00 100g"), ("C", "2.00 oz"), ("D", "1.00 cup"), ("E", "250.00 ml")]]
    con = run(food, []).con
    grams = dict(con.execute("SELECT food_name, round(grams, 2) FROM food_entries").fetchall())
    assert grams == {"A": 150.0, "B": 100.0, "C": 56.7, "D": None, "E": None}, grams


def test_time_tiers():
    food = [["2026-01-01", "8:10 AM", "Breakfast", "A", "1.00 g", "x"],
            ["2026-01-01", "", "Breakfast", "B", "1.00 g", "x"],      # same meal, same day
            ["2026-01-02", "", "Breakfast", "C", "1.00 g", "x"],      # usual breakfast time
            ["2026-01-02", "", "Snacks", "D", "1.00 g", "x"]]         # no snack time anywhere
    con = run(food, []).con
    got = dict(con.execute("SELECT food_name, time_source || ' ' || coalesce(strftime(ts, '%H:%M'), '-') "
                           "FROM food_entries").fetchall())
    assert got == {"A": "logged 08:10", "B": "same_meal 08:10", "C": "usual_time 08:10", "D": "day_only -"}, got


# --- Foods that can't contain milk (D22) ------------------------------------------------------------

def lactose(names):
    """{food name: lactose grams, or None if unknown} for 100 g of each food."""
    con = run([["2026-01-01", "8:00 AM", "Breakfast", n, "100.00 g", "x"] for n in names], []).con
    return dict(con.execute("""SELECT e.food_name, c.grams FROM food_entries e LEFT JOIN component_intake c
                               ON c.entry_id = e.entry_id AND c.component = 'lactose'""").fetchall())


def test_a_plain_food_with_no_figure_counts_as_no_lactose():
    names = ["Test Chicken, Raw", "Test Chicken Breast, Skinless", "Some Brand, Test Chicken, Organic",
             "Test Cocoa Powder", "Some Brand, Organic Cacao Test Cocoa", "Test Chicken Mince, 95% Lean, 500g"]
    assert lactose(names) == dict.fromkeys(names, 0), lactose(names)


def test_a_name_that_says_more_than_the_matched_food_stays_unknown():
    names = ["Test Chicken Wrap",              # the example the rule exists for: "chicken wrap" isn't "chicken"
             "Test Chicken Korma",             # a dish on no list: an unexplained word is enough
             "Test Chicken Pieces, Creamy",
             "Curry House Special, Test Chicken",   # the first part looks like a brand, but names a dish
             "Cheese Shop, Test Chicken",            # even a brand can say so
             "Wraps, Test Chicken",
             "Test Chicken 65",                # a number can name a dish
             "Creme Fraiche, Test Chicken",    # dairy in the part that looks like a brand
             "Crème Fraîche, Test Chicken",
             "Half-and-half, Test Chicken",
             "Half & Half, Test Chicken",
             "Test Cocoa Smoothie"]
    assert lactose(names) == dict.fromkeys(names), lactose(names)


def test_a_brand_that_shares_a_word_with_the_food_stays_part_of_the_name():
    names = ["Test Chicken Co, Korma Sauce", "Test Chicken Co, Spiced"]
    assert lactose(names) == dict.fromkeys(names), lactose(names)


def test_a_food_whose_whole_name_is_a_plain_word():
    assert lactose(["Salt"]) == {"Salt": 0}


def test_foods_where_lactose_can_be_added_stay_unknown():
    names = ["Test Sausage", "Test Potatoes", "Test Almonds"]
    assert lactose(names) == dict.fromkeys(names), lactose(names)


def test_the_tables_own_lactose_figure_still_wins():
    assert lactose(["Test Broccoli"])["Test Broccoli"] == 0.2


def test_no_lactose_needs_no_amount_and_is_named_as_a_rule():
    food = [["2026-01-01", "8:00 AM", "Breakfast", "Test Chicken", "1.00 serving", "x"]]
    con = run(food, []).con
    grams, source = con.execute("SELECT grams, source FROM component_intake WHERE component = 'lactose'").fetchone()
    assert grams == 0 and source == "No milk in this food (D22): Poultry", (grams, source)
    assert con.execute("SELECT count(*) FROM component_intake WHERE component = 'sorbitol'").fetchone()[0] == 0


def test_own_label_values_beat_the_no_milk_rule():
    con = run([["2026-01-01", "8:00 AM", "Breakfast", "Test Chicken", "100.00 g", "x"]], [],
              my_foods=[["test chicken", "lactose", "2"]]).con
    assert con.execute("SELECT grams FROM component_intake WHERE component = 'lactose'").fetchone()[0] == 2


def test_the_report_says_how_many_foods_the_rule_covered():
    from gut_finder import report
    res = run(filler(2) + [["2026-01-01", "8:00 AM", "Breakfast", "Test Chicken", "100.00 g", "x"]], [])
    assert "**No lactose by rule:** 1 entry" in report.build(res), report.build(res)


def test_episodes_merge_within_3_hours():
    con = run(filler(2), [["2026-01-01", "14:00", "bloating", ""], ["2026-01-01", "15:30", "pain", ""],
                          ["2026-01-01", "19:00", "bloating", ""]]).con
    assert con.execute("SELECT count(*) FROM symptom_episodes").fetchone()[0] == 2


# --- Amounts that aren't grams ----------------------------------------------------------------------

def amounts(food, my_servings=None):
    """{food name: (grams, how they were worked out)} for one-off foods on one day."""
    rows = [["2026-01-01", "8:00 AM", "Breakfast", f, a, "x"] for f, a in food]
    con = run(rows, [], my_servings=my_servings).con
    n, ids = con.execute("SELECT count(*), count(DISTINCT entry_id) FROM entry_amounts").fetchone()
    assert n == ids == len(rows), (n, ids)                  # one amount per entry: a repeat would double a dose
    dupes = con.execute("SELECT count(*) FROM (SELECT entry_id, component FROM component_intake "
                        "GROUP BY 1, 2 HAVING count(*) > 1)").fetchone()[0]
    assert dupes == 0, dupes
    return {n: (None if g is None else round(g, 2), s) for n, g, s in con.execute(
        "SELECT e.food_name, a.grams, a.amount_source FROM food_entries e JOIN entry_amounts a USING (entry_id)"
    ).fetchall()}


def test_ml_uses_the_density_published_for_the_matched_food():
    assert amounts([("Fixture prune juice", "100.00 ml")]) == {"Fixture prune juice": (130.0, "published density")}


def test_ml_on_a_table_with_no_density_borrows_the_liquid_type():
    assert amounts([("Fixture whole milk", "250.00 ml")]) == {"Fixture whole milk": (257.5, "typical density")}


def test_every_way_of_writing_a_volume():
    for amount, grams in (("1.00 x 250.0 ml", 257.5), ("1.00 cup (250ml)", 257.5), ("2.00 serving (100 ml)", 206.0),
                          ("0.25 l", 257.5), ("1.00 x 12.0 fl oz", 365.53)):
        got = amounts([("Fixture whole milk", amount)])["Fixture whole milk"]
        assert got == (grams, "typical density"), (amount, got)


def test_volumes_that_stay_unknown():
    # a thick milk isn't ordinary milk, a solid has no density, "1 cup" has no stated size, and a powder's
    # made-up volume says nothing about its weight
    for food, amount in (("Fixture condensed milk", "100.00 ml"), ("Fixture mushroom", "100.00 ml"),
                         ("Fixture whole milk", "1.00 cup"), ("Fixture whole milk", "335ml (once prepared)")):
        assert amounts([(food, amount)])[food] == (None, None), (food, amount)


def test_typical_size_only_for_its_own_unit():
    assert amounts([("Fixture whey powder", "2.00 scoop")]) == {"Fixture whey powder": (60.0, "typical size")}
    assert amounts([("Fixture whey powder", "1.00 serving")]) == {"Fixture whey powder": (None, None)}


def test_typical_size_is_refused_where_a_wrong_guess_could_matter():
    # sweet dried whey is mostly lactose: 25 g or 30 g is 17.5 g or 21 g, so it has to be asked, not assumed
    assert amounts([("Fixture sweet whey", "1.00 scoop")]) == {"Fixture sweet whey": (None, None)}
    rows = [["2026-01-01", "8:00 AM", "Breakfast", "Label whey blend", "1.00 scoop", "x"]]
    con = run(rows, [], my_foods=[["label whey blend", "lactose", "45"]]).con
    assert con.execute("SELECT grams FROM entry_amounts").fetchone()[0] is None


def test_your_own_answer_wins_and_logged_grams_win_over_it():
    mine = [["whey powder", "scoop", "25"], ["whole milk", "ml", "1.1"]]
    got = amounts([("Fixture whey powder", "2.00 scoop"), ("Fixture whole milk", "100.00 ml")], mine)
    assert got == {"Fixture whey powder": (50.0, "your answer"), "Fixture whole milk": (110.0, "your answer")}, got
    assert amounts([("Fixture whey powder", "40.00 g")], mine) == {"Fixture whey powder": (40.0, "logged")}


def test_the_longest_of_your_answers_wins():
    mine = [["whey", "scoop", "25"], ["fixture whey powder", "scoop", "40"]]
    assert amounts([("Fixture whey powder", "1.00 scoop")], mine)["Fixture whey powder"] == (40.0, "your answer")


def test_your_answer_works_for_a_food_with_your_own_label_value():
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test sweet", "2.00 piece", "x"]]
    con = run(food, [], my_foods=SWEET, my_servings=[["test sweet", "piece", "3"]]).con
    assert result(con, "sorbitol")["n_exposures"] == 1      # 2 pieces × 3 g = 6 g, above the 5 g dose


def test_a_converted_volume_reaches_the_dose():
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Fixture whole milk", "250.00 ml", "x"]]
    con = run(food, []).con
    grams = con.execute("SELECT round(ci.grams, 2) FROM component_intake ci JOIN food_entries e USING (entry_id) "
                        "WHERE e.food_name = 'Fixture whole milk' AND ci.component = 'lactose'").fetchone()[0]
    assert grams == 12.36, grams                            # 250 ml × 1.03 × 4.8 g per 100 g: above 12 g


def test_my_servings_file_is_checked():
    for rows, why in (([["***", "scoop", "25"]], "pattern"), ([["one two three four five six", "scoop", "25"]], "words"),
                      ([["whey", "", "25"]], "no unit"), ([["whey", "scoop", "x"]], "not a number"),
                      ([["whey", "scoop", "0"]], "zero"), ([["whey", "scoop", "6000"]], "too big"),
                      ([["whey", "scoop", "25"], ["Whey", "Scoop", "30"]], "listed twice")):
        try:
            run(filler(2), [], my_servings=rows)
        except engine.InputError:
            continue
        raise AssertionError(f"not rejected: {why}")


def test_command_line_uses_my_servings():
    import contextlib, io
    from gut_finder.__main__ import main
    food = filler(2) + [["2026-01-01", "9:00 AM", "Snacks", "Fixture whey powder", "2.00 scoop", "x"]]
    args = ["--food", str(write_csv("food.csv", engine.FOOD_COLUMNS, food)),
            "--symptoms", str(write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])),
            "--symptoms-from", "2026-01-01", "--symptoms-to", "2026-01-02", "--overwrite"]
    mine = write_csv("my_servings.csv", engine.MY_SERVING_COLUMNS, [["whey powder", "scoop", "25"]])
    out, real_run, seen = TMP / "serv.md", engine.run, {}

    def spy(*a, **k):
        seen.update(k)
        return real_run(*a, **{**k, "reference_dir": REF})
    engine.run = spy
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            assert main(args + ["--my-servings", str(mine), "--out", str(out)]) == 0
    finally:
        engine.run = real_run
    assert seen["my_servings_csv"] == str(mine), seen
    assert "1 from your answers" in out.read_text()
    bad = write_csv("bad_servings.csv", engine.MY_SERVING_COLUMNS, [["whey", "scoop", "lots"]])
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(args + ["--my-servings", str(bad), "--out", str(out)]) != 0
    assert "My servings" in err.getvalue(), err.getvalue()


def test_report_counts_one_amount_as_one_entry():
    from gut_finder import report
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Fixture whole milk", "100.00 ml", "x"]]
    assert "**Amounts worked out for you:** 1 entry (" in report.build(run(food, []))
    assert "Amounts worked out for you" not in report.build(run(filler(28), []))


def test_report_labels_estimates_and_asks_for_what_matters():
    from gut_finder import report
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Fixture whole milk", "100.00 ml", "x"],
                         ["2026-01-04", "9:00 AM", "Snacks", "Fixture whey powder", "1.00 scoop", "x"],
                         ["2026-01-05", "9:00 AM", "Snacks", "Fixture whole milk", "1.00 serving", "x"],
                         ["2026-01-06", "9:00 AM", "Snacks", "Rice, white", "1.00 serving", "x"]]
    text = report.build(run(food, []))
    assert "**Amounts worked out for you:** 2 entries" in text, text
    assert "1 from a typical density, 1 from a typical size" in text, text
    line = next(l for l in text.splitlines() if "Worth weighing once" in l)
    assert "Fixture whole milk (serving, 1 entry)" in line and "Rice" not in line, line   # rice is 0 at any amount


def test_servings_to_check_file_can_be_filled_in_and_read_back():
    import gut_finder.__main__ as cli
    from gut_finder import report
    folder = TMP / "weigh"
    folder.mkdir(exist_ok=True)
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Fixture whole milk", "1.25 serving", "x"],
                         ["2026-01-04", "9:00 AM", "Snacks", "Fixture whole milk", "0.50 serving", "x"],
                         ["2026-01-05", "9:00 AM", "Snacks", "Fixture whole milk", "200.00 g", "x"]]
    res = run(food, [])
    name = cli.write_servings_file(cli.servings_rows(res), folder / "report.md", [])
    assert name == "servings_to_check.csv"
    path = folder / name
    assert path.stat().st_mode & 0o077 == 0, "as private as the report"
    rows = list(csv.DictReader(open(path)))
    assert rows == [{"pattern": "fixture whole milk", "unit": "serving", "grams": "", "entries": "2",
                     "also_logged_as_grams": "200"}], rows
    assert "`servings_to_check.csv`" in report.build(res, servings_file=name)

    assert engine.read_my_servings(path) == [], "a blank row isn't an answer"
    path.write_text(path.read_text().replace("serving,,2", "serving,100,2"))
    assert [(r["pattern"], r["unit"], float(r["grams"])) for r in engine.read_my_servings(path)] == [
        ("fixture whole milk", "serving", 100.0)]
    filled = engine.run(write_csv("food.csv", engine.FOOD_COLUMNS, food), write_csv("sym.csv", engine.SYMPTOM_COLUMNS, []),
                        symptoms_from="2026-01-01", symptoms_to="2026-01-28", reference_dir=REF, my_servings_csv=path)
    assert report.servings_to_check(filled) == [], "answered foods stop being asked about"

    # a later run keeps typed answers, even when this file was also the input
    assert cli.write_servings_file(cli.servings_rows(res), folder / "report.md", [])
    assert "serving,100,2" in path.read_text(), "the answer survives"
    own = str(path)
    assert cli.write_servings_file(cli.servings_rows(res), folder / "report.md", [own], own)
    assert "serving,100,2" in path.read_text(), "the answer survives being refreshed"
    assert cli.write_servings_file(cli.servings_rows(res), folder / "report.md", [own]) is None, "any other input is safe"


def test_servings_patterns_reach_one_food_only():
    import gut_finder.__main__ as cli
    two = ["Fixture whole milk drink latte small", "Fixture whole milk drink latte large"]
    food = filler(28) + [[f"2026-01-{i + 3:02d}", "9:00 AM", "Snacks", name, "1.00 serving", "x"]
                         for i, name in enumerate(two)]
    con_res = run(food, [], my_foods=[["fixture whole milk drink", "lactose", "5"]])
    patterns = [r[0] for r in cli.servings_rows(con_res)]
    assert len(patterns) == 2 and len(set(patterns)) == 2, patterns
    assert not any(p in two[1].lower() for p in patterns if "small" in p), patterns
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Fixture whole milk", "1.00 serving", "x"],
                         ["2026-01-04", "9:00 AM", "Snacks", "fixture whole milk", "1.00 serving", "x"]]
    rows = cli.servings_rows(run(food, []))
    assert [(r[0], r[3]) for r in rows] == [("fixture whole milk", 2)], rows    # one answer, not two clashing rows


# --- Matching ---------------------------------------------------------------------------------------

def test_whole_words_and_my_foods_win():
    food = [["2026-01-01", "8:00 AM", "Breakfast", f, "100.00 g", "x"] for f in
            ["Pearl Barley", "Pineapple, raw", "Pear, raw", "Apple, raw", "Test Sweet mints"]]
    con = run(food, [], my_foods=SWEET).con
    got = dict(con.execute("SELECT e.food_name, m.match_source FROM food_entries e JOIN entry_match m USING (entry_id)").fetchall())
    assert got == {"Pearl Barley": "unknown", "Pineapple, raw": "unknown", "Pear, raw": "reference",
                   "Apple, raw": "reference", "Test Sweet mints": "my_foods"}, got


def test_parent_total_is_sum_of_children_not_double():
    food = filler(1) + [["2026-01-01", "8:00 AM", "Breakfast", "Pear, raw", "100.00 g", "x"]]
    con = run(food, []).con
    rows = dict(con.execute("SELECT component, sum(grams) FROM component_intake WHERE entry_id IN "
                            "(SELECT entry_id FROM food_entries WHERE food_name = 'Pear, raw') GROUP BY 1").fetchall())
    assert {k: v for k, v in rows.items() if v} == {"sorbitol": 2.0, "polyols_total": 2.0}, rows


# --- Scoring ----------------------------------------------------------------------------------------

def test_excess_fructose_scores_on_its_own_window_and_dose():
    fruit = [["test fruit", "excess_fructose", "100"]]     # 1 g of "test fruit" = 1 g excess fructose
    def states(grams):
        food = filler(5) + [["2026-01-03", "12:00 PM", "Lunch", "Test fruit", f"{grams}.00 g", "x"]]
        con = run(food, [], my_foods=fruit).con
        return con, con.execute("""SELECT min(k), max(k), count(*) FROM tick_state
                                   WHERE component = 'excess_fructose' AND state = 'E'""").fetchone()
    con, (first, last, n) = states(15)
    day_start = 2 * 96                                     # 96 ticks a day; 3 January is 2 days in
    assert (first, last) == (day_start + 50, day_start + 63), (first, last)   # 12:30 up to 16:00
    assert n == 14 and result(con, "excess_fructose")["n_exposures"] == 1
    _, (first, last, n) = states(14)
    assert n == 0, "below the dose"
    # the new card leaves the other cards alone
    food = filler(5) + [["2026-01-03", "12:00 PM", "Lunch", "Test fruit", "15.00 g", "x"]]
    con = run(food, [], my_foods=fruit + [["test fruit", "sorbitol", "0"]]).con
    assert result(con, "sorbitol")["n_exposures"] == 0


def test_only_the_listed_symptoms_count_for_a_card_with_that_rule():
    fruit = [["test fruit", "fructans", "100"], ["test fruit", "sorbitol", "6"]]
    food = filler(28) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test fruit", "120.00 g", "x"] for d in (5, 10, 15)]
    ref = _with_fructans_dose()
    def clues(*words):
        rows = [[f"2026-01-{d:02d}", f"12:{i}0", w, "5"] for d in (5, 10, 15) for i, w in enumerate(words)]
        con = engine.run(write_csv("f.csv", engine.FOOD_COLUMNS, food), write_csv("s.csv", engine.SYMPTOM_COLUMNS, rows),
                         write_csv("m.csv", engine.MY_FOOD_COLUMNS, fruit), None, "2026-01-01", "2026-01-28",
                         reference_dir=ref).con
        return result(con, "fructans")["clues"], result(con, "sorbitol")["clues"]
    assert clues("wind") == (0, 3), "wind alone isn't evidence for this card, but still is for a card with no list"
    assert clues("nausea") == (0, 3), "only the listed symptoms count"
    assert clues("bloating") == (3, 3)
    assert clues("wind", "bloating") == (3, 3), "one episode with both counts"

    # the episode starts at the first symptom that counts: wind at 09:30 can't pull bloating at 19:00 into the window
    rows = [[f"2026-01-{d:02d}", t, w, "5"] for d in (5, 10, 15) for t, w in (("10:30", "wind"), ("13:00", "wind"), ("15:30", "wind"), ("18:00", "bloating"))]
    con = engine.run(write_csv("f.csv", engine.FOOD_COLUMNS, food), write_csv("s.csv", engine.SYMPTOM_COLUMNS, rows),
                     write_csv("m.csv", engine.MY_FOOD_COLUMNS, fruit), None, "2026-01-01", "2026-01-28",
                     reference_dir=ref).con
    assert result(con, "fructans")["clues"] == 0, "bloating at 18:00 is outside 1-8 h after 09:00"


def _with_fructans_dose():
    import shutil
    ref = TMP / "ref_fructans"
    if ref.exists():
        shutil.rmtree(ref)
    shutil.copytree(REF, ref)
    p = ref / "component_rules.csv"
    lines = [("fructans,Fructans,fodmap,,1,8,Estimated,100,g,Estimated,window,Moderate,suspect,no,0.056,Agreed\n"
              if l.startswith("fructans,") else l) for l in p.read_text().splitlines(True)]
    p.write_text("".join(lines))
    return ref


def test_coeliac_message_waits_for_the_testing_point():
    from gut_finder import report, wording
    def needed(component, score, status="scored", clues=0):
        return report.coeliac_needed([{"component": component, "score": score, "result_status": status, "clues": clues}])
    for card in ("wheat_gluten", "fructans"):
        assert needed(card, 0.50) and needed(card, 0.60) and needed(card, 0.95)
        assert not needed(card, 0.49) and not needed(card, 0.25), "nothing is raised early"
        assert not needed(card, None, "not_enough_data", 2) and not needed(card, None, "cannot_separate", 1)
        assert not needed(card, None, "no_exposures", 0)
    assert not needed("gos", 0.9) and not needed("lactose", 0.9)
    assert wording.COELIAC == ("See a GP about a coeliac test before cutting out gluten. "
                               "Keep eating gluten until the tests are finished.")
    ref = _with_fructans_dose()
    days = (3, 6, 9, 12, 15, 18, 21, 24)
    food = filler(28) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test fruit", "120.00 g", "x"] for d in days]
    s = write_csv("s.csv", engine.SYMPTOM_COLUMNS, [[f"2026-01-{d:02d}", "12:00", "bloating", "5"] for d in days])
    res = engine.run(write_csv("f.csv", engine.FOOD_COLUMNS, food), s,
                     write_csv("m.csv", engine.MY_FOOD_COLUMNS, [["test fruit", "fructans", "100"]]), None,
                     "2026-01-01", "2026-01-28", reference_dir=ref)
    assert wording.COELIAC in report.build(res)


def test_suspects_that_happen_together_are_named_and_keep_their_scores():
    from gut_finder import report, wording
    both = [["test both", "sorbitol", "100"], ["test both", "mannitol", "100"], ["test both", "lactose", "100"]]
    days = (3, 6, 9, 12, 15, 18, 21, 24)
    food = filler(31) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test both", "25.00 g", "x"] for d in days]
    symptoms = [[f"2026-01-{d:02d}", t, "bloating", "5"] for d in days for t in ("11:00", "15:30")]   # 2 episodes each
    res = run(food, symptoms, my_foods=both)
    shared = res.con.execute("SELECT component, other, shared FROM suspect_overlap ORDER BY 1, 2").fetchall()
    assert ("lactose", "sorbitol", 8) in shared and ("sorbitol", "lactose", 8) in shared, shared
    assert ("sorbitol", "mannitol", 8) in shared, "two different polyols are different suspects"
    assert result(res.con, "polyols_total")["clues"] == 8
    assert not any("polyols_total" in (r[0], r[1]) and "sorbitol" in (r[0], r[1]) for r in shared), "a polyol and its total"
    assert all(s <= result(res.con, c)["clues"] for c, _, s in shared), "the same unit as clues, so never above 100%"
    text = report.build(res)
    assert wording.TOO_FREQUENT not in text, "3.6 symptoms a week, but the detective found something"
    assert text.count(wording.TESTING_NOTE) == 1 and text.count(wording.SUSPECTS_CAN_CHANGE) == 1, text
    assert text.index(wording.TESTING_NOTE) < text.index("### "), "the note comes before the suspects"
    assert text.index(wording.SUSPECTS_CAN_CHANGE) < text.index("### "), "so does this one"
    assert wording.TOGETHER.format(other="Sorbitol") in text and wording.TOGETHER.format(other="Lactose") in text, text
    assert result(res.con, "lactose")["score"] >= 0.5 and result(res.con, "sorbitol")["score"] >= 0.5

    # the same suspects on different days aren't together
    food = filler(28) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test solo" if i % 2 else "Test both", "25.00 g", "x"]
                         for i, d in enumerate(days)]
    apart = [["test both", "sorbitol", "100"], ["test solo", "lactose", "100"]]
    text = report.build(run(food, [[f"2026-01-{d:02d}", "12:00", "bloating", "5"] for d in days], my_foods=apart))
    assert "can't tell them apart" not in text, text

    # a partner below the testing point isn't named: the report wouldn't show it
    food = filler(31) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test both", "25.00 g", "x"] for d in days] + \
                        [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test solo", "25.00 g", "x"] for d in (1, 2, 4, 7, 10, 13, 16, 19, 22, 25, 28)]
    weak = [["test both", "sorbitol", "100"], ["test both", "lactose", "100"], ["test solo", "lactose", "100"]]
    res = run(food, [[f"2026-01-{d:02d}", "12:00", "bloating", "5"] for d in days], my_foods=weak)
    assert (result(res.con, "lactose")["score"] or 0) < 0.5 <= result(res.con, "sorbitol")["score"]
    assert result(res.con, "lactose")["clues"] == 8, "lactose shares every clue, but it isn't a shown suspect"
    assert wording.TOGETHER.format(other="Lactose") not in report.build(res)


def test_expired_dose_is_not_credited():
    # Codex design review: 3 g at 00:00 + 2 g at 07:00 must not count as 5 g for a symptom at 14:00.
    food = filler(6) + [["2026-01-03", "12:00 AM", "Snacks", "Test sweet", "3.00 g", "x"],
                        ["2026-01-03", "7:00 AM", "Snacks", "Test sweet", "2.00 g", "x"]]
    con = run(food, [["2026-01-03", "14:00", "bloating", ""]], my_foods=SWEET).con
    r = result(con, "sorbitol")
    assert (r["n_exposures"], r["clues"], r["counters"]) == (1, 0, 1), r   # at dose only 07:30–08:00


def test_stacking_within_the_window():
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test sweet", "3.00 g", "x"],
                        ["2026-01-03", "11:00 AM", "Snacks", "Test sweet", "3.00 g", "x"]]
    con = run(food, [["2026-01-03", "15:00", "bloating", ""]], my_foods=SWEET).con
    r = result(con, "sorbitol")
    assert (r["n_exposures"], r["clues"]) == (1, 1), r


def test_unlogged_day_counts_neither_way():
    # dose on day 3, symptom on day 4, but day 4 has no food logged: excluded, not a clue or counter
    food = filler(6, skip={4}) + [["2026-01-03", "8:00 PM", "Snacks", "Test sweet", "6.00 g", "x"]]
    con = run(food, [["2026-01-04", "01:00", "bloating", ""]], my_foods=SWEET).con
    r = result(con, "sorbitol")
    assert r["n_exposures"] == 0 and r["eligible_episodes"] == 0, r


def test_excluded_day_counts_neither_way():
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test sweet", "6.00 g", "x"]]
    con = run(food, [["2026-01-03", "12:00", "bloating", ""]], my_foods=SWEET,
              exclude=[["2026-01-03", "stomach bug"]]).con
    assert result(con, "sorbitol")["n_exposures"] == 0


def test_unreadable_amount_is_not_a_counter_example():
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Pear, raw", "1.00 cup", "x"]]
    con = run(food, []).con
    r = result(con, "sorbitol")
    assert r["n_exposures"] == 0, r
    x = con.execute("SELECT count(*) FROM tick_state WHERE component = 'sorbitol' AND state = 'U' "
                    "AND k BETWEEN 2 * 96 + 38 AND 2 * 96 + 67").fetchone()[0]
    assert x == 30, x   # 09:30–16:45 on day 3 is unknown, not "below dose"


def test_score_matches_hand_calculation():
    rng = random.Random(1)
    food, symptoms = filler(28), []
    for d in range(3, 28, 2):   # one 6 g dose every other day at 09:00
        food.append([f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test sweet", "6.00 g", "x"])
        if d % 4 == 1:
            symptoms.append([f"2026-01-{d:02d}", "12:00", "bloating", ""])
    for d in range(4, 28, 6):   # some background symptoms on the off days
        symptoms.append([f"2026-01-{d:02d}", f"{rng.randint(10, 20)}:00", "bloating", ""])
    con = run(food, symptoms, my_foods=SWEET).con
    r = result(con, "sorbitol")
    b = (r["k_blocks"] + 1) / (r["n_blocks"] + 2)
    logit = math.log(0.023 / 0.977) + r["clues"] * math.log(0.7 / b) + r["counters"] * math.log(0.3 / (1 - b))
    expected = min(1 / (1 + math.exp(-logit)), 0.95)
    assert r["clues"] > 0 and r["counters"] > 0, r
    assert abs(r["score"] - expected) < 1e-9, (r["score"], expected)


def source_of(con, food):
    return con.execute("""SELECT DISTINCT m.match_source, m.ref_food FROM entry_match m JOIN food_entries e USING (entry_id)
                          WHERE e.food_name = ?""", [food]).fetchall()


def test_a_wrong_match_can_be_corrected():
    from gut_finder import report, wording
    food = filler(28)
    milk, milk_id = run(food, []).con.execute(
        "SELECT name, food FROM reference_foods WHERE name = 'Test milk, whole'").fetchone()
    res = run(food, [], my_matches=[["RICE, WHITE", milk, "old match"]])
    assert source_of(res.con, "Rice, white") == [("corrected", milk_id)], "whatever the capitals"
    assert wording.MATCHES_CORRECTED.format(n=56, entries="entries") in report.build(res)
    assert source_of(run(food, [], my_matches=[["Rice, white", milk_id, ""]]).con, "Rice, white") == [("corrected", milk_id)]
    assert source_of(run(food, [], my_matches=[["Rice, white", "unknown", ""]]).con, "Rice, white") == [("unknown", None)]
    unanswered = run(food, [], my_matches=[["Rice, white", "", ""]])
    assert source_of(unanswered.con, "Rice, white")[0][0] != "corrected", "a blank row isn't answered yet"
    for bad, says in ((["Rice, white", "No such food", ""], "--find-food"),
                      (["", milk, ""], "your_food")):
        try:
            run(food, [], my_matches=[bad])
        except engine.InputError as e:
            assert says in str(e), e
        else:
            raise AssertionError(bad)
    try:
        run(food, [], my_matches=[["Rice, white", milk, ""], ["rice, WHITE", milk, ""]])
    except engine.InputError as e:
        assert "already listed" in str(e)
    else:
        raise AssertionError("two answers for one food")


def test_a_corrected_food_is_never_sent_to_the_ai():
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Mystery thing", "100.00 g", "x"]]
    asked = []
    f = write_csv("food.csv", engine.FOOD_COLUMNS, food)
    s = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
    mm = write_csv("mm.csv", ["your_food", "correct_food"], [["mystery thing", "unknown"]])
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-28", reference_dir=REF, online_matching=True,
                     match_cache=TMP / "corrected_cache.json", ask=lambda system, user: asked.append(user),
                     my_matches_csv=mm)
    assert not asked and source_of(res.con, "Mystery thing") == [("unknown", None)]


def test_find_food_lists_table_foods():
    found = engine.find_foods("milk", reference_dir=REF)
    assert found and all("milk" in name.lower() for name, _ in found[:3]), found


def test_a_checked_danger_sign_lets_the_analysis_run_and_a_new_one_stops_it():
    from gut_finder import report, wording
    food = filler(28)
    sign = ["2026-01-05", "12:00", "blood in stool", ""]
    line = ",".join(sign)
    symptoms = [sign, ["2026-01-06", "12:00", "bloating", ""]]
    stopped = run(food, symptoms)
    assert stopped.red_flags and stopped.con is None
    assert line in engine.flagged_entries(TMP / "symptoms.csv", stopped.red_flags)
    text = report.build(stopped, signs_file="signs.csv")
    assert wording.SIGN_CHECKED_HOW.format(file="signs.csv") in text and "Suspects" not in text
    assert run(food, symptoms, checked=[[line, ""]]).red_flags, "a blank answer isn't a confirmation"
    assert run(food, symptoms, checked=[[line, "no"]]).red_flags
    assert run(food, symptoms, checked=[["2026-01-04,12:00,blood in stool,", "yes"]]).red_flags, "another entry"
    res = run(food, symptoms, checked=[[line, "Yes"]])
    assert not res.red_flags and res.con.execute("SELECT count(*) FROM symptom_episodes").fetchone()[0] == 1
    later = symptoms + [["2026-01-09", "12:00", "blood in stool", ""]]
    assert run(food, later, checked=[[line, "yes"]]).red_flags, "only that entry is confirmed"


def test_a_broken_checked_signs_file_cant_hide_the_advice():
    food = filler(28)
    f = write_csv("food.csv", engine.FOOD_COLUMNS, food)
    s = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [["2026-01-05", "12:00", "blood in stool", ""]])
    broken = write_csv("broken.csv", ["nothing"], [["x"]])
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-28", reference_dir=REF, checked_signs_csv=broken)
    assert res.red_flags


def test_only_a_whole_entry_on_one_line_can_be_confirmed():
    header = ",".join(engine.SYMPTOM_COLUMNS)
    vocab = engine.load_vocabulary(REF)
    path = TMP / "multi.csv"
    path.write_text(header + '\n2026-01-05,12:00,"blood in stool;\nvomiting",\n')
    assert engine.scan_for_red_flags(path, vocab, frozenset({'2026-01-05,12:00,"blood in stool;'})), "half an entry"
    path.write_text(header + '\n2026-01-04,12:00,"bloating\x0b",\n2026-01-05,12:00,blood in stool,\n2026-01-06,12:00,vomiting blood,\n')
    checked = frozenset({"2026-01-05,12:00,blood in stool,"})
    flags = engine.scan_for_red_flags(path, vocab, checked)
    assert {c for _, c in flags} >= {"vomiting_blood"}, flags
    assert engine._checked_lines(path.read_text(), checked) == {3}, "the same line numbers as the CSV reader"
    path.write_text(header + "\n2026-01-05,12:00,blo\nod,\n")
    assert engine.scan_for_red_flags(path, vocab), "a sign split across lines still gives its advice"
    path.write_text(header + "\n2026-01-05,12:00,blood in stool,\n2026-01-06,12:00,wei\nght loss,\n")
    assert "weight_loss" in {c for _, c in engine.scan_for_red_flags(path, vocab)}, "even alongside another sign"


def test_a_food_name_cant_become_a_spreadsheet_formula():
    for name in ("=1+1", "+1", "-1", "@x"):
        assert engine.spreadsheet_safe(name) == "'" + name
        assert engine.from_spreadsheet(engine.spreadsheet_safe(name)) == name
    assert engine.spreadsheet_safe("Milk") == "Milk" and engine.from_spreadsheet("'tis the season") == "'tis the season"


def test_a_correction_reaches_the_food_whatever_its_letters():
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "İçim Yoğurt", "100.00 g", "x"]]
    res = run(food, [], my_matches=[["İçim Yoğurt", "unknown", ""]])
    assert source_of(res.con, "İçim Yoğurt") == [("unknown", None)]
    assert res.con.execute("SELECT count(*) FROM my_matches WHERE food_key = (SELECT lower(food_name) FROM food_entries "
                           "WHERE food_name = 'İçim Yoğurt')").fetchone()[0] == 1


def test_consent_never_writes_through_a_link():
    import builtins
    import contextlib
    import io
    import gut_finder.__main__ as cli
    saved_file, saved_stdin, saved_input = cli.CONSENT_FILE, sys.stdin, builtins.input
    target = TMP / "precious.csv"
    target.write_text("keep me")
    cli.CONSENT_FILE = TMP / "linked_consent.json"
    cli.CONSENT_FILE.symlink_to(target)

    class Terminal(io.StringIO):
        def isatty(self):
            return True
    try:
        sys.stdin, builtins.input = Terminal(), lambda prompt: "I agree"
        with contextlib.redirect_stdout(io.StringIO()):
            cli.agree_once()
        assert target.read_text() == "keep me" and cli.has_agreed() and not cli.CONSENT_FILE.is_symlink()
    finally:
        cli.CONSENT_FILE, sys.stdin, builtins.input = saved_file, saved_stdin, saved_input


def test_consent_is_asked_once_and_no_agreement_means_no_use():
    import builtins
    import contextlib
    import io
    import gut_finder.__main__ as cli
    from gut_finder import wording
    saved_file, saved_stdin, saved_input = cli.CONSENT_FILE, sys.stdin, builtins.input
    cli.CONSENT_FILE = TMP / "consent_home" / "consent.json"

    class Terminal(io.StringIO):
        def isatty(self):
            return True
    try:
        with contextlib.redirect_stdout(io.StringIO()) as out:
            sys.stdin = io.StringIO()
            try:
                cli.agree_once()
            except engine.InputError as e:
                assert str(e) == wording.CONSENT_NEEDS_TERMINAL
            else:
                raise AssertionError("agreed with nobody there")
            assert wording.LIMITATION in out.getvalue() and wording.BEST_WHEN in out.getvalue()
            sys.stdin = Terminal()
            builtins.input = lambda prompt: "no thanks"
            try:
                cli.agree_once()
            except engine.InputError as e:
                assert str(e) == wording.CONSENT_REFUSED
            else:
                raise AssertionError("ran without agreement")
            assert not cli.CONSENT_FILE.exists()
            builtins.input = lambda prompt: " I AGREE "
            cli.agree_once()
        assert cli.has_agreed() and cli.CONSENT_FILE.stat().st_mode & 0o077 == 0
        builtins.input = lambda prompt: 1 / 0     # never asked again
        cli.agree_once()
        cli.CONSENT_FILE.write_text('{"version": 0}')
        assert not cli.has_agreed(), "new terms ask again"
    finally:
        cli.CONSENT_FILE, sys.stdin, builtins.input = saved_file, saved_stdin, saved_input


def test_fill_in_files_keep_answers_already_typed():
    import gut_finder.__main__ as cli
    out = TMP / "fill" / "report.md"
    columns = ["your_food", "correct_food", "matched_to", "entries"]
    assert cli.write_fill_in_file("m.csv", columns, [["a", "", "x", 2], ["b", "", "y", 1]], out, [], 100) == "m.csv"
    path = out.with_name("m.csv")
    path.write_text(path.read_text().replace("a,,x,2", "a,right food,x,2"))
    cli.write_fill_in_file("m.csv", columns, [["a", "", "z", 9], ["c", "", "y", 1]], out, [], 100)
    rows = list(csv.DictReader(open(path)))
    assert [(r["your_food"], r["correct_food"]) for r in rows] == [("a", "right food"), ("c", "")], rows
    assert path.stat().st_mode & 0o077 == 0, "private"
    own = str(path)
    assert cli.write_fill_in_file("m.csv", columns, [["d", "", "y", 1]], out, [own], 100, own) == "m.csv", "used as an input"
    assert cli.write_fill_in_file("m.csv", columns, [["d", "", "y", 1]], out, [own], 100) is None, "a symptoms file with this name"
    assert cli.write_fill_in_file("m.csv", columns, [["d", "", "y", 1]], out, [own, own], 100, own) is None, "given twice"
    path.write_bytes(b"\xef\xbb\xbf" + path.read_bytes())      # saved by a spreadsheet
    cli.write_fill_in_file("m.csv", columns, [["e", "", "y", 1]], out, [], 100)
    rows = list(csv.DictReader(open(path)))
    assert ("a", "right food") in [(r["your_food"], r["correct_food"]) for r in rows], rows


def test_every_report_says_what_the_tool_cant_do():
    from gut_finder import report, wording
    text = report.build(run(filler(28), []))
    assert wording.LIMITATION in text and wording.LOW_SCORE in text
    assert "lactose and polyols (sugar alcohols)" not in wording.LIMITATION
    assert all(w in wording.LIMITATION for w in ("fructans", "GOS", "excess fructose", "wheat and gluten", "coffee"))


def daily_food_and_symptoms(days, symptom_days, food="Test sweet"):
    """Food at 9:00 every day and symptoms late at night, outside its window, so nothing can be separated."""
    log = filler(days) + [[f"2026-01-{d:02d}", "9:00 AM", "Snacks", food, "6.00 g", "x"] for d in range(1, days + 1)]
    return log, [[f"2026-01-{d:02d}", "23:30", "bloating", "5"] for d in symptom_days]


def test_too_frequent_to_find_a_pattern_moves_to_the_second_pathway():
    from gut_finder import report, wording
    food, symptoms = daily_food_and_symptoms(28, range(1, 29))
    text = report.build(run(food, symptoms, my_foods=SWEET))
    assert wording.TOO_FREQUENT in text and wording.TOO_FREQUENT_NEXT in text, text
    assert wording.TOO_FREQUENT_STAPLES.format(foods="Test sweet") in text, text
    assert "Nothing reaches the testing point" not in text and wording.TESTING_NOTE not in text
    assert "Rice" not in text.split(wording.TOO_FREQUENT)[1].split("##")[0], "a food with nothing the tool checks"
    assert wording.COELIAC not in text, "no wheat here, so nothing about coeliac disease"


def test_the_second_pathway_needs_frequent_symptoms_and_enough_days():
    from gut_finder import report, wording
    food, symptoms = daily_food_and_symptoms(28, (1, 15))                  # once every 2 weeks
    text = report.build(run(food, symptoms, my_foods=SWEET))
    assert wording.TOO_FREQUENT not in text and "Nothing reaches the testing point" in text, text
    food, symptoms = daily_food_and_symptoms(28, range(2, 29, 3))          # 9 in 28 days: under 3 a week
    assert wording.TOO_FREQUENT not in report.build(run(food, symptoms, my_foods=SWEET))
    food, symptoms = daily_food_and_symptoms(28, [d for d in range(1, 29) if d % 7 in (1, 3, 5)])   # exactly 3 a week
    assert wording.TOO_FREQUENT in report.build(run(food, symptoms, my_foods=SWEET))
    food, symptoms = daily_food_and_symptoms(10, range(1, 11))             # one bad stretch, too few days
    assert wording.TOO_FREQUENT not in report.build(run(food, symptoms, my_foods=SWEET))


def test_wheat_on_most_days_shows_the_coeliac_message_on_the_second_pathway():
    from gut_finder import report, wording
    food, symptoms = daily_food_and_symptoms(28, range(1, 29), food="Test loaf")
    loaf = [["test loaf", "wheat_gluten", "100"]]
    text = report.build(run(food, symptoms, my_foods=loaf))
    assert wording.TOO_FREQUENT in text and wording.COELIAC in text, text
    food, symptoms = daily_food_and_symptoms(28, (1, 15), food="Test loaf")
    assert wording.COELIAC not in report.build(run(food, symptoms, my_foods=loaf)), "rare symptoms: nothing raised"


def test_clear_time_is_counted_in_hours_whatever_the_window():
    food = filler(28)
    symptoms = [[f"2026-01-{d:02d}", "12:00", "bloating", ""] for d in (2, 9, 16)]
    for d in (2, 9, 16):
        food.append([f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Test sweet", "6.00 g", "x"])
    con = run(food, symptoms, my_foods=SWEET).con
    for name, blocks, hours, start, end in con.execute("""
            SELECT r.component, r.n_blocks, r.clear_hours, c.window_start_h, c.window_end_h
            FROM component_results r JOIN component_rules c USING (component) WHERE r.n_blocks > 0""").fetchall():
        assert abs(float(hours) - blocks * float(end - start)) < 1e-9, name
    clear = float(result(con, "sorbitol")["clear_hours"])
    assert clear > 0

    def with_minimum(hours):
        folder = TMP / f"ref_{hours}"
        if folder.exists():
            shutil.rmtree(folder)
        shutil.copytree(REF, folder)
        rules = folder / "global_rules.csv"
        rules.write_text(rules.read_text().replace("min_clear_hours,150,", f"min_clear_hours,{hours},"))
        return result(run(food, symptoms, my_foods=SWEET, ref=folder).con, "sorbitol")["result_status"]
    assert with_minimum(clear) == "scored"
    assert with_minimum(clear + 1) == "not_enough_data"


def test_import_order_does_not_change_results():
    food, symptoms = synthetic.generate(seed=5, days=40)
    a = run(food, symptoms).con.execute("SELECT component, score FROM component_results ORDER BY 1").fetchall()
    random.Random(0).shuffle(food)
    b = run(food, symptoms).con.execute("SELECT component, score FROM component_results ORDER BY 1").fetchall()
    assert a == b, (a, b)


def test_red_flag_stops_the_analysis():
    res = run(filler(3), [["2026-01-02", "10:00", "Blood in stool", ""]])
    assert res.red_flags == [(2, "blood_in_stool")], res.red_flags
    assert res.con is None   # nothing loaded, nothing scored


def test_bad_input_is_rejected_clearly():
    for symptoms, why in ([["2026-13-01", "10:00", "x", ""]], "bad date"),\
                         ([["2026-01-01", "3pm", "x", ""]], "bad time"),\
                         ([["2026-01-01", "10:00", "x", "11"]], "severity out of range"):
        try:
            run(filler(2), symptoms)
        except engine.InputError:
            continue
        raise AssertionError(f"not rejected: {why}")


# --- Codex review fixes -----------------------------------------------------------------------------

def test_window_edges_never_credit_too_early():
    # food 09:14, symptom 09:31: only 17 min later, inside the 30-min minimum. Must not be a clue.
    food = filler(6) + [["2026-01-03", "9:14 AM", "Snacks", "Test sweet", "6.00 g", "x"]]
    con = run(food, [["2026-01-03", "09:31", "bloating", ""]], my_foods=SWEET).con
    r = result(con, "sorbitol")
    assert (r["n_exposures"], r["clues"]) == (1, 0), r


def test_unrecognised_food_is_unknown_not_zero():
    # a food the reference doesn't know could contain anything: on its own, that time counts neither way
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Mystery bar", "50.00 g", "x"]]
    con = run(food, [["2026-01-03", "12:00", "bloating", ""]]).con
    r = result(con, "sorbitol")
    assert r["n_exposures"] == 0 and r["eligible_episodes"] == 0, r
    assert _ticks(con, "sorbitol", "U", 3, 38, 67) == 30, r
    assert r["checked_share"] < result(run(filler(6), []).con, "sorbitol")["checked_share"], r


def test_parent_is_at_least_the_known_children_when_one_is_missing():
    # "test sweet" (my foods) only gives sorbitol, so all polyols combined is "at least" its sorbitol
    food = filler(2) + [["2026-01-01", "8:00 AM", "Snacks", "Test sweet", "30.00 g", "x"]]
    con = run(food, [], my_foods=SWEET).con
    rows = con.execute("SELECT ci.grams, ci.at_least, ci.source FROM component_intake ci JOIN food_entries e "
                       "USING (entry_id) WHERE e.food_name = 'Test sweet' AND ci.component = 'polyols_total'"
                       ).fetchall()
    assert rows == [(30.0, True, "at least: sum of sorbitol")], rows
    pear = con.execute("SELECT ci.at_least FROM component_intake ci JOIN food_entries e USING (entry_id) "
                       "WHERE e.food_name = 'Rice, white' AND ci.component = 'polyols_total' LIMIT 1").fetchone()
    assert pear == (False,), pear                      # every child known: an exact total


def _ticks(con, component, state, day, start_k, end_k):
    return con.execute("SELECT count(*) FROM tick_state WHERE component = ? AND state = ? AND k BETWEEN ? AND ?",
                       [component, state, (day - 1) * 96 + start_k, (day - 1) * 96 + end_k]).fetchone()[0]


def test_at_least_rule_known_part_reaching_the_dose_is_an_exposure():
    # 25 g sorbitol plus a food nothing is known about: all polyols combined is at least 25 g, above its 20 g
    # dose, so it is an exposure. The same holds for sorbitol on its own card (at least 25 g, dose 5 g).
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test sweet", "25.00 g", "x"],
                        ["2026-01-03", "9:00 AM", "Snacks", "Mystery bar", "50.00 g", "x"]]
    sym = [["2026-01-03", "12:00", "bloating", ""]]
    con = run(food, sym, my_foods=SWEET).con
    assert _ticks(con, "polyols_total", "E", 3, 38, 67) == 30          # 09:30–16:45: the whole window
    total = result(con, "polyols_total")
    assert total["n_exposures"] == 1 and total["clues"] == 1, total
    assert _ticks(con, "sorbitol", "E", 3, 38, 67) == 30
    assert result(con, "sorbitol")["clues"] == 1


def test_at_least_rule_below_the_dose_stays_unknown():
    # 10 g known, the rest unknown: it could be under or over 20 g, so that time counts neither way
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test sweet", "10.00 g", "x"]]
    con = run(food, [], my_foods=SWEET).con
    assert _ticks(con, "polyols_total", "U", 3, 38, 67) == 30
    assert _ticks(con, "polyols_total", "B", 3, 38, 67) == 0
    assert result(con, "polyols_total")["n_exposures"] == 0


MILK = [["test milk", "lactose", "100"], ["rice", "lactose", "0"]]   # exact lactose doses; rice is a known 0


def test_known_dose_beside_an_unknown_food_is_a_counter_example():
    # 15 g of known lactose and an unknown breakfast is "at least 15 g". The unknown food can
    # only add lactose, so no symptom in the known dose's window counts against lactose.
    food = filler(6) + [["2026-01-03", "8:00 AM", "Breakfast", "Mystery bar", "50.00 g", "x"],
                        ["2026-01-03", "12:00 PM", "Lunch", "Test milk", "15.00 g", "x"]]
    con = run(food, [], my_foods=MILK).con
    assert _ticks(con, "lactose", "U", 3, 34, 49) == 16        # 08:30–12:15: the unknown food alone
    assert _ticks(con, "lactose", "E", 3, 50, 79) == 30        # 12:30–19:45: above the dose, unknown or not
    r = result(con, "lactose")
    assert (r["n_exposures"], r["clues"], r["counters"]) == (1, 0, 1), r


def test_known_dose_beside_an_unknown_food_with_a_symptom_is_a_clue():
    food = filler(6) + [["2026-01-03", "8:00 AM", "Breakfast", "Mystery bar", "50.00 g", "x"],
                        ["2026-01-03", "12:00 PM", "Lunch", "Test milk", "15.00 g", "x"]]
    r = result(run(food, [["2026-01-03", "14:00", "bloating", ""]], my_foods=MILK).con, "lactose")
    assert (r["n_exposures"], r["clues"], r["counters"]) == (1, 1, 0), r


def test_known_dose_below_the_card_stays_unknown_beside_an_unknown_food():
    # 6 g known plus an unknown food could be under or over 12 g, so a symptom there counts neither way
    food = filler(6) + [["2026-01-03", "12:00 PM", "Lunch", "Mystery bar", "50.00 g", "x"],
                        ["2026-01-03", "12:00 PM", "Lunch", "Test milk", "6.00 g", "x"]]
    con = run(food, [["2026-01-03", "14:00", "bloating", ""]], my_foods=MILK).con
    assert _ticks(con, "lactose", "U", 3, 50, 79) == 30
    r = result(con, "lactose")
    assert r["n_exposures"] == 0 and r["eligible_episodes"] == 0, r


def test_unknown_on_one_side_doesnt_rescue_a_window_cut_by_an_unusable_day():
    # day 4 has no food: the window of an 8 pm dose runs into it, so the stretch stays out, whatever is beside it
    food = filler(6, skip={4}) + [["2026-01-03", "4:00 PM", "Snacks", "Mystery bar", "50.00 g", "x"],
                                  ["2026-01-03", "8:00 PM", "Snacks", "Test milk", "15.00 g", "x"]]
    con = run(food, [["2026-01-03", "21:00", "bloating", ""]], my_foods=MILK).con
    assert _ticks(con, "lactose", "X", 4, 0, 95) == 96
    assert result(con, "lactose")["n_exposures"] == 0


def test_time_lost_to_the_look_back_stays_x():
    # the look-back before the log, and a day that can't be used, are never "unknown food" time
    food = filler(6, skip={4})
    con = run(food, [], my_foods=MILK).con
    assert _ticks(con, "lactose", "X", 1, 0, 31) == 32
    assert _ticks(con, "lactose", "U", 4, 0, 95) == 0


def test_checked_time_includes_a_known_dose_beside_an_unknown_food():
    food = filler(6) + [["2026-01-03", "12:00 PM", "Lunch", "Mystery bar", "50.00 g", "x"]]
    both = food + [["2026-01-03", "12:00 PM", "Lunch", "Test milk", "15.00 g", "x"]]
    assert result(run(both, [], my_foods=MILK).con, "lactose")["checked_share"] > \
        result(run(food, [], my_foods=MILK).con, "lactose")["checked_share"]


def test_matched_all_episodes_counts_episodes_in_unknown_time_too():
    # Codex review, D19: "matched all 3 recorded symptom episodes" must not hide a fourth in unknown time
    from gut_finder import report
    food, symptoms = filler(28), []
    for d in (5, 12, 19):
        day = f"2026-01-{d:02d}"
        food += [[day, "8:00 AM", "Breakfast", "Mystery bar", "50.00 g", "x"],
                 [day, "12:00 PM", "Lunch", "Test milk", "15.00 g", "x"]]
        symptoms.append([day, "14:00", "bloating", ""])
    sentence = "matched all 3 recorded symptom episodes"
    assert sentence in report.build(run(food, symptoms, my_foods=MILK))
    food.append(["2026-01-24", "8:00 AM", "Breakfast", "Mystery bar", "50.00 g", "x"])
    res = run(food, symptoms + [["2026-01-24", "10:00", "bloating", ""]], my_foods=MILK)
    r = result(res.con, "lactose")
    assert (r["clues"], r["counters"], r["eligible_episodes"]) == (3, 0, 3), r
    assert sentence not in report.build(res)


# --- Own table (D24) ----------------------------------------------------------------------------------

def test_a_dish_from_the_own_table_gives_grams_and_the_report_says_its_typical():
    from gut_finder import report
    food = filler(2) + [["2026-01-01", "8:00 AM", "Breakfast", "Test Milk Bake", "200.00 g", "x"]]
    res = run(food, [])
    got = res.con.execute("SELECT grams FROM component_intake WHERE component = 'lactose' AND source LIKE 'Own table%'"
                          ).fetchall()
    assert got == [(4.8,)], got
    assert "**Typical recipes:** 1 entry uses a typical recipe" in report.build(res), report.build(res)
    # a dish can give figures for several components: it is still one entry
    res.con.execute("""INSERT INTO component_intake SELECT * REPLACE ('sorbitol' AS component) FROM component_intake
                       WHERE component = 'lactose' AND source LIKE 'Own table%'""")
    assert "**Typical recipes:** 1 entry uses a typical recipe" in report.build(res), report.build(res)


def test_a_plain_food_from_the_own_table_is_not_called_a_recipe():
    from gut_finder import report
    res = run(filler(2) + [["2026-01-01", "8:00 AM", "Breakfast", "Test Creatine", "5.00 g", "x"]], [])
    assert res.con.execute("SELECT count(*) FROM component_intake WHERE component = 'lactose' AND grams = 0 "
                           "AND source = 'Own table: R24'").fetchone()[0] == 1
    assert "Typical recipes" not in report.build(res)


# --- "At most" rule (D23): an unknown food can't hold more than it weighs -----------------------------------

def state_at_10am(entries, my_foods=MILK, component="lactose"):
    """The state of 10:00 on 2 Jan, after these foods. Rice around them keeps the days usable."""
    rows = filler(3) + [["2026-01-02", t, g, n, a, "x"] for t, g, n, a in entries]
    con = run(rows, [], my_foods=my_foods).con
    k = (24 + 10) * 4
    return con.execute("SELECT state FROM tick_state WHERE component = ? AND k = ?", [component, k]).fetchone()[0]


BREAKFAST = ("8:00 AM", "Breakfast")


def test_at_most_a_small_unknown_food_cant_reach_the_dose():
    assert state_at_10am([(*BREAKFAST, "Test Milk", "5.00 g"), (*BREAKFAST, "Mystery Powder", "5.00 g")]) == "B"


def test_at_most_a_food_heavy_enough_to_reach_the_dose_stays_unknown():
    assert state_at_10am([(*BREAKFAST, "Test Milk", "5.00 g"), (*BREAKFAST, "Mystery Powder", "8.00 g")]) == "U"


def test_at_most_exactly_the_dose_stays_unknown():
    assert state_at_10am([(*BREAKFAST, "Test Milk", "5.00 g"), (*BREAKFAST, "Mystery Powder", "7.00 g")]) == "U"


def test_at_most_adds_up_every_unknown_food():
    assert state_at_10am([(*BREAKFAST, "Mystery Powder", "7.00 g"), (*BREAKFAST, "Mystery Bar", "7.00 g")]) == "U"
    assert state_at_10am([(*BREAKFAST, "Mystery Powder", "5.00 g"), (*BREAKFAST, "Mystery Bar", "5.00 g")]) == "B"


def test_at_most_needs_every_weight():
    assert state_at_10am([(*BREAKFAST, "Mystery Powder", "5.00 g"), (*BREAKFAST, "Mystery Bar", "1.00 serving")]) == "U"


def test_at_most_needs_a_time():
    # with no time the food could sit anywhere in the day, so its whole day stays unknown
    assert state_at_10am([("", "Snacks", "Mystery Powder", "1.00 g")]) == "U"


def test_at_most_works_on_each_card_and_counts_as_checked():
    assert state_at_10am([(*BREAKFAST, "Test Sweet", "3.00 g"), (*BREAKFAST, "Mystery Powder", "1.00 g")],
                         SWEET, "sorbitol") == "B"
    assert state_at_10am([(*BREAKFAST, "Test Sweet", "3.00 g"), (*BREAKFAST, "Mystery Powder", "2.00 g")],
                         SWEET, "sorbitol") == "U"


def test_at_most_a_food_with_a_known_part_is_not_counted_short():
    # 15 g of known polyols, plus a 10 g food with 1 g of sorbitol and the rest unknown: at most 25 g, over the 20 g
    # dose. On its known part alone that's 16 g, so losing the food's unknown span would wrongly clear the time.
    mine = [["test total", "polyols_total", "100"], ["test part", "sorbitol", "10"]]
    assert state_at_10am([(*BREAKFAST, "Test Total", "15.00 g"), (*BREAKFAST, "Test Part", "10.00 g")],
                         mine, "polyols_total") == "U"


def test_known_limit_two_small_doses_beside_an_unknown_food():
    # EDGE-CASES E14: 7 g + 7 g only reach the dose where their windows overlap (14:30–16:45). A symptom at 17:30
    # sits in unknown time, so the stretch counts as a counter-example though the unknown food may have kept
    # the total high. A symptom inside the overlap is a clue as usual.
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Test milk", "7.00 g", "x"],
                        ["2026-01-03", "1:00 PM", "Snacks", "Mystery bar", "50.00 g", "x"],
                        ["2026-01-03", "2:00 PM", "Snacks", "Test milk", "7.00 g", "x"]]
    after = result(run(food, [["2026-01-03", "17:30", "bloating", ""]], my_foods=MILK).con, "lactose")
    assert (after["n_exposures"], after["clues"], after["counters"]) == (1, 0, 1), after
    inside = result(run(food, [["2026-01-03", "15:00", "bloating", ""]], my_foods=MILK).con, "lactose")
    assert (inside["n_exposures"], inside["clues"], inside["counters"]) == (1, 1, 0), inside


def test_longer_stretch_never_reverses_evidence():
    # many repeated doses make one long exposure; a symptom inside it must still add points
    food = filler(10)
    for h in range(8, 22, 2):   # 6 g every 2 hours from 08:00 to 20:00 → one long stretch
        food.append(["2026-01-05", f"{h % 12 or 12}:00 {'AM' if h < 12 else 'PM'}", "Snacks", "Test sweet", "6.00 g", "x"])
    with_sym = result(run(food, [["2026-01-05", "15:00", "bloating", ""]], my_foods=SWEET).con, "sorbitol")
    without = result(run(food, [], my_foods=SWEET).con, "sorbitol")
    assert with_sym["clues"] == 1 and without["counters"] == 1
    assert with_sym["score"] > 0.023 > without["score"], (with_sym["score"], without["score"])


def test_weak_card_is_capped():
    food = filler(28)
    symptoms = []
    for d in range(3, 28):
        food.append([f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Maltitol sweets", "20.00 g", "x"])
        symptoms.append([f"2026-01-{d:02d}", "12:00", "bloating", ""])
    con = run(food, symptoms, my_foods=[["maltitol sweets", c, "100" if c == "maltitol" else "0"]
                                        for c in ("sorbitol", "mannitol", "maltitol", "isomalt", "xylitol", "erythritol")]).con
    assert abs(result(con, "maltitol")["score"] - 0.60) < 1e-9


def test_partly_logged_days_still_count():
    # a meal that isn't logged counts as nothing eaten; only fully unlogged days are left out
    food = filler(10) + [[f"2026-01-{d:02d}", "8:00 AM", "Breakfast", "Oats, rolled", "50.00 g", "x"]
                         for d in range(1, 11)]
    food = [r for r in food if not (r[0] == "2026-01-05" and r[3] != "Oats, rolled")]   # day 5: breakfast only
    food = [r for r in food if r[0] != "2026-01-07"]                                    # day 7: nothing logged
    con = run(food, []).con
    got = dict(con.execute("SELECT strftime(day, '%d'), usable FROM day_coverage WHERE day IN "
                           "('2026-01-05', '2026-01-07')").fetchall())
    assert got == {"05": True, "07": False}, got


def test_zero_value_food_is_known_at_any_amount():
    # "1 cup" of a food with no sorbitol is still 0 g sorbitol, so it mustn't block time
    food = filler(6) + [["2026-01-03", "9:00 AM", "Snacks", "Rice, white", "1.00 cup", "x"]]
    con = run(food, []).con
    grams = con.execute("SELECT ci.grams FROM component_intake ci JOIN food_entries e USING (entry_id) "
                        "WHERE e.unit = 'cup' AND ci.component = 'sorbitol'").fetchone()[0]
    assert grams == 0, grams
    baseline = result(run(filler(6), []).con, "sorbitol")["checked_share"]   # only the first 8 h can't be checked
    assert result(con, "sorbitol")["checked_share"] == baseline, (result(con, "sorbitol"), baseline)


def test_symptom_words_and_red_flag_variants():
    for text in ("black stools", "Weight-loss", "ｂｌｏｏｄ", "bl\u200bood", "bleedin", "BLOOD!"):
        res = run(filler(3), [["2026-01-02", "10:00", text, ""]])
        assert res.red_flags, f"missed red flag: {text!r}"
    res = run(filler(3), [["2026-01-02", "10:00", "Bloated; wind", ""]])
    assert not res.red_flags
    assert sorted(r[0] for r in res.con.execute("SELECT symptom FROM symptom_onsets").fetchall()) == ["bloating", "gas"]
    try:
        run(filler(3), [["2026-01-02", "10:00", "felt weird", ""]])
    except engine.InputError:
        pass
    else:
        raise AssertionError("an unrecognised symptom must stop the run")


def test_safety_config_must_be_complete():
    import shutil
    bad = TMP / "bad_ref"
    shutil.copytree(REF, bad, dirs_exist_ok=True)
    (bad / "symptom_words.csv").write_text("phrase,code,red_flag,source\nbloating,bloating,no,\n")
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(2))
    s = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
    try:
        engine.run(f, s, symptoms_from="2026-01-01", symptoms_to="2026-01-02", reference_dir=bad)
    except engine.ConfigError:
        return
    raise AssertionError("a red-flag list without the required codes must stop the run")


def test_strict_csv_reading():
    dup = TMP / "dup.csv"
    dup.write_text("date,time,symptom,symptom\n2026-01-01,10:00,pain,bloating\n")
    wide = TMP / "wide.csv"
    wide.write_text("date,time,symptom,severity\n2026-01-01,10:00,bloating,3,extra\n")
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(2))
    for path in (dup, wide):
        try:
            engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-02", reference_dir=REF)
        except engine.InputError:
            continue
        raise AssertionError(f"not rejected: {path.name}")


def test_duplicate_my_foods_rows_rejected():
    try:
        run(filler(2), [], my_foods=SWEET + SWEET)
    except engine.InputError:
        return
    raise AssertionError("duplicate label rows would double the dose")


def test_symptom_period_must_make_sense():
    for a, b in (("2026-01-05", "2026-01-01"), ("2027-01-01", "2027-02-01")):
        try:
            run(filler(5), [], symptoms_from=a, symptoms_to=b)
        except engine.InputError:
            continue
        raise AssertionError(f"accepted symptom period {a} to {b}")


def test_report_builds_and_escapes():
    from gut_finder import report, wording
    food = filler(28) + [["2026-01-03", "9:00 AM", "Snacks", "Mystery | *bold* <b>\nfake", "100.00 g", "x"]]
    text = report.build(run(food, [["2026-01-03", "12:00", "bloating", ""]]))
    assert "<b>" not in text and "\\|" in text and "fake" in text and "\nfake" not in text
    assert wording.TESTING_NOTE not in text, "no suspects, so nothing to explain"
    assert wording.SUSPECTS_CAN_CHANGE not in text


def test_round2_safety_gaps():
    # Codex security round 2: a symbol-only segment, a non-ASCII digit, an edited red-flag row
    for symptoms in ([["2026-01-02", "10:00", "bloating; \U0001fa78", ""]],
                     [["2026-01-02", "10:00", "bloating", "\u00b2"]]):
        try:
            run(filler(3), symptoms)
        except engine.InputError:
            continue
        raise AssertionError(f"accepted: {symptoms}")
    import shutil
    for edit in (("blood,blood_in_stool,yes", "blood,blood_in_stool,no"),
                 ("bloating,bloating,no,", "bloating,bloating,no,\nbloating,bloating,no,")):
        bad = TMP / "bad_ref2"
        shutil.copytree(REF, bad, dirs_exist_ok=True)
        text = (REF / "symptom_words.csv").read_text()
        assert edit[0] in text, edit[0]
        (bad / "symptom_words.csv").write_text(text.replace(edit[0], edit[1], 1))
        f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(2))
        sym = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
        try:
            engine.run(f, sym, symptoms_from="2026-01-01", symptoms_to="2026-01-02", reference_dir=bad)
        except engine.ConfigError:
            continue
        raise AssertionError(f"config edit not caught: {edit[1][:30]}")


def test_command_line_writes_a_private_report_once():
    import os
    from gut_finder.__main__ import main
    out = TMP / "cli" / "report.md"
    assert main(["--demo", "--out", str(out)]) == 0
    assert oct(os.stat(out).st_mode & 0o777) == "0o600", oct(os.stat(out).st_mode)
    assert "Demo" in out.read_text()
    assert main(["--demo", "--out", str(out)]) == 2               # won't replace without --overwrite
    assert main(["--demo", "--out", str(out), "--overwrite"]) == 0
    assert main(["--demo", "--out", str(TMP / "cli" / "report.txt")]) == 2


def test_round3_safety_gaps():
    # a red flag next to an unknown word still reaches the medical advice, not an input error
    res = run(filler(3), [["2026-01-02", "10:00", "blood; dizzy", ""]])
    assert res.red_flags == [(2, "blood_in_stool"), (2, "emergency_check")], res.red_flags   # "dizzy" is free text
    # an emoji inside a known word's segment is never silently dropped
    try:
        run(filler(3), [["2026-01-02", "10:00", "bloating \U0001fa78", ""]])
    except engine.InputError:
        pass
    else:
        raise AssertionError("emoji silently dropped")
    # ordinary vomiting stays ordinary
    res = run(filler(3), [["2026-01-02", "10:00", "vomiting", ""]])
    assert not res.red_flags
    # editing any danger-sign row to an ordinary symptom is caught when the list loads
    import shutil
    for edit in (("blood in stool,blood_in_stool,yes", "blood in stool,blood_in_stool,no"),
                 ("blood in stool,blood_in_stool,yes", "blood in stool,bloating,no"),
                 ("dark red poo,black_stool,yes", "dark red poo,bloating,no"),
                 ("pale skin,anaemia_signs,yes", "pale skin,bloating,no")):
        bad = TMP / "bad_ref3"
        shutil.copytree(REF, bad, dirs_exist_ok=True)
        text = (REF / "symptom_words.csv").read_text()
        assert edit[0] in text, edit[0]
        (bad / "symptom_words.csv").write_text(text.replace(edit[0], edit[1], 1))
        f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(2))
        sym = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
        try:
            engine.run(f, sym, symptoms_from="2026-01-01", symptoms_to="2026-01-02", reference_dir=bad)
        except engine.ConfigError:
            continue
        raise AssertionError(f"config edit not caught: {edit[1]}")


def test_connection_closed_after_a_failure():
    import duckdb
    opened = []
    real_connect = duckdb.connect
    def spy(*a, **k):
        con = real_connect(*a, **k)
        opened.append(con)
        return con
    engine.duckdb.connect = spy
    try:
        run(filler(3), [["2026-01-02", "10:00", "felt weird", ""]])
    except engine.InputError:
        pass
    finally:
        engine.duckdb.connect = real_connect
    try:
        opened[-1].execute("SELECT 1")
    except duckdb.Error:
        return
    raise AssertionError("connection left open after a failed run")


def test_reference_amounts_must_be_grams():
    import duckdb
    con = duckdb.connect()
    con.execute((Path(__file__).resolve().parent.parent / "sql" / "03_load_reference.sql").read_text()
                .replace("'reference/", f"'{REF}/"))
    try:
        con.execute("INSERT INTO food_components VALUES ('x', 'sorbitol', 2, 'mg', NULL, 'Measured', 't', 'Open')")
    except duckdb.Error:
        return
    raise AssertionError("an amount in mg was accepted as grams")


def test_round4_advice_wins_and_data_rules():
    from gut_finder import report
    # a danger sign is never hidden behind other problems in the file, even a broken file
    for text in ("date,time,symptom,severity\n2026-01-02,,blood,\n",
                 "date,time,symptom,severity\n2026-01-02,10:00,blood,\nnot,a,valid,row,at all\n",
                 "date,time,symptom,symptom\n2026-01-01,10:00,blood,bloating\n"):
        path = TMP / "flag.csv"
        path.write_text(text)
        f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
        res = engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
        assert res.red_flags and res.red_flags[0][1] == "blood_in_stool", (text, res.red_flags)
        text = report.build(res)
        assert "doesn't show any food suspects" in text and "period blood" in text, text
    # an amount with no unit can't be read as grams
    import duckdb
    con = duckdb.connect()
    con.execute((Path(__file__).resolve().parent.parent / "sql" / "03_load_reference.sql").read_text()
                .replace("'reference/", f"'{REF}/"))
    try:
        con.execute("INSERT INTO food_components VALUES ('x', 'sorbitol', 2, NULL, NULL, 'Measured', 't', 'Open')")
    except duckdb.Error:
        pass
    else:
        raise AssertionError("an amount with no unit was accepted")
    # label patterns are 1–5 words; accents don't stop a match
    try:
        run(filler(2), [], my_foods=[["one two three four five six", "sorbitol", "1"]])
    except engine.InputError:
        pass
    else:
        raise AssertionError("6-word pattern accepted")
    con = run(filler(1) + [["2026-01-01", "8:00 AM", "Breakfast", "Péar (Rôcha), raw", "100.00 g", "x"]], []).con
    assert con.execute("SELECT match_source FROM entry_match JOIN food_entries USING (entry_id) "
                       "WHERE food_name LIKE 'P%'").fetchone()[0] == "reference"


def test_round5_scan_lists_every_sign_and_is_bounded():
    path = TMP / "two.csv"
    path.write_text("date,time,symptom,severity\n2026-01-02,10:00,blood; black stool,\n")
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    res = engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    assert res.red_flags == [(2, "black_stool"), (2, "blood_in_stool")], res.red_flags
    big = TMP / "big.csv"
    big.write_text("date,time,symptom,severity\n" + "2026-01-02,10:00,bloating,\n" * 250_000)
    try:
        engine.run(f, big, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    except engine.InputError:
        return
    raise AssertionError("an oversized symptoms file was scanned")


def test_round6_oversized_file_and_overlaps():
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    big = TMP / "big_flag.csv"
    big.write_text("date,time,symptom,severity\n2026-01-02,10:00,vomiting blood,\n" + "\n" * 6_000_000)
    res = engine.run(f, big, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    assert res.red_flags == [(2, "vomiting_blood")], res.red_flags   # advice wins; no extra "blood in stool"
    import io, contextlib
    from gut_finder.__main__ import main
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        assert main(["--food", str(f), "--symptoms", str(TMP / "missing.csv"), "--symptoms-from", "2026-01-01",
                     "--symptoms-to", "2026-01-03", "--out", str(TMP / "x.md")]) == 2
    assert "NHS 111" in err.getvalue()   # every error carries the safety reminder


def test_round7_positions_full_advice_and_pipes():
    import io, os, contextlib
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    path = TMP / "two_signs.csv"
    path.write_text("date,time,symptom,severity\n2026-01-02,10:00,vomiting blood; blood,\n")
    res = engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    assert res.red_flags == [(2, "blood_in_stool"), (2, "vomiting_blood")], res.red_flags
    from gut_finder.__main__ import main
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        main(["--food", str(f), "--symptoms", str(TMP / "missing.csv"), "--symptoms-from", "2026-01-01",
              "--symptoms-to", "2026-01-03", "--out", str(TMP / "x.md")])
    assert "NHS 111" in err.getvalue() and "999" in err.getvalue()   # calm reminder when no sign was found
    fifo = TMP / "pipe.csv"
    if not fifo.exists():
        os.mkfifo(fifo)
    try:
        engine.run(f, fifo, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    except engine.InputError:
        return
    raise AssertionError("a pipe was accepted as a symptoms file")


def test_round8_advice_on_any_failure():
    import io, os, contextlib
    from gut_finder.__main__ import main
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    sym = TMP / "vb.csv"
    sym.write_text("date,time,symptom,severity\n2026-01-02,10:00,vomiting blood; in stool,\n")
    res = engine.run(f, sym, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    assert res.red_flags == [(2, "vomiting_blood"), (2, "emergency_check")], res.red_flags   # "in stool": free text
    import gut_finder.__main__ as cli
    real_write = cli.write_report
    def refuse(*a, **k):   # simulate "no permission to write the report", whoever runs the test
        raise PermissionError("denied")
    err = io.StringIO()
    cli.write_report = refuse
    try:
        with contextlib.redirect_stderr(err):
            code = main(["--food", str(f), "--symptoms", str(sym), "--symptoms-from", "2026-01-01",
                         "--symptoms-to", "2026-01-03", "--out", str(TMP / "r_denied.md")])
    finally:
        cli.write_report = real_write
    assert code == 4, code
    out = err.getvalue()   # a sign was found but the report couldn't be saved: its own advice, in full
    assert "You've logged vomiting blood" in out and "999" in out and "blood in your poo" not in out, out


def test_round9_descriptors_closed_and_fullwidth_separators():
    import os
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    sym = TMP / "fw.csv"
    sym.write_text("date,time,symptom,severity\n2026-01-02,10:00,vomiting blood\uff1b in stool,\n")
    res = engine.run(f, sym, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
    assert res.red_flags == [(2, "vomiting_blood"), (2, "emergency_check")], res.red_flags
    real_fstat, closed = engine.os.fstat, []
    real_close = engine.os.close
    def broken_fstat(fd):
        raise OSError("disk error")
    def spy_close(fd):
        closed.append(fd)
        real_close(fd)
    engine.os.fstat, engine.os.close = broken_fstat, spy_close
    try:
        engine._open_regular(f, "Food log")
    except engine.InputError:
        pass
    finally:
        engine.os.fstat, engine.os.close = real_fstat, real_close
    assert len(closed) == 1, closed   # the file was closed despite the error


def test_advice_matches_only_what_was_logged():
    from gut_finder import report, wording
    res = engine.RunResult(con=None, red_flags=[(2, "black_stool"), (5, "black_stool"), (7, "lump")])
    text = report.build(res)
    assert wording.RED_FLAG_MESSAGES["black_stool"] in text and wording.RED_FLAG_MESSAGES["lump"] in text
    assert "line 2, 5" in text and text.count(wording.RED_FLAG_MESSAGES["black_stool"]) == 1
    assert wording.RED_FLAG_MESSAGES["vomiting_blood"] not in text and "A&E" not in text


def test_weak_card_explains_its_cap():
    from gut_finder import report, wording
    food = filler(28)
    symptoms = []
    for d in range(3, 28):
        food.append([f"2026-01-{d:02d}", "9:00 AM", "Snacks", "Maltitol sweets", "20.00 g", "x"])
        symptoms.append([f"2026-01-{d:02d}", "12:00", "bloating", ""])
    text = report.build(run(food, symptoms, my_foods=[["maltitol sweets", c, "100" if c == "maltitol" else "0"]
                                                      for c in ("sorbitol", "mannitol", "maltitol", "isomalt",
                                                                "xylitol", "erythritol")]))
    assert "Weaker leads" in text and wording.WEAK_EVIDENCE_DEFAULT in text, text


def test_round11_signs_sorted_by_stem():
    E = (2, "emergency_check")   # own words: the 999 check is always added
    both = [(2, "blood_in_stool"), E, (2, "vomiting_blood")]
    for text, expected in (("vomited blood; dizzy", both),            # unlisted: every sign it could be
                           ("throwing up blood; dizzy", both), ("threw up blood", both),
                           ("bleeding non-stop and vomiting clear fluid", both),
                           ("black vomit; dizzy", [(2, "black_stool"), E, (2, "vomiting_blood")]),
                           ("vomiting black liquid", [(2, "black_stool"), E, (2, "vomiting_blood")]),
                           ("vomiting/black liquid; dizzy", [(2, "black_stool"), E, (2, "vomiting_blood")]),
                           ("coffee-ground vomit; dizzy", [E, (2, "vomiting_blood")]),
                           ("vomiting brown liquid", [E, (2, "vomiting_blood")]),
                           ("black stool with large clots", [(2, "black_stool"), (2, "blood_in_stool"), E]),
                           ("bleedin non-stop, lots of bloodclots", [(2, "blood_in_stool"), E]),
                           ("blood in my poo after being sick", both),
                           ("vomiting blood", [(2, "vomiting_blood")]),   # exactly on the list: one sign
                           ("vomiting", None), ("feeling sick", None)):
        path = TMP / "stems.csv"
        path.write_text(f"date,time,symptom,severity\n2026-01-02,10:00,\"{text}\",\n")
        f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
        try:
            res = engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
            got = res.red_flags or None
        except engine.InputError:
            got = "input error"
        if expected is None:
            assert got is None or got == "input error", (text, got)
        else:
            assert got == expected, (text, got)


def test_card_without_evidence_strength_is_not_scored():
    import duckdb, shutil
    bad = TMP / "no_strength"
    shutil.copytree(REF, bad, dirs_exist_ok=True)
    text = (REF / "component_rules.csv").read_text().replace(",Moderate,suspect,no,0.023,Agreed", ",,suspect,no,0.023,Agreed", 1)
    (bad / "component_rules.csv").write_text(text)
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    s = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
    con = engine.run(f, s, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=bad).con
    blank = con.execute("SELECT component FROM component_rules WHERE evidence_strength IS NULL "
                        "AND flag_mode IS NOT NULL").fetchall()
    assert blank, "test setup: no card lost its evidence strength"
    assert not con.execute("SELECT count(*) FROM scored_components WHERE component = ?", [blank[0][0]]).fetchone()[0]


def test_round13_danger_phrases_cannot_be_remapped():
    import shutil
    bad = TMP / "remap"
    shutil.copytree(REF, bad, dirs_exist_ok=True)
    text = (REF / "symptom_words.csv").read_text()
    assert "vomiting blood,vomiting_blood,yes" in text
    (bad / "symptom_words.csv").write_text(text.replace("vomiting blood,vomiting_blood,yes", "vomiting blood,black_stool,yes"))
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(2))
    sym = write_csv("symptoms.csv", engine.SYMPTOM_COLUMNS, [])
    try:
        engine.run(f, sym, symptoms_from="2026-01-01", symptoms_to="2026-01-02", reference_dir=bad)
    except engine.ConfigError:
        return
    raise AssertionError("a danger phrase was remapped to a different sign")


def test_emergency_check_appears_once_and_last():
    from gut_finder import report, wording
    res = engine.RunResult(con=None, red_flags=[(2, "emergency_check"), (2, "vomiting_blood"), (3, "emergency_check")])
    text = report.build(res)
    assert text.count(wording.RED_FLAG_MESSAGES["emergency_check"]) == 1
    assert text.index(wording.RED_FLAG_MESSAGES["vomiting_blood"]) < text.index(wording.RED_FLAG_MESSAGES["emergency_check"])


def test_round14_free_text_anywhere_adds_the_emergency_check():
    f = write_csv("food.csv", engine.FOOD_COLUMNS, filler(3))
    for body, must in (("pale skin; brought up coffee granules; dizzy", {"anaemia_signs", "emergency_check"}),
                       ("black stool; spewed coffee grounds; confused", {"black_stool", "emergency_check"}),
                       ('"vomiting\nblack stool\nconfused"', {"black_stool", "vomiting_blood", "emergency_check"}),
                       ('"vomiting\nblack stool"', {"black_stool", "vomiting_blood", "emergency_check"}),
                       ("black stool", {"black_stool"}), ("blood; black stool", {"black_stool", "blood_in_stool"})):
        path = TMP / "free.csv"
        path.write_text(f"date,time,symptom,severity\n2026-01-02,10:00,{body},\n")
        res = engine.run(f, path, symptoms_from="2026-01-01", symptoms_to="2026-01-03", reference_dir=REF)
        got = {c for _, c in res.red_flags}
        assert got == must, (body, got)


# --- Planted triggers (D4) ----------------------------------------------------------------

def test_planted_trigger_is_found():
    for seed in range(5):
        food, symptoms = synthetic.generate(seed, plant="sorbitol")
        con = run(food, symptoms).con
        top = con.execute("SELECT component, score FROM component_results "
                          "ORDER BY score DESC NULLS LAST LIMIT 1").fetchone()
        assert top[0] == "sorbitol" and top[1] >= 0.5, (seed, top)


def test_no_trigger_rarely_raises_a_suspect():
    seeds, alarms = 60, 0
    for seed in range(1000, 1000 + seeds):
        food, symptoms = synthetic.generate(seed, plant=None)
        con = run(food, symptoms).con
        alarms += con.execute("SELECT count(*) > 0 FROM component_results WHERE score >= 0.5").fetchone()[0]
    assert alarms / seeds <= 0.10, f"false alarms in {alarms} of {seeds} datasets"


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} passed")
