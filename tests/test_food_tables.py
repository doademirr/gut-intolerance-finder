"""Checks for the published food tables (gut_finder/build_food_tables.py, gut_finder/xlsx.py, sql/03).

Run from the project root:  .venv/bin/python tests/test_food_tables.py
"""
import csv
import shutil
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

import duckdb

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from gut_finder import build_food_tables as bft, xlsx  # noqa: E402

LOAD_SQL = (ROOT / "sql/03_load_reference.sql").read_text()
FIXTURE_REF = ROOT / "tests/fixtures/ref"


def load(reference_dir):
    con = duckdb.connect()
    con.execute(LOAD_SQL.replace("'reference/", f"'{Path(reference_dir).resolve()}/"))
    return con


def value(con, food, component):
    row = con.execute("SELECT amount_per_100g FROM food_components WHERE food = ? AND component = ?",
                      [food, component]).fetchone()
    return None if row is None else row[0]


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc:
        return
    raise AssertionError(f"{fn.__name__}{a!r} didn't raise {exc.__name__}")


# --- Workbooks shaped like the real downloads -------------------------------------------------------------

def _col_letters(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def workbook(path, sheets, padding=0):
    """Write a real .xlsx: text goes to shared strings (as Excel does), numbers are plain <v> cells, and None
    leaves the cell out entirely (a sparse row). sheets = {name: [row, ...]}."""
    m = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    rel = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    shared, index = [], {}

    def cell(ref, v):
        try:
            float(v)
            return f'<c r="{ref}"><v>{v}</v></c>'
        except ValueError:
            if v not in index:
                index[v] = len(shared)
                shared.append(v)
            return f'<c r="{ref}" t="s"><v>{index[v]}</v></c>'

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        sheet_xml, rels = [], []
        for n, (name, rows) in enumerate(sheets.items(), start=1):
            body = []
            for r, row in enumerate(rows, start=1):
                cs = "".join(cell(f"{_col_letters(c)}{r}", v) for c, v in enumerate(row) if v is not None)
                if cs:
                    body.append(f'<row r="{r}">{cs}</row>')
            z.writestr(f"xl/worksheets/sheet{n}.xml",
                       f'<worksheet xmlns="{m}"><sheetData>{"".join(body)}</sheetData></worksheet>')
            sheet_xml.append(f'<sheet name="{escape(name)}" sheetId="{n}" r:id="rId{n}"/>')
            rels.append(f'<Relationship Id="rId{n}" Target="worksheets/sheet{n}.xml"/>')
        z.writestr("xl/workbook.xml", f'<workbook xmlns="{m}" xmlns:r="{rel}"><sheets>{"".join(sheet_xml)}'
                                      f'</sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/'
                                                 f'package/2006/relationships">{"".join(rels)}</Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<sst xmlns="{m}">' + "".join(
            f"<si><t>{escape(s)}</t></si>" for s in shared) + "</sst>")
        if padding:
            z.writestr("xl/padding.xml", "<p>" + "0" * padding + "</p>")


def cofid_rows():
    # CoFID layout: 3 heading rows (name, code, label), lactose found by its heading, not its position.
    head = ["Food Code", "Food Name", "Description", "Group", "Previous", "Main data references", "Footnote",
            "Water (g)", "Galactose (g)", "Lactose (g)", "Fructose (g)", "Glucose (g)"]
    return [head, [None] * 7 + ["WATER", "GALACT", "LACT"], [None] * 7 + ["Water", "Galactose", "Lactose"],
            ["11-001", "Test milk", "x", "MA", None, None, None, "87.8", "0", "4.70", "1.5", "Tr"],
            ["11-002", "Test butter", "x", "OB", None, None, None, "15", "Tr", "Tr"],
            ["11-003", "Test beer", "x", "QA", None, None, None, "93", "0", "0.5"],
            ["11-004", "Test cream liqueur", "x", "QC", None, None, None, "50", "N", "N"],
            ["11-005", "Test bread", "x", "AB", None, None, None, "38"],                      # lactose cell missing
            ["11-006", "Test watercress", "x", "DG", None, None, None, "94", "0", "0"],
            ["11-006", "Test aubergine", "x", "DG", None, None, None, "84", "0", "0"],          # repeated code
            ["11-007", "Test cheese", "x", "MB", None, None, None, "40", "0", "1.2E-2"]]


def afcd_rows():
    # AFCD layout: title row, blank row, then headings; nutrient headings carry a line break before the unit.
    head = ["Public Food Key", "Classification", "Derivation", "Food Name", "Energy with dietary fibre (kJ)",
            "Mannitol \n(g)", "Lactose \n(g)", "Sorbitol \n(g)", "Erythritol \n(g)", "Maltitol \n(g)",
            "Xylitol \n(g)", "Fructose \n(g)", "Glucose \n(g)"]
    return [["Release 3 - Nutrient profiles (per 100 g)"], [], head,
            ["F000001", "16801", "Analysed", "Test prune, dried", "900", None, "0", "12.7"],
            ["F000002", "24701", "Analysed", "Test mushroom, raw", "100", "1.3", "0", "0"],
            ["F000003", "19101", "Borrowed", "Test milk", "270", None, None, None],
            ["F000004", "28101", "Analysed", "Test chocolate, no added sugar", "2000", None, "0", None, "3.8",
             "28", "3.6", "2.5", "1"]]


def by_id(rows):
    return {r["food_id"]: r for r in rows}


def test_read_cofid_on_a_source_shaped_workbook():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "cofid.xlsx"
        workbook(path, {"1.1 Notes": [["Notes"]], "1.3 Proximates": cofid_rows()})
        rows = by_id(bft.read_cofid(path))
    assert rows["11-001"]["lactose"] == "4.7" and rows["11-001"]["per"] == "100g"
    assert rows["11-002"]["lactose"] == "Tr"
    assert rows["11-003"]["per"] == "100ml"              # alcoholic drinks (groups Q…) are per 100 ml
    assert rows["11-004"]["lactose"] == "N"
    assert rows["11-005"]["lactose"] == ""               # missing cell = no value
    assert rows["11-006#1"]["name"] == "Test watercress" and rows["11-006#2"]["name"] == "Test aubergine"
    assert "11-006" not in rows
    assert rows["11-007"]["lactose"] == "0.012"          # written as a plain decimal, value unchanged
    assert rows["11-001"]["sorbitol"] == rows["11-001"]["mannitol"] == rows["11-001"]["maltitol"] == ""
    assert rows["11-001"]["food_group"] == "MA" and rows["11-006#1"]["food_group"] == "DG"
    assert (rows["11-001"]["fructose"], rows["11-001"]["glucose"]) == ("1.5", "Tr")


def test_read_afcd_on_a_source_shaped_workbook():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "afcd.xlsx"
        workbook(path, {"Contents": [["About"]], "All solids & liquids per 100 g": afcd_rows(),
                        "Liquids only per 100 mL": [["ignored"]]})
        rows = by_id(bft.read_afcd(path))
    assert rows["F000001"]["sorbitol"] == "12.7" and rows["F000001"]["mannitol"] == ""
    assert rows["F000002"]["mannitol"] == "1.3" and rows["F000002"]["sorbitol"] == "0"
    assert rows["F000003"]["lactose"] == ""
    assert all(r["per"] == "100g" for r in rows.values())
    assert rows["F000001"]["food_group"] == "16801"                 # the published classification code
    assert (rows["F000004"]["maltitol"], rows["F000004"]["xylitol"], rows["F000004"]["erythritol"]) == (
        "28", "3.6", "3.8")
    assert rows["F000001"]["maltitol"] == ""
    assert (rows["F000004"]["fructose"], rows["F000004"]["glucose"]) == ("2.5", "1")


def test_read_afcd_density_keeps_liquids_and_drops_solids():
    head = ["Public Food Key", "Classification", "Food Name", "Specific Gravity"]
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "d.xlsx"
        workbook(path, {"Food details": [["Notes"], head, ["F000001", "1", "Milk", "1.03"], ["F000002", "2", "Bread", "0"],
                                          ["F000003", "3", "Blank", ""]]})
        assert bft.read_afcd_density(path) == [{"food_id": "F000001", "specific_gravity": "1.03"}]
        workbook(path, {"Food details": [head, ["F000001", "1", "Milk", "9"]]})
        raises(bft.BuildError, bft.read_afcd_density, path)
        workbook(path, {"Food details": [head, ["F000001", "1", "Milk", "heavy"]]})
        raises(bft.BuildError, bft.read_afcd_density, path)
        two = ["Public Food Key", "Specific Gravity Notes", "Food Name", "Specific Gravity"]   # picks by exact name
        workbook(path, {"Food details": [two, ["F000001", "1.2", "Milk", "1.03"]]})
        assert bft.read_afcd_density(path) == [{"food_id": "F000001", "specific_gravity": "1.03"}]
        workbook(path, {"Food details": [["Public Food Key", "Specific Gravity Notes"], ["F000001", "1.2"]]})
        raises(bft.BuildError, bft.read_afcd_density, path)
        workbook(path, {"Food details": [head, ["F000001", "1", "Milk", "1.03"], ["F000001", "1", "Milk", "0"]]})
        raises(bft.BuildError, bft.read_afcd_density, path)


def test_density_files_are_checked_when_loaded():
    with tempfile.TemporaryDirectory() as tmp:
        for name, text in (("food_tables/au_afcd_r3_density.csv", "food_id,specific_gravity\nT000001,9\n"),
                           ("food_tables/au_afcd_r3_density.csv", "food_id,specific_gravity\nNOPE,1.1\n"),
                           ("liquid_densities.csv", "first_part,density,based_on\nmilk,0,x\n"),
                           ("typical_portions.csv", "pattern,unit,grams,max_per_100g,basis\nwhey,scoop,0,10,x\n"),
                           ("typical_portions.csv", "pattern,unit,grams,max_per_100g,basis\nwhey,scoop,NaN,10,x\n"),
                           ("typical_portions.csv", "pattern,unit,grams,max_per_100g,basis\nwhey,scoop,inf,10,x\n")):
            ref = Path(tmp) / "ref"
            shutil.rmtree(ref, ignore_errors=True)
            shutil.copytree(FIXTURE_REF, ref)
            (ref / name).write_text(text)
            raises(duckdb.Error, load, ref)


def test_read_usda_keeps_missing_lactose_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        with open(d / "food.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["fdc_id", "data_type", "description", "food_category_id", "publication_date"])
            w.writerows([["1", "sr", "Milk, whole", "1", "2019"], ["2", "sr", "Bread,  white", "18", "2019"],
                         ["3", "sr", "Water", "14", "2019"]])
        with open(d / "food_category.csv", "w", newline="") as f:
            csv.writer(f).writerows([["id", "code", "description"], ["1", "0100", "Dairy and Egg Products"],
                                     ["18", "1800", "Baked Products"]])
        with open(d / "food_nutrient.csv", "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(["id", "fdc_id", "nutrient_id", "amount"])
            w.writerows([["10", "1", "1013", "4.80"], ["11", "1", "1003", "3.2"], ["12", "3", "1013", "0.0"],
                         ["13", "1", "1012", "2.50"], ["14", "1", "1011", "1.00"]])
        rows = by_id(bft.read_usda(d))
    assert rows["1"]["lactose"] == "4.8"
    assert (rows["1"]["fructose"], rows["1"]["glucose"]) == ("2.5", "1"), "fructose and glucose nutrient ids"
    assert rows["2"]["lactose"] == "" and rows["2"]["name"] == "Bread, white"
    assert rows["3"]["lactose"] == "0"
    assert rows["1"]["food_group"] == "Dairy and Egg Products" and rows["3"]["food_group"] == ""   # unlisted id


def test_readers_stop_on_bad_data():
    with tempfile.TemporaryDirectory() as tmp:
        bad_value = afcd_rows()
        bad_value[3][6] = "150"                       # 150 g lactose per 100 g is impossible
        workbook(Path(tmp) / "a.xlsx", {"All solids & liquids per 100 g": bad_value})
        raises(bft.BuildError, bft.read_afcd, Path(tmp) / "a.xlsx")
        bad_id = cofid_rows()
        bad_id[3][0] = "11-001; DROP"
        workbook(Path(tmp) / "c.xlsx", {"1.3 Proximates": bad_id})
        raises(bft.BuildError, bft.read_cofid, Path(tmp) / "c.xlsx")
        no_head = afcd_rows()
        no_head[2][0] = "Key"
        workbook(Path(tmp) / "h.xlsx", {"All solids & liquids per 100 g": no_head})
        raises(bft.BuildError, bft.read_afcd, Path(tmp) / "h.xlsx")
        workbook(Path(tmp) / "s.xlsx", {"Other": [["x"]]})
        raises(xlsx.WorkbookError, bft.read_cofid, Path(tmp) / "s.xlsx")


# --- Values ------------------------------------------------------------------------------------------------

def test_clean_value_is_lossless_and_strict():
    assert bft.clean_value("4.70", "t") == "4.7"
    assert bft.clean_value("1E-2", "t") == "0.01"
    assert bft.clean_value("0.0000001", "t") == "0.0000001"       # no rounding
    assert bft.clean_value("0.123456789", "t") == "0.123456789"
    assert bft.clean_value("1.1000000000000001", "t") == "1.1"    # Excel's stored form of 1.1
    assert bft.clean_value("5.0999999999999996", "t") == "5.1"
    assert bft.clean_value("100", "t") == "100"
    assert bft.clean_value("0.0", "t") == "0"
    assert bft.clean_value(" Tr ", "t") == "Tr"
    assert bft.clean_value("N", "t") == "N"
    assert bft.clean_value(None, "t") == ""
    for bad in ("abc", "-1", "4 g", "tr", "100.1", "1E5", "nan", "inf", "1" * 40, "1E-30"):  # 1E-30: too
        # many decimal places to load, so it stops here rather than at loading
        raises(bft.BuildError, bft.clean_value, bad, "t")


def test_long_bad_value_is_rejected_quickly():
    start = time.perf_counter()
    for n in (1_000, 10_000, 100_000):
        raises(bft.BuildError, bft.clean_value, "1" * n + "x", "t")
    assert time.perf_counter() - start < 0.5


# --- Loading into the engine (sql/03) ----------------------------------------------------------------------

def test_reading_rules_number_trace_unknown_and_millilitres():
    con = load(FIXTURE_REF)
    assert value(con, "usda:900001", "lactose") == 4.8             # a number is grams per 100 g
    assert value(con, "usda:900002", "lactose") is None            # blank: unknown, no row
    assert value(con, "uk:90-001", "lactose") == 0                 # "Tr": counts as 0
    assert value(con, "uk:90-002", "lactose") is None              # "N": present but not measured: unknown
    assert value(con, "uk:90-003", "lactose") is None              # per 100 ml: millilitres aren't grams
    assert con.execute("SELECT per FROM reference_foods WHERE food = 'uk:90-003'").fetchone()[0] == "100ml"
    assert value(con, "au:T000001", "sorbitol") == 12.7
    assert value(con, "au:T000001", "mannitol") is None
    assert value(con, "au:T000002", "mannitol") == 1.3
    assert value(con, "au:T000002", "sorbitol") == 0


def test_every_value_names_its_source():
    con = load(FIXTURE_REF)
    src = con.execute("SELECT source FROM food_components WHERE food = 'au:T000001' AND component = 'sorbitol'"
                      ).fetchone()[0]
    assert src == "Australian Food Composition Database Release 3 T000001", src


def test_fresh_food_has_no_maltitol_or_isomalt():
    con = load(FIXTURE_REF)
    for food in ("usda:900003", "usda:900004", "uk:90-004#1", "uk:90-004#2", "au:T000001", "au:T000002", "au:T000004"):
        assert value(con, food, "maltitol") == 0 and value(con, food, "isomalt") == 0, food
    for food in ("usda:900005",            # sweets: not fresh food
                 "uk:90-005",              # fruit group, but a nectar
                 "uk:90-006",              # vegetable dishes aren't on the list
                 "au:T000005",             # fruit drink, not 100% juice
                 "usda:900001", "usda:900002", "uk:90-001"):
        assert value(con, food, "maltitol") is None and value(con, food, "isomalt") is None, food
    assert value(con, "uk:90-003", "maltitol") is None                      # per 100 ml stays unknown
    assert value(con, "au:T000003", "maltitol") == 28                       # the table's own value
    assert value(con, "au:T000003", "xylitol") == 3.6 and value(con, "au:T000003", "erythritol") == 3.8
    assert value(con, "au:T000001", "xylitol") is None                      # only maltitol and isomalt are 0
    src = con.execute("SELECT source FROM food_components WHERE food = 'au:T000004' AND component = 'maltitol'"
                      ).fetchone()[0]
    assert src == "Fresh food has no maltitol (D14): Fruit juices", src


def test_only_sweetening_words_keep_a_fresh_food_unknown():
    names = {"Chicken, light meat, raw": 0, "Bacon, reduced salt": 0, "Mushrooms, exposed to ultraviolet light": 0,
             "Prunes, stewed, without added sugar": 0, "Baked beans, reduced sugar": None,
             "Peanut butter, no added sugar or salt": None, "Jam, sugar reduced": None, "Juice, apple, lite": None,
             "Cola, diet": None, "Yoghurt, Sugar-Free": None, "Peach nectar": None,
             "Cordial, intense sweetened": None, "Jelly, with sucralose": None,
             "Juice, apple, low energy": None, "Juice, apple, low calories": None, "Juice, energy reduced": None,
             "Apple juice with mannitol": None, "Pear, with lactitol": None, "Juice, with cyclamate": None,
             "Pomegranate juice drink": None, "Fruit cocktail, canned": None, "Fruit juice smoothie": None,
             "Potato salad, home-prepared": None, "Pumpkin pie mix, canned": None, "Cranberry sauce": None,
             "Chicken wing, barbecue flavored": None, "Baked beans, with pork sausages": None,
             "Fruit salad, homemade": 0, "Squash, zucchini, baby, raw": 0, "Plums, stewed": 0,
             "Peanuts, raisins and chocolate chips": None, "Corn pudding, home prepared": None,
             "Mints, sugar free": None, "Cranberries, dried, in a cereal bar": None, "Yogurt, fruit": None,
             "Potato pancakes": 0, "Fishcakes, white fish, coated in breadcrumbs, baked": 0, "Mint, fresh": 0,
             "Cranberry, dried, sweetened": None, "Nuts, all flavors": None, "Almonds, vanilla flavour": None,
             "Cashews, chilli flavor": None, "Peanuts, mixed flavours": None,
             "Cranberries, dried, unsweetened": 0, "Apple juice, unsweetened": 0,
             "Nuts, coconut meat, dried (desiccated), not sweetened": 0}
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        with open(ref / "food_tables/usda_sr_legacy.csv", "a", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            for i, name in enumerate(names):
                w.writerow([f"95{i:04d}", name, "Fruits and Fruit Juices", "", "", "", "", "", "", "", "", "100g"])
        con = load(ref)
        got = {name: value(con, f"usda:95{i:04d}", "maltitol") for i, name in enumerate(names)}
    assert got == names, got


def test_a_fresh_food_group_that_matches_nothing_stops_the_load():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        with open(ref / "fresh_food_groups.csv", "a") as f:
            f.write("uk,FZ,Typo\n")
        raises(duckdb.Error, load, ref)


def test_foods_that_cannot_contain_milk():
    con = load(FIXTURE_REF)
    free = dict(con.execute("SELECT food, description FROM milk_free_foods").fetchall())
    assert free["usda:900010"] == "Poultry", free
    assert free["usda:900014"] == "Plain cocoa powder", free
    for food in ("usda:900011",            # a sausage: lactose can be added
                 "usda:900012",            # mashed: made with milk
                 "usda:900016",            # honey roasted: can be made with butter
                 "usda:900013",            # the table's own figure wins
                 "usda:900015",            # the same group as cocoa, but not the named food
                 "usda:900002"):           # bread: not on the list
        assert food not in free, food


def test_singular_and_plural_words_are_the_same_word():
    con = load(FIXTURE_REF)
    got = lambda name: con.execute("SELECT name_words(?)", [name]).fetchone()[0]
    assert got("Sandwiches, Pies & Strawberries") == got("sandwich pie strawberry"), got("Sandwiches, Pies & Strawberries")
    assert got("Tomatoes") == got("tomato") and got("Glass") == ["glass"]
    assert got("McKenzie's 95% Lean, 500 g") == got("mckenzie lean"), got("McKenzie's 95% Lean, 500 g")
    assert "65" in got("Chicken 65") and "b12" in got("Lean B12")     # other numbers can name something
    assert got("Cacao") == got("cocoa") and got("Half & Half") == got("half-and-half") == got("cream")


def test_a_milk_free_group_that_matches_nothing_stops_the_load():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        with open(ref / "milk_free_groups.csv", "a") as f:
            f.write("usda,Sweets,No such food,Typo\n")
        raises(duckdb.Error, load, ref)


def test_a_word_listed_as_both_kinds_stops_the_load():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        with open(ref / "lactose_name_words.csv", "a") as f:
            f.write("creams,plain\n")         # the same word as "cream" once singular
        raises(duckdb.Error, load, ref)


def test_a_dish_is_worked_out_from_its_recipe():
    con = load(FIXTURE_REF)
    assert value(con, "own:D900", "lactose") == 2.4          # 50 g of milk at 4.8, in 100 g; chicken counts as no milk
    assert value(con, "own:D900", "sorbitol") is None        # nothing else is claimed
    assert value(con, "own:P900", "lactose") == 0
    src, basis, status = con.execute("""SELECT source, basis, status FROM food_components
                                        WHERE food = 'own:D900' AND component = 'lactose'""").fetchone()
    assert src == "Own table, typical recipe: R25" and basis == "Estimated" and status == "Proposed", (src, basis, status)


def _broken_own_table(file, line):
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        with open(ref / file, "a") as f:
            f.write(line)
        raises(duckdb.Error, load, ref)


def _own_table_with(*changes):
    """Load the fixture tables with extra lines added to the named files."""
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        for file, line in changes:
            with open(ref / file, "a") as f:
                f.write(line)
        return load(ref)


def test_an_ingredient_with_no_known_figure_leaves_the_dish_unknown():
    con = _own_table_with(("own_dishes.csv", "D901,Test bread bake,R25,,,Proposed,\n"),
                          ("own_dish_ingredients.csv", "D901,Milk,50,usda:900001,\nD901,Bread,50,usda:900002,\n"))
    assert value(con, "own:D901", "lactose") is None         # a gap can't quietly become 0, or a half figure
    assert con.execute("SELECT count(*) FROM reference_foods WHERE food = 'own:D901'").fetchone() == (1,)


def test_weight_lost_in_cooking_raises_the_figure_per_100_g():
    con = _own_table_with(("own_dishes.csv", "D907,Test reduced milk,R25,,,Proposed,-50\n"),
                          ("own_dish_ingredients.csv", "D907,Milk,100,usda:900001,\n"))
    assert value(con, "own:D907", "lactose") == 9.6          # 4.8 g in 50 g of finished dish


def test_a_weight_change_that_isnt_a_percentage_stops_the_load():
    for bad in ("lots", "-100"):
        try:
            _own_table_with(("own_dishes.csv", f"D908,Test bad change,R25,,usda:900001,Proposed,{bad}\n"),
                            ("own_dish_ingredients.csv", "D908,Milk,10,usda:900001,\n"))
        except duckdb.Error as e:
            assert "isn't a percentage" in str(e), e
        else:
            raise AssertionError("loaded")


def test_any_component_is_worked_out_when_every_ingredient_has_it():
    con = load(FIXTURE_REF)
    food, grams = con.execute("""SELECT food, amount_per_100g FROM food_components
                                 WHERE component = 'sorbitol' AND amount_per_100g > 0 ORDER BY food LIMIT 1""").fetchone()
    con = _own_table_with(("own_dishes.csv", "D903,Test sorbitol dish,R25,,,Proposed,\n"),
                          ("own_dish_ingredients.csv", f"D903,Fruit,100,{food},\n"),
                          ("ingredient_tags.csv", f"{food},Test,\n"))
    assert value(con, "own:D903", "sorbitol") == round(grams, 2)


def test_a_dish_with_a_published_figure_keeps_it_and_gets_only_tags():
    con = load(FIXTURE_REF)
    assert con.execute("SELECT count(*) FROM reference_foods WHERE food = 'own:D902'").fetchone() == (0,)
    assert value(con, "own:D902", "lactose") is None
    assert value(con, "usda:900001", "lactose") == 4.8
    got = {r[0] for r in con.execute("SELECT tag FROM dish_tags WHERE food = 'usda:900001'").fetchall()}
    assert got == {"milk", "wheat", "egg"}, got              # from the ingredients and what the stand-in can't carry


def test_a_dish_inherits_the_tags_of_its_ingredients():
    con = load(FIXTURE_REF)
    got = {r[0] for r in con.execute("SELECT tag FROM dish_tags WHERE food = 'own:D900' AND basis = 'recipe'").fetchall()}
    assert got == {"milk", "excess_fructose"}, got           # the chicken has no tags


def test_often_tags_stay_apart_and_a_recipe_tag_wins():
    con = load(FIXTURE_REF)
    got = dict(con.execute("SELECT tag, basis FROM dish_tags WHERE food = 'own:D900'").fetchall())
    assert got == {"milk": "recipe", "excess_fructose": "recipe", "wheat": "often"}, got


def test_an_often_row_for_an_unknown_dish_stops_the_load():
    _broken_own_table("own_dish_often.csv", "D999,wheat,Test\n")


def test_an_often_row_with_no_dish_stops_the_load():
    _broken_own_table("own_dish_often.csv", ",wheatt,Test\n")


def test_an_often_row_with_no_tags_stops_the_load():
    _broken_own_table("own_dish_often.csv", "D902,,Test\n")


def test_an_often_row_with_no_reason_stops_the_load():
    _broken_own_table("own_dish_often.csv", "D902,wheat,\n")


def test_an_often_tag_not_on_the_list_stops_the_load():
    _broken_own_table("own_dish_often.csv", "D902,wheatt,Test\n")


def test_an_own_rows_status_comes_from_its_file():
    con = load(FIXTURE_REF)
    assert con.execute("SELECT status FROM food_components WHERE food = 'own:P900'").fetchone() == ("Agreed",)


def test_an_ingredient_that_isnt_a_table_food_stops_the_load():
    _broken_own_table("own_dish_ingredients.csv", "D900,Typo,10,usda:999999,\n")


def test_an_ingredient_for_a_dish_that_isnt_listed_stops_the_load():
    _broken_own_table("own_dish_ingredients.csv", "D999,Milk,10,usda:900001,\n")


def test_a_repeated_ingredient_stops_the_load():
    _broken_own_table("own_dish_ingredients.csv", "D900,Milk,50,usda:900001,\n")


def test_a_dish_with_no_recipe_stops_the_load():
    _broken_own_table("own_dishes.csv", "D901,Test empty dish,R25,,,Proposed,\n")


def test_an_own_row_with_no_source_stops_the_load():
    _broken_own_table("own_foods.csv", "P901,Test no source,Own: plain food,0,,,Proposed\n")


def test_an_unknown_status_stops_the_load():
    _broken_own_table("own_foods.csv", "P901,Test bad status,Own: plain food,0,R24,,Maybe\n")


def trigger(con, food):
    return tuple(value(con, food, c) for c in ("fructans", "gos", "wheat_gluten"))


def test_foods_are_tagged_by_review_then_words_then_group():
    rows = ("950001,Test rye bread,Baked Products,,,,,,,,,100g\n"
            "950002,\"Test bread, gluten free\",Baked Products,,,,,,,,,100g\n"
            "950003,Test green bean,Baked Products,,,,,,,,,100g\n"
            "950004,\"Test bean, black\",Baked Products,,,,,,,,,100g\n"
            "950005,Test snack bar,Snacks,,,,,,,,,100g\n"
            "950006,Test rice cake mix,Snacks,,,,,,,,,100g\n"
            "950007,Test plum,Fruits and Fruit Juices,,,,,,,,,100g\n"
            "950008,Test onion jam,Fruits and Fruit Juices,,,,,,,,,100g\n")
    con = _own_table_with(("food_tables/usda_sr_legacy.csv", rows))
    assert trigger(con, "usda:950001") == (100, 0, 100), "a word tags it, and the other cards are known"
    assert trigger(con, "usda:950002") == (0, 0, 0), "gluten free: grain words don't count"
    assert trigger(con, "usda:950003") == (None, None, None), "green bean isn't a dried bean, so nothing says what it is"
    assert trigger(con, "usda:950004") == (0, 100, 0)
    assert trigger(con, "usda:950005") == (None, None, None), "no word and no plain group: unknown, never 0"
    assert trigger(con, "usda:950006") == (0, 0, 0), "a word for a plain food"
    assert trigger(con, "usda:950007") == (0, 0, 0), "a plain group"
    assert trigger(con, "usda:950008") == (100, 0, 0), "a word beats a plain group"


def test_a_dish_counts_only_its_tagged_ingredients():
    con = _own_table_with(
        ("food_tables/usda_sr_legacy.csv", "950001,Test bread roll,Baked Products,,,,,,,,,100g\n"
                                           "950007,Test plum,Fruits and Fruit Juices,,,,,,,,,100g\n"
                                           "950005,Test snack bar,Snacks,,,,,,,,,100g\n"),
        ("own_dishes.csv", "D910,Test plum roll,R25,,,Proposed,\nD911,Test coated plum,R25,,,Proposed,\n"
                           "D912,Test bar plum,R25,,,Proposed,\n"),
        ("own_dish_ingredients.csv", "D910,Roll,50,usda:950001,\nD910,Plum,150,usda:950007,\n"
                                     "D911,Plum,100,usda:950007,wheat\n"
                                     "D912,Plum,50,usda:950007,\nD912,Bar,50,usda:950005,\n"),
        ("tag_list.csv", "fructans,trigger,x\n"),
        ("ingredient_tags.csv", "usda:950001,Test bread roll,wheat;fructans\nusda:950007,Test plum,\n"
                                "usda:950005,Test snack bar,\n"))
    assert trigger(con, "own:D910") == (25, 0, 25), "50 g of 200 g"
    assert value(con, "own:D911", "wheat_gluten") == 100 and value(con, "own:D911", "fructans") == 0, "also_contains"
    assert trigger(con, "own:D912") == (0, 0, 0), "a reviewed ingredient with no tag counts as none"


def test_bad_trigger_rules_stop_the_load():
    _broken_own_table("trigger_words.csv", "cake,lactose,Not a tag card\n")
    _broken_own_table("trigger_free_groups.csv", "usda,No such group,Typo\n")


def test_excess_fructose_is_fructose_minus_glucose_and_needs_both():
    rows = ("T000901,Test pear,16801,,,,,,,6,2,100g\n"        # 4 g excess
            "T000902,Test grape,16801,,,,,,,3,5,100g\n"       # more glucose: 0, never negative
            "T000903,Test fig,16801,,,,,,,6,,100g\n"          # no glucose: unknown
            "T000904,Test date,16801,,,,,,,N,2,100g\n"        # not analysed: unknown
            "T000905,Test swede,16801,,,,,,,Tr,Tr,100g\n"     # traces count as 0
            "T000906,Test cordial,16801,,,,,,,6,2,100ml\n")   # per 100 ml: unknown
    con = _own_table_with(("food_tables/au_afcd_r3.csv", rows))
    assert value(con, "au:T000901", "excess_fructose") == 4
    assert value(con, "au:T000902", "excess_fructose") == 0
    assert value(con, "au:T000903", "excess_fructose") is None
    assert value(con, "au:T000904", "excess_fructose") is None
    assert value(con, "au:T000905", "excess_fructose") == 0
    assert value(con, "au:T000906", "excess_fructose") is None
    assert con.execute("SELECT count(*) FROM food_components WHERE component IN ('fructose', 'glucose')"
                       ).fetchone() == (0,)


def test_a_bad_fructose_value_stops_the_load():
    _broken_own_table("food_tables/au_afcd_r3.csv", "T000907,Test bad,16801,,,,,,,lots,2,100g\n")


def test_own_figures_fill_blanks_and_copy_a_published_figure():
    con = _own_table_with(("own_figures.csv", "usda:900015,,usda:900001,USDA test,Same food,Proposed\n"))
    assert value(con, "usda:900015", "lactose") == 4.8, "same_as copies the published figure"
    row = con.execute("SELECT basis, source, status FROM food_components WHERE food = 'usda:900015' "
                      "AND component = 'lactose'").fetchone()
    assert row == ("Estimated", "Own figure: USDA test", "Proposed"), row
    con = _own_table_with(("own_figures.csv", "usda:900015,6,,R37,Typed,Proposed\n"))
    assert value(con, "usda:900015", "lactose") == 6


def test_bad_own_figures_stop_the_load():
    for line in ("usda:900015,6,,,No source,Proposed\n",            # no source
                 "usda:900015,6,,R37,Bad status,Maybe\n",           # status
                 "usda:999999,6,,R37,No such food,Proposed\n",      # not in any table
                 "usda:900015,6,usda:900001,R37,Both,Proposed\n",   # a figure and same_as
                 "usda:900015,,,R37,Neither,Proposed\n",            # neither
                 "usda:900015,many,,R37,Not a number,Proposed\n",
                 "usda:900015,120,,R37,Over 100,Proposed\n",
                 "usda:900015,,usda:999999,R37,Nothing to copy,Proposed\n",
                 "usda:900001,6,,R37,Already published,Proposed\n",  # a published figure always wins
                 "usda:900015,6,,R37,Twice,Proposed\nusda:900015,5,,R37,Twice,Proposed\n",
                 "usda:900017,6,,R37,Counted as no lactose,Proposed\n",   # salt: 0 and 6 would both apply
                 "uk:90-003,6,,R37,Per 100 ml,Proposed\n",
                 "usda:900015,,uk:90-003,R37,Copying a per-100 ml row,Proposed\n",
                 "usda:900015,,own:P900,R37,Not a published figure,Proposed\n"):
        _broken_own_table("own_figures.csv", line)


def test_a_published_food_that_doesnt_exist_stops_the_load():
    try:
        _own_table_with(("own_dishes.csv", "D904,Test typo,R25,,usda:999999,Proposed,\n"),
                        ("own_dish_ingredients.csv", "D904,Milk,10,usda:900001,\n"))
    except duckdb.Error as e:
        assert "published food not in any table" in str(e), e
    else:
        raise AssertionError("loaded")


def test_a_published_dish_with_lactose_gets_the_milk_tag_whatever_its_recipe_says():
    con = _own_table_with(("own_dishes.csv", "D905,Test milky stew,R25,,usda:900097,Proposed,\n"),
                          ("own_dish_ingredients.csv", "D905,Chicken,100,usda:900010,\n"))
    assert con.execute("SELECT tag FROM dish_tags WHERE food = 'usda:900097'").fetchall() == [("milk",)]


def test_a_bad_status_stops_the_load_on_a_dish_that_makes_no_figure():
    try:
        _own_table_with(("own_dishes.csv", "D906,Test bad status,R25,,usda:900001,Maybe,\n"),
                        ("own_dish_ingredients.csv", "D906,Milk,10,usda:900001,\n"))
    except duckdb.Error as e:
        assert "no valid status" in str(e), e
    else:
        raise AssertionError("loaded")


def test_an_ingredient_nobody_reviewed_stops_the_load():
    _broken_own_table("own_dish_ingredients.csv", "D900,Sausage,10,usda:900011,\n")


def test_a_tag_not_on_the_list_stops_the_load():
    _broken_own_table("ingredient_tags.csv", "usda:900011,Test sausage,porkk\n")


def test_an_also_contains_tag_not_on_the_list_stops_the_load():
    _broken_own_table("own_dish_ingredients.csv", "D900,Coated,10,usda:900010,wheatt\n")


def test_lactose_without_a_milk_tag_stops_the_load():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        path = ref / "ingredient_tags.csv"
        path.write_text(path.read_text().replace('"Test milk, whole",milk', '"Test milk, whole",'))
        raises(duckdb.Error, load, ref)


def test_a_real_own_table_loads_and_every_dish_is_a_sane_figure():
    con = load(ROOT / "reference")
    got = dict(con.execute("""SELECT f.food, c.amount_per_100g FROM reference_foods f JOIN food_components c
                              ON c.food = f.food AND c.component = 'lactose' WHERE f.tbl = 'own'""").fetchall())
    assert all(0 <= v < 5 for v in got.values()), got
    dishes = con.execute("SELECT count(*) FROM own_dishes").fetchone()[0]
    assert dishes >= 230, dishes


def test_real_excess_fructose_figures():
    con = load(ROOT / "reference")
    named = dict(con.execute("""SELECT f.name, c.amount_per_100g FROM reference_foods f JOIN food_components c
                                USING (food) WHERE c.component = 'excess_fructose'""").fetchall())
    assert named["Honey"] > 5, named["Honey"]
    assert named["Bananas, raw"] < 1, named["Bananas, raw"]
    rule = con.execute("""SELECT dose_threshold, window_start_h, window_end_h, flag_mode, status FROM component_rules
                          WHERE component = 'excess_fructose'""").fetchone()
    assert rule == (6, 0.5, 4, "suspect", "Agreed"), rule


def test_real_trigger_cards_and_foods():
    con = load(ROOT / "reference")
    rules = dict((c, (d, s, e)) for c, d, s, e in con.execute(
        "SELECT component, dose_threshold, window_start_h, window_end_h FROM component_rules").fetchall())
    assert rules["excess_fructose"] == (6, 0.5, 4) and rules["fructans"] == (100, 1, 8), rules
    assert rules["gos"] == (90, 1, 8) and rules["wheat_gluten"] == (60, 1, 24), rules
    assert rules["coffee"] == (25, 0, 2), rules
    coffee = dict(con.execute("""SELECT f.name, c.amount_per_100g FROM reference_foods f JOIN food_components c USING (food)
                                 WHERE c.component = 'coffee'""").fetchall())
    assert coffee["Cream, fluid, light (coffee cream or table cream)"] == 0, "cream isn't coffee"
    assert coffee["Truffles, mocha, homemade"] == 0, "a sweet isn't a coffee drink"
    assert coffee["Coffee, espresso, from ground coffee beans"] == 100
    assert coffee["Alcoholic beverage, liqueur, coffee, 53 proof"] == 0, "liqueurs don't count"
    assert coffee["Coffee, instant, made up with water"] == 100 and coffee["SILK Coffee, soymilk"] == 100
    assert coffee["Beverages, coffee, brewed, prepared with tap water, decaffeinated"] == 100, "decaf counts"
    named = {}
    for name, component, amount in con.execute("""
            SELECT f.name, c.component, c.amount_per_100g FROM reference_foods f JOIN food_components c USING (food)
            WHERE c.component IN ('fructans', 'gos', 'wheat_gluten')""").fetchall():
        named[(name, component)] = amount
    assert named[("Onions, raw", "fructans")] == 100 and named[("Onions, raw", "wheat_gluten")] == 0
    assert named[("Bread, gluten free", "wheat_gluten")] == 0, "gluten free"
    assert named[("Beans, green, raw", "gos")] == 0, "green beans have no GOS tag"
    assert named[("Carob flour", "wheat_gluten")] == 0, "not wheat flour"
    assert named[("Hummus, commercial", "gos")] == 100
    assert named[("Mung beans, mature seeds, raw", "gos")] == 100, "only mung bean sprouts are clear"
    assert named[("Mung beans, mature seeds, sprouted, raw", "gos")] == 0
    assert named[("Snacks, rice cakes, brown rice, rye", "wheat_gluten")] == 100, "a named grain still counts"
    assert named[("Snacks, rice cakes, brown rice, corn", "wheat_gluten")] == 0
    for name, card in (("Beef, New Zealand, imported, cube roll, separable lean only, raw", "wheat_gluten"),
                       ("Pie fillings, apple, canned", "wheat_gluten"), ("Cherry juice, tart", "wheat_gluten"),
                       ("Potato chips, oven ready, no batter, baked", "wheat_gluten"),
                       ("Burger, beef, 98-99% beef, grilled", "wheat_gluten"),
                       ("Bread, flat wrap or tortilla, corn, commercial", "wheat_gluten")):
        assert named.get((name, card), 0) == 0, name     # 0 or unknown, never tagged
    for name, card in (("Burger, Big Mac, takeaway", "wheat_gluten"), ("Burger, beef, with bun, grilled, homemade", "wheat_gluten"),
                       ("Sausage roll, commercial, ready to eat", "wheat_gluten"), ("Cod, in batter, baked", "wheat_gluten")):
        assert named[(name, card)] == 100, name


def test_real_own_figures_load():
    con = load(ROOT / "reference")
    for food, lactose in (("usda:173180", 6), ("usda:171268", 5.5), ("usda:171248", 2.7), ("usda:173420", 1.4),
                          ("usda:171284", 4.7), ("own:P012", 3.7)):
        assert value(con, food, "lactose") == lactose, food


def test_real_dishes_carry_the_tags_their_ingredients_should_give():
    con = load(ROOT / "reference")

    def tags(name):
        rows = con.execute("""SELECT t.tag FROM own_dishes d
                              JOIN dish_tags t ON t.food = coalesce(nullif(d.published, ''), 'own:' || d.food_id)
                              WHERE d.name = ?""", [name]).fetchall()
        return {r[0] for r in rows}

    assert {"milk", "wheat", "egg"} <= tags("Lasagne with beef")
    assert {"fish", "wheat"} <= tags("Fish and chips, battered fish, takeaway")
    assert "peanut" in tags("Kung pao pork")
    assert {"gos"} <= tags("Lentil soup")
    assert {"fish"} <= tags("Sushi roll, tuna")
    assert "milk" not in tags("Tabbouleh")
    assert "milk" in tags("Chicken curry")                   # from its published lactose, not its recipe
    assert "celery" in tags("Pho")                           # stock, not the water that stood in for it
    assert {"soy", "wheat"} <= tags("Sushi roll, tuna")      # served with it, not in USDA's recipe
    assert "peanut" in tags("Granola")                       # varies by brand
    assert {"wheat", "soy"} <= tags("Dumplings or wontons with meat, steamed")
    assert {"fish", "crustacean"} <= tags("Chicken curry with coconut milk (Thai green or red style)")
    assert {"peanut"} <= tags("Rice paper roll, prawn")
    assert {"coconut", "cashew", "peanut", "wheat", "soy", "fish", "crustacean"} <= tags(
        "Chicken curry with coconut milk (Thai green or red style)")
    assert "cashew" in tags("Granola")
    assert "milk" in tags("Chicken katsu curry")             # roux blocks hold milk powders and whey
    assert value(con, con.execute("""SELECT 'own:' || food_id FROM own_dishes
                                     WHERE name = 'Chicken katsu curry'""").fetchone()[0], "lactose") is None
    assert {"mustard"} <= tags("Aloo gobi (potato and cauliflower curry)")


def test_a_missing_or_misspelt_table_column_stops_the_load():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        path = ref / "food_tables/au_afcd_r3.csv"
        path.write_text(path.read_text().replace(",maltitol,", ",maltitoll,", 1))   # the chocolate's 28 g would vanish
        raises(duckdb.Error, load, ref)


def test_fresh_food_groups_must_cover_every_table():
    with tempfile.TemporaryDirectory() as tmp:
        ref = Path(tmp) / "ref"
        shutil.copytree(FIXTURE_REF, ref)
        path = ref / "fresh_food_groups.csv"
        path.write_text("".join(line for line in path.read_text().splitlines(True) if not line.startswith("uk,")))
        raises(duckdb.Error, load, ref)


def test_usda_repeated_category_stops_the_build():
    with tempfile.TemporaryDirectory() as tmp:
        d = Path(tmp)
        (d / "food_category.csv").write_text("id,code,description\n1,1800,Baked Products\n1,0900,Fruits and Fruit Juices\n")
        (d / "food_nutrient.csv").write_text("id,fdc_id,nutrient_id,amount\n")
        (d / "food.csv").write_text("fdc_id,data_type,description,food_category_id,publication_date\n9,sr,Bread,1,2019\n")
        raises(bft.BuildError, bft.read_usda, d)


def test_a_bad_value_stops_the_load():
    for bad in ("abc", "-1", "1e-2", " 4", "1000", "100.5", "Infinity", "NaN"):
        with tempfile.TemporaryDirectory() as tmp:
            ref = Path(tmp) / "ref"
            shutil.copytree(FIXTURE_REF, ref)
            path = ref / "food_tables/usda_sr_legacy.csv"
            path.write_text(path.read_text().replace(",4.8,", f",{bad},"))
            try:
                load(ref)
            except duckdb.Error:
                continue
            raise AssertionError(f"value {bad!r} was loaded")


def test_real_tables_load_with_the_expected_counts():
    con = load(ROOT / "reference")
    by_table = dict(con.execute("SELECT tbl, count(*) FROM reference_foods GROUP BY 1").fetchall())
    assert by_table == {"usda": 7793, "uk": 2887, "au": 1588, "own": 149}, by_table
    lactose = dict(con.execute("""
        SELECT split_part(food, ':', 1), count(*) FROM food_components WHERE component = 'lactose' GROUP BY 1
    """).fetchall())
    assert lactose["usda"] == 1757 and lactose["au"] == 1370, lactose   # 1,712 published + 45 own figures
    # UK: 2,574 numbers + 271 traces = 2,845, minus the per-100 ml drinks that have a value
    uk_expected = con.execute("""
        SELECT count(*) FROM reference_foods f JOIN read_csv(?, header = true, all_varchar = true) c
          ON f.food = 'uk:' || c.food_id
        WHERE f.per = '100g' AND c.lactose IS NOT NULL AND c.lactose <> 'N'
    """, [str(ROOT / "reference/food_tables/uk_cofid_2021.csv")]).fetchone()[0]
    assert lactose["uk"] == uk_expected, (lactose, uk_expected)
    uk_ml = con.execute("""SELECT count(*) FROM food_components c JOIN reference_foods f USING (food)
                           WHERE f.per = '100ml'""").fetchone()[0]
    assert uk_ml == 0, uk_ml
    polyols = dict(con.execute("""SELECT component, count(*) FROM food_components
                                  WHERE component IN ('sorbitol', 'mannitol') GROUP BY 1""").fetchall())
    assert polyols == {"sorbitol": 90, "mannitol": 17}, polyols
    named = dict(con.execute("""SELECT f.name, c.amount_per_100g FROM reference_foods f
                                LEFT JOIN food_components c ON c.food = f.food AND c.component = 'maltitol'
                                WHERE f.food IN ('usda:171688', 'usda:168210')""").fetchall())
    assert named == {"Apples, raw, with skin (Includes foods for USDA's Food Distribution Program)": 0,
                     "Guava nectar, with sucralose, canned": None}, named
    assert value(con, "uk:13-669#2", "lactose") == 0   # the repeated UK code: both foods kept


# --- The .xlsx reader on hostile files ---------------------------------------------------------------------

def test_real_density_files_load():
    con = load(ROOT / "reference")
    assert con.execute("SELECT count(*) FROM food_density").fetchone()[0] == 213
    for (pattern,) in con.execute("SELECT first_part FROM liquid_densities").fetchall():
        assert con.execute("SELECT count(*) FROM reference_foods WHERE tbl <> 'au' AND regexp_full_match("
                           "lower(trim(split_part(name, ',', 1))), ?)", [pattern]).fetchone()[0], pattern


def test_xlsx_limits_count_what_is_actually_unpacked():
    with tempfile.TemporaryDirectory() as tmp:
        ok, big = Path(tmp) / "ok.xlsx", Path(tmp) / "big.xlsx"
        workbook(ok, {"S": [["Food", "Lactose"], ["Milk", "4.7"]]})
        assert list(xlsx.Workbook(ok).rows("S")) == [["Food", "Lactose"], ["Milk", "4.7"]]
        workbook(big, {"S": [["a" * 5000]]})          # compresses to a few bytes, unpacks to 5 kB
        old = xlsx.MAX_MEMBER_BYTES
        xlsx.MAX_MEMBER_BYTES = 1024
        try:
            raises(xlsx.WorkbookError, xlsx.Workbook, big)
        finally:
            xlsx.MAX_MEMBER_BYTES = old
        raises(xlsx.WorkbookError, lambda: next(xlsx.Workbook(ok).rows("Missing")))


def test_xlsx_rejects_impossible_cell_positions():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wide.xlsx"
        workbook(path, {"S": [["x"]]})
        with zipfile.ZipFile(path) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        parts["xl/worksheets/sheet1.xml"] = parts["xl/worksheets/sheet1.xml"].replace(b'r="A1"', b'r="ZZZZZZ1"')
        with zipfile.ZipFile(path, "w") as z:
            for n, data in parts.items():
                z.writestr(n, data)
        raises(xlsx.WorkbookError, lambda: list(xlsx.Workbook(path).rows("S")))


def test_xlsx_refuses_entity_expansion():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "bomb.xlsx"
        workbook(path, {"S": [["x"]]})
        bomb = (b'<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaaaaaaaa">'
                + b"".join(f'<!ENTITY {c} "{("&" + p + ";") * 10}">'.encode()
                           for p, c in zip("abcdefgh", "bcdefghi"))
                + b']><sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>&i;</t></si></sst>')
        with zipfile.ZipFile(path) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        parts["xl/sharedStrings.xml"] = bomb
        with zipfile.ZipFile(path, "w") as z:
            for n, data in parts.items():
                z.writestr(n, data)
        start = time.perf_counter()
        wb = None
        try:
            wb = xlsx.Workbook(path)
        except xlsx.WorkbookError:   # refused: the expected outcome
            pass
        assert time.perf_counter() - start < 5
        if wb is not None:           # if it opened, the entities must not have been expanded
            assert max((len(s) for s in wb.shared), default=0) < 1_000_000


def test_xlsx_caps_cells_in_sparse_rows():
    # Each row has one cell in the last possible column: tiny file, huge rows if expanded without a budget.
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sparse.xlsx"
        workbook(path, {"S": [["x"]] * 400})
        with zipfile.ZipFile(path) as z:
            parts = {n: z.read(n) for n in z.namelist()}
        sheet = parts["xl/worksheets/sheet1.xml"].decode()
        for r in range(1, 401):
            sheet = sheet.replace(f'r="A{r}"', f'r="XFD{r}"', 1)
        parts["xl/worksheets/sheet1.xml"] = sheet.encode()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for n, data in parts.items():
                z.writestr(n, data)
        assert path.stat().st_size < 10_000
        raises(xlsx.WorkbookError, lambda: list(xlsx.Workbook(path).rows("S")))   # 400 × 16,384 > 5 M cells


def test_errors_never_repeat_the_bad_value():
    for bad in ("secret-ish text", "1" * 40, "12.5 g"):
        try:
            bft.clean_value(bad, "AFCD F1 lactose")
        except bft.BuildError as e:
            assert bad not in str(e) and "secret" not in str(e), str(e)
        else:
            raise AssertionError(f"accepted {bad!r}")


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_")]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} passed")
