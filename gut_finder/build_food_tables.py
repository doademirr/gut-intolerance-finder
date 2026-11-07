"""Turn the downloaded food composition tables into the CSVs in reference/food_tables/ (D14, D18).

  python -m gut_finder.build_food_tables --usda DIR --cofid FILE.xlsx --afcd FILE.xlsx [--out DIR]

  --usda   the unzipped USDA FoodData Central SR Legacy CSV folder (food.csv, food_nutrient.csv, food_category.csv)
  --cofid  UK CoFID 2021: "McCance and Widdowson's composition of foods integrated dataset" (.xlsx)
  --afcd   Australia AFCD Release 3: "Nutrient profiles" (.xlsx)

Each output file keeps the values exactly as published ("Tr" for trace, "N" for not measured, blank when the
table has no value), plus each food's group as the table publishes it (a USDA category, a CoFID group code,
an AFCD classification code). The rules for reading them live in sql/03_load_reference.sql, in one place:
"Tr" counts as 0, "N" and blank stay unknown, and per-100 ml rows stay unknown.
"""
import argparse
import csv
import re
import sys
from decimal import Decimal
from pathlib import Path

from gut_finder.xlsx import Workbook, WorkbookError

COLUMNS = ["food_id", "name", "food_group", "lactose", "sorbitol", "mannitol", "maltitol", "xylitol",
           "erythritol", "fructose", "glucose", "per"]
EMPTY = {"sorbitol": "", "mannitol": "", "maltitol": "", "xylitol": "", "erythritol": ""}
MAX_GROUP_CHARS = 80
# Nutrient ids in FoodData Central
USDA_NUTRIENTS = {"1013": "lactose", "1012": "fructose", "1011": "glucose"}
# Non-overlapping parts, so a long bad value can't make the match slow; length is capped first anyway.
NUMBER = re.compile(r"(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][-+]?[0-9]{1,3})?")
MAX_VALUE_CHARS = 30
MAX_GRAMS = 100                # grams per 100 g: no component can weigh more than the food
MAX_FOODS = 50_000             # per table (the real ones have 1,588–7,793)
MAX_CSV_BYTES = 500 * 1024 * 1024
MAX_NAME_CHARS = 300


class BuildError(ValueError):
    pass


def clean_value(text, where):
    """Keep a published value as text: a plain decimal, "Tr", "N" or "" (no value). Anything else stops.

    Numbers become the shortest plain decimal that stands for exactly the same stored number, so nothing is
    rounded away (0.0000001 stays 0.0000001) and Excel's binary storage noise is dropped (Excel stores the
    published 1.1 as 1.1000000000000001; both are the same number, and 1.1 is what the table shows)."""
    v = (text or "").strip()
    if v in ("", "Tr", "N"):
        return v
    if len(v) > MAX_VALUE_CHARS or not NUMBER.fullmatch(v):
        raise BuildError(f"{where}: a value isn't a number, Tr or N")
    d = Decimal(repr(float(v)))
    if d > MAX_GRAMS:
        raise BuildError(f"{where}: more than {MAX_GRAMS} g per 100 g")
    out = format(d.normalize(), "f")
    out = "0" if Decimal(out) == 0 else out
    if len(out) > MAX_VALUE_CHARS:   # same limit the loader checks (sql/03), so a built file always loads
        raise BuildError(f"{where}: a value has more decimal places than any food table uses")
    return out


def clean_name(text):
    name = " ".join((text or "").split())
    if not name or len(name) > MAX_NAME_CHARS:
        raise BuildError(f"a food name is empty or longer than {MAX_NAME_CHARS} characters")
    return name


def clean_group(text, label):
    group = " ".join(str(text or "").split())
    if len(group) > MAX_GROUP_CHARS or not re.fullmatch(r"[A-Za-z0-9 ,&/()'-]*", group):
        raise BuildError(f"{label}: a food group isn't plain text of up to {MAX_GROUP_CHARS} characters")
    return group


def clean_id(text, label):
    food_id = (text or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9-]{1,20}", food_id):
        raise BuildError(f"{label}: a food id isn't 1–20 letters, digits or hyphens")
    return food_id


def _unique(rows, label):
    if len(rows) > MAX_FOODS:
        raise BuildError(f"{label}: more than {MAX_FOODS:,} foods")
    seen = set()
    for r in rows:
        if r["food_id"] in seen:
            raise BuildError(f"{label}: food id {r['food_id']} appears twice")
        seen.add(r["food_id"])
    return rows


def _open_csv(path, label):
    path = Path(path)
    try:
        if not path.is_file() or path.stat().st_size > MAX_CSV_BYTES:
            raise BuildError(f"{label}: {path.name} is missing, not a file, or too large")
        return open(path, newline="", encoding="utf-8")
    except OSError:
        raise BuildError(f"{label}: {path.name} can't be read") from None


def read_usda(folder):
    folder = Path(folder)
    values = {c: {} for c in USDA_NUTRIENTS.values()}
    with _open_csv(folder / "food_nutrient.csv", "USDA") as f:
        for r in csv.DictReader(f):
            component = USDA_NUTRIENTS.get(r.get("nutrient_id"))
            if component:
                fdc = clean_id(r.get("fdc_id"), "USDA")
                values[component][fdc] = clean_value(r.get("amount"), f"USDA {fdc} {component}")
                if len(values[component]) > MAX_FOODS:
                    raise BuildError(f"USDA: more than {MAX_FOODS:,} {component} values")
    categories = {}
    with _open_csv(folder / "food_category.csv", "USDA") as f:
        for r in csv.DictReader(f):
            cid = clean_id(r.get("id"), "USDA category")
            if cid in categories:          # a repeat would silently re-label every food in that category
                raise BuildError(f"USDA: food category {cid} appears twice")
            categories[cid] = clean_group(r.get("description"), "USDA")
            if len(categories) > 1000:
                raise BuildError("USDA: more than 1,000 food categories")
    out = []
    with _open_csv(folder / "food.csv", "USDA") as f:
        for r in csv.DictReader(f):
            fdc = clean_id(r.get("fdc_id"), "USDA")
            out.append({"food_id": fdc, "name": clean_name(r.get("description")),
                        "food_group": categories.get((r.get("food_category_id") or "").strip(), ""),
                        **{c: v.get(fdc, "") for c, v in values.items()}, **EMPTY, "per": "100g"})
            if len(out) > MAX_FOODS:
                raise BuildError(f"USDA: more than {MAX_FOODS:,} foods")
    return _unique(out, "USDA")


def _find(header, starts):
    for i, h in enumerate(header):
        if h and " ".join(h.split()).lower().startswith(starts.lower()):
            return i
    raise BuildError(f"no column starting {starts!r}")


def _sheet_rows(path, sheet, label):
    rows = []
    for r in Workbook(path).rows(sheet):
        rows.append(r)
        if len(rows) > MAX_FOODS + 10:
            raise BuildError(f"{label}: more than {MAX_FOODS:,} foods")
    if not rows:
        raise BuildError(f"{label}: the sheet {sheet!r} is empty")
    return rows


def read_cofid(path):
    rows = _sheet_rows(path, "1.3 Proximates", "CoFID")
    head = rows[0]
    code, name, group = _find(head, "Food Code"), _find(head, "Food Name"), _find(head, "Group")
    cols = {c: _find(head, c) for c in ("lactose", "fructose", "glucose")}
    out = []
    for r in rows[3:]:                       # rows 1–3 are headings (CoFID user guide)
        cell = lambda i: r[i] if i < len(r) else None  # noqa: E731
        if not cell(code):
            continue
        food_id = clean_id(cell(code), "CoFID")
        # The user guide: values are per 100 g, except alcoholic beverages (groups Q…), per 100 ml.
        per = "100ml" if (cell(group) or "").strip().startswith("Q") else "100g"
        out.append({"food_id": food_id, "name": clean_name(cell(name)),
                    "food_group": clean_group(cell(group), "CoFID"),
                    **{c: clean_value(cell(i), f"CoFID {food_id} {c}") for c, i in cols.items()},
                    **EMPTY, "per": per})
    # The published file gives two different foods the same code (13-669: roasted aubergine and raw
    # watercress). Codes that repeat get their order in the sheet added (13-669#1, 13-669#2).
    counts = {}
    for r in out:
        counts[r["food_id"]] = counts.get(r["food_id"], 0) + 1
    seen = {}
    for r in out:
        if counts[r["food_id"]] > 1:
            seen[r["food_id"]] = seen.get(r["food_id"], 0) + 1
            r["food_id"] = f"{r['food_id']}#{seen[r['food_id']]}"
    return _unique(out, "CoFID")


def read_afcd(path):
    rows = _sheet_rows(path, "All solids & liquids per 100 g", "AFCD")
    hi = next((i for i, r in enumerate(rows[:10]) if r and r[0] and r[0].startswith("Public Food Key")), None)
    if hi is None:
        raise BuildError("AFCD: no heading row starting 'Public Food Key' in the first 10 rows")
    head = rows[hi]
    key, name, group = _find(head, "Public Food Key"), _find(head, "Food Name"), _find(head, "Classification")
    cols = {c: _find(head, c) for c in ("lactose", "sorbitol", "mannitol", "maltitol", "xylitol", "erythritol",
                                        "fructose", "glucose")}
    out = []
    for r in rows[hi + 1:]:
        cell = lambda i: r[i] if i < len(r) else None  # noqa: E731
        if not cell(key):
            continue
        food_id = clean_id(cell(key), "AFCD")
        row = {"food_id": food_id, "name": clean_name(cell(name)),
               "food_group": clean_group(cell(group), "AFCD"), "per": "100g"}
        for c, i in cols.items():
            row[c] = clean_value(cell(i), f"AFCD {food_id} {c}")
        out.append(row)
    return _unique(out, "AFCD")


def read_afcd_density(path):
    """Published specific gravity of each liquid. The table gives solids 0, so they're left out."""
    rows = _sheet_rows(path, "Food details", "AFCD details")
    hi = next((i for i, r in enumerate(rows[:10]) if r and r[0] and r[0].startswith("Public Food Key")), None)
    if hi is None:
        raise BuildError("AFCD details: no heading row starting 'Public Food Key' in the first 10 rows")
    key = _find(rows[hi], "Public Food Key")
    named = [i for i, h in enumerate(rows[hi]) if h and h.strip().lower() == "specific gravity"]
    if len(named) != 1:
        raise BuildError("AFCD details: expected exactly one 'Specific Gravity' column")
    gravity = named[0]
    out, seen = [], set()
    for r in rows[hi + 1:]:
        cell = lambda i: r[i] if i < len(r) else None  # noqa: E731
        if not cell(key):
            continue
        food_id = clean_id(cell(key), "AFCD details")
        if food_id in seen:                 # checked before solids are dropped, so a liquid and solid can't clash
            raise BuildError(f"AFCD details: {food_id} appears twice")
        seen.add(food_id)
        if cell(gravity) in (None, ""):
            continue
        try:
            value = float(cell(gravity))
        except ValueError:
            raise BuildError(f"AFCD details {food_id}: specific gravity isn't a number") from None
        if value == 0:
            continue
        if not 0 < value <= 2:
            raise BuildError(f"AFCD details {food_id}: specific gravity out of range")
        out.append({"food_id": food_id, "specific_gravity": f"{value:g}"})
    return _unique(out, "AFCD details")


def write(rows, path, columns=COLUMNS):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=columns, lineterminator="\n")
        w.writeheader()
        w.writerows(sorted(rows, key=lambda r: r["food_id"]))


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--usda", required=True)
    p.add_argument("--cofid", required=True)
    p.add_argument("--afcd", required=True)
    p.add_argument("--afcd-details", required=True)
    p.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "reference" / "food_tables"))
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    try:
        tables = {"usda_sr_legacy.csv": read_usda(a.usda), "uk_cofid_2021.csv": read_cofid(a.cofid),
                  "au_afcd_r3.csv": read_afcd(a.afcd)}
        density = read_afcd_density(a.afcd_details)
    except (BuildError, WorkbookError) as e:
        sys.exit(f"Couldn't build the food tables: {e}")
    except (OSError, KeyError, UnicodeDecodeError, csv.Error):
        sys.exit("Couldn't build the food tables: a download is missing, unreadable or not in the expected format")
    for filename, rows in tables.items():
        write(rows, out / filename)
        print(f"{filename}: {len(rows)} foods")
    write(density, out / "au_afcd_r3_density.csv", ["food_id", "specific_gravity"])
    print(f"au_afcd_r3_density.csv: {len(density)} liquids")


if __name__ == "__main__":
    main()
