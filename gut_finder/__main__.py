"""Command line: python -m gut_finder --food FOOD.csv --symptoms SYMPTOMS.csv
                                     --symptoms-from YYYY-MM-DD --symptoms-to YYYY-MM-DD [options]

Everything runs on this computer. The only file written is the report (readable only by you).
"""
import argparse
import csv
import io
import json
import os
import sys
from pathlib import Path

from gut_finder import engine, report, synthetic, wording

DEFAULT_OUT = engine.ROOT / "data" / "private" / "report.md"   # gitignored
# Printed with every error, so no problem with a file can ever hide the medical advice (D17).
SAFETY_REMINDER = wording.WHEN_TO_GET_HELP


SERVINGS_FILE = "servings_to_check.csv"
SIGNS_FILE = "danger_signs_to_confirm.csv"
CONSENT_FILE = Path.home() / ".gut_finder" / "consent.json"
CONSENT_VERSION = 1      # a new version asks again


def write_report(out, text, inputs, overwrite=False, suffix=".md"):
    """Write the report safely: a .md file, never a link, never one of the input files, private from the start.

    The text goes to a new private temporary file next to the target, which then replaces the target in one
    step, so an existing file's contents (or anything hard-linked to it) are never written into."""
    out = Path(out)
    if out.suffix.lower() != suffix:
        raise engine.InputError(f"--out must be a {suffix} file")
    if out.is_symlink():
        raise engine.InputError("--out points at a link; give a normal file path")
    target = out.resolve()
    if any(p and (Path(p).resolve() == target or (target.exists() and os.path.samefile(p, target)))
           for p in inputs):
        raise engine.InputError("--out would overwrite one of your input files")
    if target.exists() and not overwrite:
        raise engine.InputError("a report already exists there; add --overwrite to replace it")
    if engine.ROOT in target.parents and not (engine.ROOT / "data") in target.parents:
        raise engine.InputError("--out must be outside the code folders (use data/private/ or another folder)")
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        if overwrite:
            os.replace(tmp, target)
        else:
            try:
                os.link(tmp, target)   # fails if a file appeared there meanwhile: never replaces by accident
            except FileExistsError:
                raise engine.InputError("a report already exists there; add --overwrite to replace it") from None
    finally:
        if tmp.exists():
            tmp.unlink()   # only our own temporary file, if the replace didn't happen
    return target


def servings_rows(res):
    """The file's rows: pattern, unit, blank grams, entries, the grams this food was also logged in.

    A food's pattern is the longest run of its words found in no other food in the log, taken from the same list of
    phrases the matching uses (sql/05), so an answer reaches this food and no other. With no such run, it's left out."""
    phrases = {}
    for name, phrase in res.con.execute("""
            WITH names AS (SELECT DISTINCT lower(e.food_name) AS food, p.phrase
                           FROM entry_phrases p JOIN food_entries e USING (entry_id)),
                 only_one AS (SELECT phrase FROM names GROUP BY 1 HAVING count(*) = 1)
            SELECT n.food, n.phrase FROM names n JOIN only_one USING (phrase)
            ORDER BY n.food, length(n.phrase), n.phrase""").fetchall():
        phrases[name] = phrase                                     # sorted, so the longest ends up here
    rows = {}
    for name, unit, n, also_logged in report.servings_to_check(res):
        pattern = phrases.get(name.lower())
        if pattern and unit and len(unit) <= 60:
            key = (pattern, unit.strip().lower())
            if key in rows:                                        # the same name in different capitals
                rows[key][3] += n
            else:
                rows[key] = [*key, "", n, "" if also_logged is None else f"{also_logged:g}"]
    return list(rows.values())


def write_servings_file(rows, out, inputs, own_input=None):
    """Save the foods worth weighing as a file to fill in. Returns its name, or None if it wasn't saved.

    The columns --my-servings needs come first, with grams blank; rows left blank are ignored when it's read back.
    Answers already typed into an earlier file are kept. Failing here never stops the report."""
    path = Path(out).with_name(SERVINGS_FILE)
    kept = {}
    try:
        if path.is_file() and not path.is_symlink():
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if (r.get("grams") or "").strip():
                        kept[(r.get("pattern"), r.get("unit"))] = r
    except (OSError, UnicodeDecodeError, csv.Error):
        return None                                                # can't tell what's in it, so leave it alone
    fresh = [r for r in rows if (r[0], r[1]) not in kept]
    old = [[r.get("pattern"), r.get("unit"), r.get("grams"), r.get("entries", ""), r.get("also_logged_as_grams", "")]
           for r in kept.values()]
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\n")
    w.writerow([*engine.MY_SERVING_COLUMNS, "entries", "also_logged_as_grams"])
    w.writerows((old + fresh)[:engine.MAX_ROWS["My servings"]])    # the most the file can hold when it's read back
    try:
        return write_report(path, text.getvalue(), _other_inputs(inputs, own_input), overwrite=True, suffix=".csv").name
    except (engine.InputError, OSError):
        return None


def _other_inputs(inputs, own_input):
    """A fill-in file given as its own kind of input can be refreshed, because the answers in it are kept. Every other
    input stays protected, so a symptoms file that happens to have the same name is never replaced."""
    others = [p for p in inputs if p]
    if own_input in others:
        others.remove(own_input)     # once only: the same file given as another input stays protected
    return others


def write_fill_in_file(name, columns, rows, out, inputs, limit, own_input=None):
    """Save a file to fill in, next to the report. The second column is the answer, and answers already typed are kept.
    Returns the file's name, or None if it wasn't saved. Failing here never stops the report."""
    path = Path(out).with_name(name)
    kept = {}
    try:
        if path.is_file() and not path.is_symlink():
            with open(path, newline="", encoding="utf-8-sig") as f:
                for r in csv.DictReader(f):
                    if (r.get(columns[1]) or "").strip():
                        kept[r.get(columns[0])] = [r.get(c, "") for c in columns]
    except (OSError, UnicodeDecodeError, csv.Error):
        return None                                                # can't tell what's in it, so leave it alone
    text = io.StringIO()
    w = csv.writer(text, lineterminator="\n")
    w.writerow(columns)
    w.writerows((list(kept.values()) + [r for r in rows if r[0] not in kept])[:limit])
    try:
        return write_report(path, text.getvalue(), _other_inputs(inputs, own_input), overwrite=True, suffix=".csv").name
    except (engine.InputError, OSError):
        return None


def has_agreed():
    try:
        with open(CONSENT_FILE, encoding="utf-8") as f:
            return json.load(f).get("version") == CONSENT_VERSION
    except (OSError, ValueError, AttributeError):
        return False


def agree_once():
    """Consent at set-up: shown once, on its own screen. No agreement, no use."""
    if has_agreed():
        return
    print(wording.CONSENT_SCREEN)
    if not sys.stdin.isatty():
        raise engine.InputError(wording.CONSENT_NEEDS_TERMINAL)
    if input(wording.CONSENT_ASK).strip().lower() != "i agree":
        raise engine.InputError(wording.CONSENT_REFUSED)
    CONSENT_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONSENT_FILE.with_name(f".{CONSENT_FILE.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump({"version": CONSENT_VERSION}, f)
        os.replace(tmp, CONSENT_FILE)       # replaces a link itself, never the file it points at
    finally:
        if tmp.exists():
            tmp.unlink()


def _fail(message, code, res):
    """Every failure prints the medical advice, and says so plainly if a danger sign was already found."""
    print(message, file=sys.stderr)
    if res is not None and res.red_flags:   # a danger sign was found: show its advice here instead
        print(wording.RED_FLAG_INTRO + " The report couldn't be saved, so here is the advice:", file=sys.stderr)
        for line in report.red_flag_advice(res.red_flags):
            if line:
                print(line.replace("*", ""), file=sys.stderr)
    else:
        print(SAFETY_REMINDER, file=sys.stderr)
    return code


def main(argv=None):
    p = argparse.ArgumentParser(prog="python -m gut_finder",
                                description="Find food components worth testing, from your own logs. Not medical advice.")
    p.add_argument("--food", help="Cronometer 'Food & Recipe Entries' export (CSV)")
    p.add_argument("--symptoms", help="your symptoms CSV: date, time, symptom, severity (see examples/)")
    p.add_argument("--symptoms-from", help="first day you logged every symptom (YYYY-MM-DD)")
    p.add_argument("--symptoms-to", help="last day you logged every symptom (YYYY-MM-DD)")
    p.add_argument("--my-foods", help="optional: grams per 100 g from your product labels (see examples/)")
    p.add_argument("--my-servings", help="optional: what one unit of a food weighs, e.g. one scoop (see examples/)")
    p.add_argument("--exclude-days", help="optional: days to leave out, e.g. illness or travel (see examples/)")
    p.add_argument("--out", default=str(DEFAULT_OUT), help=f"where to write the report (default: {DEFAULT_OUT})")
    p.add_argument("--overwrite", action="store_true", help="replace an existing report at --out")
    p.add_argument("--demo", action="store_true", help="run on synthetic data with test-only foods")
    p.add_argument("--my-matches", help="optional: correct a food match yourself (see examples/my_matches_template.csv)")
    p.add_argument("--checked-signs", help="optional: danger signs a doctor or nurse has checked, a filled-in "
                                           "danger_signs_to_confirm.csv")
    p.add_argument("--find-food", metavar="WORDS", help="look up the table foods closest to these words, then stop")
    p.add_argument("--match-cache", default=None,
                   help="where saved matches are kept (default: ~/.gut_finder/food_matches.json)")
    a = p.parse_args(argv)

    res = None
    try:
        if a.find_food:
            for name, food in engine.find_foods(a.find_food):
                print(f"{name}  ({food})")
            return 0
        if a.demo:
            demo_dir = engine.ROOT / "data" / "synthetic"
            demo_dir.mkdir(parents=True, exist_ok=True)
            food, symptoms = synthetic.generate(seed=7, plant="sorbitol")
            synthetic.write(food, symptoms, demo_dir / "demo_food_log.csv", demo_dir / "demo_symptoms.csv")
            inputs = [demo_dir / "demo_food_log.csv", demo_dir / "demo_symptoms.csv"]
            res = engine.run(*inputs, symptoms_from=food[0][0], symptoms_to=food[-1][0],
                             reference_dir=engine.ROOT / "tests" / "fixtures" / "ref")
        else:
            if not (a.food and a.symptoms):
                p.error("--food and --symptoms are required (or use --demo)")
            agree_once()
            inputs = [a.food, a.symptoms, a.my_foods, a.my_servings, a.exclude_days, a.my_matches, a.checked_signs]
            res = engine.run(a.food, a.symptoms, a.my_foods, a.exclude_days, a.symptoms_from, a.symptoms_to,
                             my_servings_csv=a.my_servings, my_matches_csv=a.my_matches,
                             checked_signs_csv=a.checked_signs, online_matching=True, match_cache=a.match_cache)
        try:
            to_weigh = [] if a.demo or res.con is None else servings_rows(res)
            signs = [] if a.demo else [[engine.spreadsheet_safe(e), ""]
                                       for e in engine.flagged_entries(a.symptoms, res.red_flags)]
            text = report.build(res, demo=a.demo, servings_file=SERVINGS_FILE if to_weigh else None,
                                signs_file=SIGNS_FILE if signs else None)
            target = write_report(a.out, text, inputs, a.overwrite)      # first: a refused run changes nothing
            if signs and write_fill_in_file(SIGNS_FILE, engine.CHECKED_SIGN_COLUMNS, signs, a.out, inputs,
                                            engine.MAX_ROWS["Checked signs"], a.checked_signs) is None:
                print(f"{SIGNS_FILE} couldn't be saved, so the report stays stopped. Please try again.")
            if to_weigh and write_servings_file(to_weigh, a.out, inputs, a.my_servings) is None:
                print(f"{SERVINGS_FILE} couldn't be saved, so the report's list of foods to weigh is all there is.")
        finally:
            if res.con is not None:
                res.con.close()   # drop the in-memory copy of the logs straight away
    except engine.InputError as e:
        return _fail(f"Can't run: {e}", 2, res)
    except engine.ConfigError as e:
        return _fail(f"Setup problem, nothing was analysed: {e}", 3, res)
    except OSError as e:   # e.g. no permission to write the report
        return _fail(f"Can't run: a file problem ({type(e).__name__})", 4, res)

    print("Report written to " + "".join(ch if ch.isprintable() else "?" for ch in str(target)))
    if res.red_flags:
        print("Your symptom log includes a sign that needs medical advice. Please read the report.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
