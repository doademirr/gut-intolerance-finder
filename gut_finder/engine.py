"""Load the user's files, check them, and run the SQL steps (D2, D8, D13, D17).

Python only moves data in and checks it; the logic lives in sql/. Everything runs in an in-memory
DuckDB with spilling to disk switched off, so nothing is written except the report.

Every check fails closed: a file that can't be read exactly as expected stops the run with a message
naming the file, row and column (never the private value itself).
"""
import csv
import os
import re
import stat
import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb

from gut_finder import matching, search, wording

ROOT = Path(__file__).resolve().parent.parent
SQL_DIR = ROOT / "sql"
PATTERN = re.compile(r"[a-z0-9]+( [a-z0-9]+){0,4}")   # 1–5 words, same rule as food_aliases (sql/03)

FOOD_COLUMNS = ["Day", "Time", "Group", "Food Name", "Amount", "Category"]
SYMPTOM_COLUMNS = ["date", "time", "symptom", "severity"]
MY_FOOD_COLUMNS = ["pattern", "component", "grams_per_100g"]
MY_SERVING_COLUMNS = ["pattern", "unit", "grams"]
MY_MATCH_COLUMNS = ["your_food", "correct_food"]
CHECKED_SIGN_COLUMNS = ["entry", "checked"]
EXCLUDE_COLUMNS = ["date", "reason"]

# Size limits (Codex security review): far above a real log, far below what could exhaust memory.
MAX_FILE_BYTES = 50 * 1024 * 1024
MAX_ROWS = {"Food log": 50_000, "Symptoms": 20_000, "My foods": 300, "My servings": 300, "Excluded days": 2_000,
            "My matches": 5_000, "Checked signs": 500}
MAX_DAYS = 3 * 366   # a food log longer than 3 years is split by the user

# The safety gate needs these red-flag codes in reference/symptom_words.csv (D17, G14).
REQUIRED_RED_FLAGS = {"blood_in_stool", "black_stool", "weight_loss", "lump", "anaemia_signs", "vomiting_blood"}
# These phrases must be in the list and marked red_flag=yes, so an edited row can't switch a danger sign off.
REQUIRED_RED_FLAG_PHRASES = {"blood": "blood_in_stool", "bleeding": "blood_in_stool",
                             "rectal bleeding": "blood_in_stool", "black stool": "black_stool",
                             "black stools": "black_stool", "black poo": "black_stool",
                             "weight loss": "weight_loss", "lump": "lump", "vomiting blood": "vomiting_blood",
                             "blood in vomit": "vomiting_blood", "palpitations": "anaemia_signs"}
MAX_REFERENCE_BYTES = 5 * 1024 * 1024
MAX_SYMPTOM_BYTES = 5 * 1024 * 1024    # 20,000 rows is well under 1 MB
MAX_VOCABULARY = 1_000                 # symptom phrases, each 1–5 words
# Text that isn't a known symptom but contains one of these is treated as a red flag, not a typo.
RED_FLAG_STEMS = ("blood", "bleed", "black", "tarry", "darkred", "weight", "lump", "swell", "palpitat", "breath",
                  "pale", "vomit")
# Phrases containing these stems must carry a red-flag code (checked when the list loads and on every match),
# so an edited row can't quietly turn a danger sign into an ordinary symptom. ("vomiting" alone is ordinary.)
CONFIG_STEMS = tuple(s for s in RED_FLAG_STEMS if s != "vomit")
ALLOWED_CHARS = re.compile(r"[a-z0-9 \-'.!?()]*")


class InputError(ValueError):
    """A user file can't be used. The message says which file, row and column, and what to fix."""


class ConfigError(RuntimeError):
    """The repo's own reference files are missing or broken. Nothing is analysed."""


@dataclass
class RunResult:
    con: duckdb.DuckDBPyConnection
    red_flags: list = field(default_factory=list)   # (row, code)


def _fold(text):
    """Fold look-alike Unicode (ｂ → b), drop invisible characters, strip accents (é → e), lower-case."""
    text = unicodedata.normalize("NFKD", unicodedata.normalize("NFKC", text))
    return "".join(ch for ch in text if unicodedata.category(ch) not in ("Cf", "Mn")).lower()


def normalise(text):
    """Lower-case words only: folds look-alike Unicode, drops invisible characters and punctuation."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", _fold(text)).split())


def only_plain_words(text):
    """True if the text is plain English words and simple punctuation: no emoji, symbols or other scripts."""
    return bool(ALLOWED_CHARS.fullmatch(" ".join(_fold(text).split())))


def signs_from_stems(text):
    """Sort danger words into every sign they could describe, so a misspelling or an unlisted phrase still
    gets advice with the right urgency (D17). When in doubt, include the sign rather than leave it out:
    "threw up black stuff" gets both the vomiting-blood and the black-poo advice."""
    s = normalise(text).replace(" ", "")
    signs = set()
    blood = any(w in s for w in ("blood", "bleed", "clot"))
    dark = any(w in s for w in ("black", "tarry", "darkred"))
    vomit = any(w in s for w in ("vomit", "sick", "throw", "threw", "puke", "retch"))
    if vomit and (blood or dark or "brown" in s or "coffee" in s):
        signs.add("vomiting_blood")   # NHS: vomited blood can look red, brown, black or like coffee granules
    if blood:
        signs.add("blood_in_stool")
    if dark:
        signs.add("black_stool")
    if "weight" in s:
        signs.add("weight_loss")
    if "lump" in s or "swell" in s:
        signs.add("lump")
    if any(w in s for w in ("palpitat", "breath", "pale")):
        signs.add("anaemia_signs")
    return signs


STRUCTURE_WORDS = {"date", "time", "symptom", "severity"}


def is_exact(segment, vocab):
    """True if a piece of the file is only list words, or only structure (dates, times, numbers, headers)."""
    words = normalise(segment).split()
    if all(w.isdigit() or w in STRUCTURE_WORDS for w in words):
        return True
    covered = set()
    matches = [(n, i) for i in range(len(words)) for n in range(1, 6)
               if i + n <= len(words) and " ".join(words[i:i + n]) in vocab]
    for n, i in sorted(matches, key=lambda m: -m[0]):
        span = set(range(i, i + n))
        if not span & covered:
            covered |= span
    return covered == set(range(len(words)))


def red_signs(segment, vocab):
    """The danger signs in one listed symptom. A phrase exactly on the list gets exactly its own sign (show only
    the matching advice). Anything else, including an edited list, falls back to the stem
    check above, which errs towards including advice."""
    words = normalise(segment).split()
    matches = [(n, i, " ".join(words[i:i + n])) for i in range(len(words)) for n in range(1, 6)
               if i + n <= len(words) and " ".join(words[i:i + n]) in vocab]
    covered, red_covered, red_codes = set(), set(), set()
    for n, i, phrase in sorted(matches, key=lambda m: -m[0]):   # longest first, by position
        span = set(range(i, i + n))
        if not span & covered:
            covered |= span
            code, is_flag = vocab[phrase]
            if is_flag:
                red_codes.add(code)
                red_covered |= span
    if words and red_covered == set(range(len(words))):
        return red_codes   # every word belongs to a danger-sign phrase on the list: exact advice
    found = red_codes | signs_from_stems(segment)   # own words or a combination: err towards more
    return found | {"emergency_check"} if found else found


def has_stem(phrase, stems):
    squashed = phrase.replace(" ", "")
    return any(stem in squashed for stem in stems)


def _open_regular(path, name):
    """Open a file for reading without ever blocking, and refuse anything that isn't a regular file
    (pipes, devices), so a strange path can't hang the run or feed it endless data."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        raise InputError(f"{name}: file not found or not readable") from None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InputError(f"{name}: not a regular file")
        if info.st_size > MAX_FILE_BYTES:
            raise InputError(f"{name}: file is larger than {MAX_FILE_BYTES // (1024 * 1024)} MB")
    except OSError:
        os.close(fd)
        raise InputError(f"{name}: file not found or not readable") from None
    except InputError:
        os.close(fd)
        raise
    return fd


def _read(path, name, required, optional=(), allow_extra=False, skip_lines=frozenset()):
    """Read a CSV strictly: known columns once each, every row the same width, size limits.
    skip_lines: lines of the file to leave out, as confirmed danger signs are (D20)."""
    fd = _open_regular(path, name)   # closes the file itself on any problem
    try:
        with os.fdopen(fd, newline="", encoding="utf-8-sig") as f:
            reader = csv.reader(f, strict=True)
            header = [h.strip() for h in next(reader, [])]
            if len(set(header)) != len(header):
                raise InputError(f"{name}: a column name appears twice")
            missing = [c for c in required if c not in header]
            if missing:
                raise InputError(f"{name}: missing column(s) {missing}")
            extra = [c for c in header if c not in (*required, *optional)]
            if extra and not allow_extra:
                raise InputError(f"{name}: {len(extra)} unexpected column(s); expected only {[*required, *optional]}")
            rows = []
            for line_no, cells in enumerate(reader, start=2):
                if not any(c.strip() for c in cells) or reader.line_num in skip_lines:
                    continue   # blank line
                if len(cells) != len(header):
                    raise InputError(f"{name}, row {line_no}: {len(cells)} cells, expected {len(header)}")
                row = dict(zip(header, (c.strip() for c in cells)))
                rows.append({"_row": line_no, **{c: row.get(c, "") for c in (*required, *optional)}})
                if len(rows) > MAX_ROWS[name]:
                    raise InputError(f"{name}: more than {MAX_ROWS[name]:,} rows")
    except (csv.Error, UnicodeDecodeError):
        raise InputError(f"{name}: not a readable UTF-8 CSV file") from None
    return rows


def _date(value, name, row, column):
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise InputError(f"{name}, row {row}, column '{column}': not a date like 2026-01-31") from None


def read_food_log(path):
    rows = _read(path, "Food log", FOOD_COLUMNS[:5], FOOD_COLUMNS[5:], allow_extra=True)
    if not rows:
        raise InputError("Food log: the file has no entries")
    days = set()
    for r in rows:
        days.add(_date(r["Day"], "Food log", r["_row"], "Day"))
        if r["Time"]:
            try:
                datetime.strptime(r["Time"].upper(), "%I:%M %p")
            except ValueError:
                raise InputError(f"Food log, row {r['_row']}, column 'Time': not a time like 2:21 PM "
                                 f"(leave it empty if unknown)") from None
    if (max(days) - min(days)).days + 1 > MAX_DAYS:
        raise InputError(f"Food log: covers more than {MAX_DAYS} days; please upload up to 3 years at a time")
    return rows, min(days), max(days)


def load_vocabulary(reference_dir):
    path = Path(reference_dir) / "symptom_words.csv"
    try:
        if path.stat().st_size > MAX_REFERENCE_BYTES:
            raise ConfigError("reference/symptom_words.csv is too large: the safety check can't run")
        with open(path, newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
    except (OSError, csv.Error, UnicodeDecodeError):
        raise ConfigError("reference/symptom_words.csv is missing or unreadable: the safety check can't run") from None
    vocab = {}
    for r in rows:
        phrase, code, flag = normalise(r.get("phrase") or ""), (r.get("code") or "").strip(), r.get("red_flag")
        if not phrase or not code or flag not in ("yes", "no"):
            raise ConfigError("reference/symptom_words.csv has an incomplete row: the safety check can't run")
        if phrase in vocab:
            raise ConfigError("reference/symptom_words.csv lists a phrase twice: the safety check can't run")
        if len(phrase.split()) > 5 or len(vocab) >= MAX_VOCABULARY:
            raise ConfigError("reference/symptom_words.csv: phrases are 1–5 words, at most 1,000 of them")
        is_flag = flag == "yes"
        if is_flag != (code in REQUIRED_RED_FLAGS):
            raise ConfigError("reference/symptom_words.csv: red_flag must be yes exactly for the red-flag codes")
        if has_stem(phrase, CONFIG_STEMS) and not is_flag:
            raise ConfigError("reference/symptom_words.csv: a phrase that names a danger sign isn't marked as one")
        vocab[phrase] = (code, is_flag)
    present = {code for code, flag in vocab.values() if flag}
    if not REQUIRED_RED_FLAGS <= present:
        raise ConfigError(f"reference/symptom_words.csv lacks red-flag codes {sorted(REQUIRED_RED_FLAGS - present)}")
    wrong = sorted(p for p, code in REQUIRED_RED_FLAG_PHRASES.items() if vocab.get(p) != (code, True))
    if wrong:
        raise ConfigError(f"reference/symptom_words.csv must keep these danger signs as they are: {wrong}")
    return vocab


def scan_for_red_flags(path, vocab, checked=frozenset()):
    """Look for danger signs anywhere in the symptoms file, line by line, before any other check (D17).

    Runs first and tolerates a broken file, so the medical advice is never hidden behind an input error.
    checked: entries the user confirmed have been seen by a doctor or nurse (D20). Only those exact lines are left out."""
    try:
        fd = _open_regular(path, "Symptoms")
    except InputError:
        return []   # the normal reader reports the problem; with no content there is nothing to scan
    try:
        with os.fdopen(fd, "rb") as f:
            data = f.read(MAX_SYMPTOM_BYTES + 1)      # never more than the limit, whatever the file claims
    except OSError:
        raise InputError("Symptoms: the file couldn't be read") from None
    too_big = len(data) > MAX_SYMPTOM_BYTES
    text = data[:MAX_SYMPTOM_BYTES].decode("utf-8-sig", errors="replace")
    skip = _checked_lines(text, checked)
    lines = ["" if n in skip else line for n, line in enumerate(_physical_lines(text), start=1)]
    text = "\n".join(lines)
    flags, any_free = [], False
    for line_no, line in enumerate(lines, start=1):
        codes = set()
        for segment in re.split(r"[;,/]", unicodedata.normalize("NFKC", line)):   # one cell or symptom at a time
            codes |= red_signs(segment, vocab)
            any_free = any_free or not is_exact(segment, vocab)
        whole = signs_from_stems(line)
        if "vomiting_blood" in whole and "vomiting_blood" not in codes:
            codes |= {"vomiting_blood", "emergency_check"}   # vomiting and blood/black split by a separator
        if not codes and has_stem(normalise(line).replace(" ", ""), CONFIG_STEMS):
            codes = {"unrecognised_red_flag"}
        flags += [(line_no, code) for code in sorted(codes)]
    # Across the whole file: entries can span lines, and free text anywhere means the file can't be read
    # precisely, so any danger sign also gets the emergency check. Over-including advice is the safe side.
    if flags or signs_from_stems(text):
        first = flags[0][0] if flags else 1
        have, extra = {c for _, c in flags}, set()
        if "vomiting_blood" in signs_from_stems(text) and "vomiting_blood" not in have:
            extra |= {"vomiting_blood", "emergency_check"}   # vomiting and blood/black on different lines
        on_one_line = set().union(*(signs_from_stems(line) for line in lines))
        extra |= signs_from_stems(text) - on_one_line        # a sign split across lines, found nowhere else
        if any_free and flags:
            extra.add("emergency_check")
        flags += [(first, c) for c in sorted(extra - have)]
    if too_big and not flags:   # danger signs found in the first 5 MB still win over the size error
        raise InputError(f"Symptoms: file is larger than {MAX_SYMPTOM_BYTES // (1024 * 1024)} MB")
    return flags


def read_symptoms(path, vocab, skip_lines=frozenset()):
    """Each symptom must be a known word (see examples/). Returns rows with a symptom code, plus red flags.
    Red flags are returned even if other rows have problems: the advice must never be hidden."""
    rows = _read(path, "Symptoms", SYMPTOM_COLUMNS[:3], SYMPTOM_COLUMNS[3:], skip_lines=skip_lines)
    out, flags, problems = [], [], []
    for r in rows:
        try:
            _date(r["date"], "Symptoms", r["_row"], "date")
        except InputError as e:
            problems.append(str(e))
        try:
            datetime.strptime(r["time"], "%H:%M")
        except ValueError:
            problems.append(f"Symptoms, row {r['_row']}, column 'time': not a 24-hour time like 14:30")
        if r["severity"] and not (re.fullmatch(r"[0-9]{1,2}", r["severity"]) and 1 <= int(r["severity"]) <= 10):
            problems.append(f"Symptoms, row {r['_row']}, column 'severity': a whole number from 1 to 10, or empty")
        raw_parts = [p for p in re.split(r"[;,/]", unicodedata.normalize("NFKC", r["symptom"])) if p.strip()]
        if not raw_parts:
            problems.append(f"Symptoms, row {r['_row']}, column 'symptom': empty")
        whole = signs_from_stems(r["symptom"])
        if "vomiting_blood" in whole:   # vomiting and blood/black split by a separator in one cell
            flags += [(r["_row"], "vomiting_blood"), (r["_row"], "emergency_check")]
        if whole and not all(is_exact(p, vocab) for p in re.split(r"[;,/]", r["symptom"])):
            flags.append((r["_row"], "emergency_check"))   # free text in this entry: add the 999 check
        for raw in raw_parts:
            part = normalise(raw)
            signs = red_signs(raw, vocab)
            if signs:                                        # a danger sign, however it was written
                flags += [(r["_row"], c) for c in sorted(signs)]
            elif not part or not only_plain_words(raw):     # e.g. an emoji, alone or after a word
                problems.append(f"Symptoms, row {r['_row']}, column 'symptom': use words, not symbols or emoji")
            elif part in vocab:
                out.append({**r, "symptom": vocab[part][0]})
            else:
                problems.append(f"Symptoms, row {r['_row']}, column 'symptom': not a recognised symptom. Use the "
                                f"words listed in reference/symptom_words.csv, e.g. bloating, pain, diarrhoea")
    # Red flags win over every other problem: the medical advice must never be hidden behind an input error.
    if problems and not flags:
        raise InputError(problems[0])
    return out, flags


def read_my_foods(path, components):
    rows = _read(path, "My foods", MY_FOOD_COLUMNS)
    seen = set()
    for r in rows:
        r["pattern"] = normalise(r["pattern"])
        where = f"My foods, row {r['_row']}"
        if not PATTERN.fullmatch(r["pattern"]):
            raise InputError(f"{where}, column 'pattern': use only letters, numbers and spaces")
        if r["component"] not in components:
            raise InputError(f"{where}, column 'component': use one of {sorted(components)}")
        try:
            grams = float(r["grams_per_100g"])
        except ValueError:
            raise InputError(f"{where}, column 'grams_per_100g': must be a number") from None
        if not 0 <= grams <= 100:
            raise InputError(f"{where}, column 'grams_per_100g': must be between 0 and 100")
        if not PATTERN.fullmatch(r["pattern"]) or len(r["pattern"].split()) > 5:
            raise InputError(f"{where}, column 'pattern': 1 to 5 words")
        if (r["pattern"], r["component"]) in seen:
            raise InputError(f"{where}: this food and component are already listed (one value each)")
        seen.add((r["pattern"], r["component"]))
    return rows


def read_exclude_days(path):
    rows = _read(path, "Excluded days", EXCLUDE_COLUMNS[:1], EXCLUDE_COLUMNS[1:])
    for r in rows:
        _date(r["date"], "Excluded days", r["_row"], "date")
    return rows


def _load_table(con, table, columns, rows):
    cols = ", ".join(f'"{c}" VARCHAR' for c in columns)
    con.execute(f"CREATE OR REPLACE TABLE {table} ({cols})")
    if rows:
        marks = ", ".join("?" for _ in columns)
        con.executemany(f"INSERT INTO {table} VALUES ({marks})", [[r.get(c, "") for c in columns] for r in rows])


def _run_sql(con, filename, reference_dir):
    sql = (SQL_DIR / filename).read_text()
    if filename == "03_load_reference.sql":
        ref = str(Path(reference_dir).resolve()).replace("'", "''")
        sql = sql.replace("'reference/", f"'{ref}/")
    con.execute(sql)


def spreadsheet_safe(text):
    """A cell a spreadsheet won't run as a formula. from_spreadsheet undoes it."""
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def from_spreadsheet(text):
    return text[1:] if text[:1] == "'" and text[1:2] in ("=", "+", "-", "@", "\t", "\r") else text


def read_my_matches(path):
    """The user's corrections: the table food a logged food really is, or "unknown". Blank rows aren't answered yet."""
    rows = _read(path, "My matches", MY_MATCH_COLUMNS, allow_extra=True)
    rows = [r for r in rows if r["correct_food"]]
    for r in rows:
        r["your_food"] = from_spreadsheet(r["your_food"]).strip()
        if not r["your_food"]:
            raise InputError(f"My matches, row {r['_row']}, column 'your_food': empty")
    return rows


def _resolve_my_matches(con, rows):
    """Each answer becomes a table food, found by its exact name or id. None means "treat this food as unknown"."""
    by_name, by_id, seen = {}, {}, set()
    for food, name in con.execute("SELECT food, name FROM reference_foods").fetchall():
        by_name.setdefault(name.strip().lower(), []).append(food)
        by_id[food.lower()] = food
    out = []
    for r in rows:
        answer = r["correct_food"].strip().lower()
        where = f"My matches, row {r['_row']}, column 'correct_food'"
        key = con.execute("SELECT lower(?)", [r["your_food"]]).fetchone()[0]    # the same lower-casing the SQL uses
        if key in seen:
            raise InputError(f"My matches, row {r['_row']}: this food is already listed (one answer each)")
        seen.add(key)
        if answer == "unknown":
            out.append((key, None))
        elif answer in by_id:
            out.append((key, by_id[answer]))
        elif len(by_name.get(answer, ())) == 1:
            out.append((key, by_name[answer][0]))
        elif answer in by_name:
            raise InputError(f"{where}: more than one table has this name; use its id: "
                             + ", ".join(sorted(by_name[answer])[:5]))
        else:
            raise InputError(f"{where}: no table food has exactly this name. Use --find-food to look it up")
    return out


def read_checked_signs(path):
    """D20: the entries the user has confirmed a doctor or nurse has checked. Anything else stays a danger sign."""
    rows = _read(path, "Checked signs", CHECKED_SIGN_COLUMNS, allow_extra=True)
    return frozenset(from_spreadsheet(r["entry"]) for r in rows if r["checked"].strip().lower() == "yes" and r["entry"])


def flagged_entries(path, flags):
    """The lines of the symptoms file behind these danger signs, without the header. Empty if it can't be read."""
    wanted = {row for row, _ in flags if row > 1}
    lines = _physical_lines(_symptom_text(path))
    return list(dict.fromkeys(lines[n - 1].strip() for n in sorted(wanted) if n <= len(lines) and lines[n - 1].strip()))


def _symptom_text(path):
    try:
        fd = _open_regular(path, "Symptoms")    # never blocks on a pipe, like every other read
        with os.fdopen(fd, "rb") as f:
            return f.read(MAX_SYMPTOM_BYTES).decode("utf-8-sig", errors="replace")
    except (InputError, OSError):
        return ""


def _physical_lines(text):
    """Lines as a CSV reader counts them, so a line number means the same thing everywhere."""
    return re.split(r"\r\n|\n|\r", text)


def _checked_lines(text, checked):
    """The lines holding these exact entries. Only an entry on a single line of its own can be confirmed, so a confirmed
    line can't stand in for part of a longer entry, and the header never counts."""
    if not checked:
        return frozenset()
    lines, found, end = _physical_lines(text), set(), 0
    reader = csv.reader(lines)
    try:
        for _ in reader:
            start, end = end + 1, reader.line_num
            if start == end > 1 and lines[end - 1].strip() in checked:
                found.add(end)
    except csv.Error:
        return frozenset()     # can't tell where entries start, so nothing is left out
    return frozenset(found)


def find_foods(term, reference_dir=ROOT / "reference", k=10):
    """The closest table foods to a word or two, as (name, id). Reads the tables only, and sends nothing anywhere."""
    con = duckdb.connect()
    try:
        _run_sql(con, "03_load_reference.sql", reference_dir)
        foods = con.execute("SELECT food, name FROM reference_foods ORDER BY food").fetchall()
    except (duckdb.Error, OSError):
        raise ConfigError("the reference files in reference/ couldn't be loaded") from None
    finally:
        con.close()
    index = search.Search([name for _, name in foods])
    return [(foods[i][1], foods[i][0]) for i in index.top(term, k)]


def read_my_servings(path):
    """The user's own answers: what one unit of a food weighs, e.g. one scoop of a protein powder (D21)."""
    rows = _read(path, "My servings", MY_SERVING_COLUMNS, allow_extra=True)   # a filled-in servings_to_check.csv
    rows = [r for r in rows if r["grams"]]    # a food left blank there is simply not answered yet
    seen = set()
    for r in rows:
        r["pattern"] = normalise(r["pattern"])
        r["unit"] = r["unit"].strip().lower()
        where = f"My servings, row {r['_row']}"
        if not PATTERN.fullmatch(r["pattern"]) or len(r["pattern"].split()) > 5:
            raise InputError(f"{where}, column 'pattern': 1 to 5 words, using only letters, numbers and spaces")
        if not r["unit"] or len(r["unit"]) > 60:
            raise InputError(f"{where}, column 'unit': give the unit as your food log writes it, e.g. scoop")
        try:
            grams = float(r["grams"])
        except ValueError:
            raise InputError(f"{where}, column 'grams': must be a number") from None
        if not 0 < grams <= 5000:
            raise InputError(f"{where}, column 'grams': must be above 0 and at most 5000")
        if (r["pattern"], r["unit"]) in seen:
            raise InputError(f"{where}: this food and unit are already listed (one value each)")
        seen.add((r["pattern"], r["unit"]))
    return rows


def run(food_csv, symptoms_csv, my_foods_csv=None, exclude_days_csv=None,
        symptoms_from=None, symptoms_to=None, reference_dir=ROOT / "reference",
        online_matching=None, match_cache=None, ask=None, my_servings_csv=None, my_matches_csv=None,
        checked_signs_csv=None):
    """Check the inputs, then run sql/03–06. Returns the open connection with every table.

    symptoms_from / symptoms_to: the user's declaration that every symptom between these dates is in the
    file (D16). Required, because a missing row is only "no symptom" if the user was logging.
    online_matching (D8): None = no AI matching (tests, demo); False = use saved matches only, and stop
    with an explanation if a food needs looking up; True = the user agreed to send food names.
    match_cache: path of the saved-matches file. ask: replaces the real AI call (tests only).
    my_servings_csv: the user's own answers for what one unit of a food weighs.
    my_matches_csv: the user's corrections to food matches.
    checked_signs_csv: danger signs the user confirmed have been checked (D20).
    """
    vocab = load_vocabulary(reference_dir)
    try:
        checked = read_checked_signs(checked_signs_csv) if checked_signs_csv else frozenset()
    except InputError:
        flags = scan_for_red_flags(symptoms_csv, vocab)     # a broken file can't hide the advice
        if flags:
            return RunResult(con=None, red_flags=flags)
        raise
    flags = scan_for_red_flags(symptoms_csv, vocab, checked)
    if flags:   # D17: stop before anything else; no data is loaded, no suspects are calculated
        return RunResult(con=None, red_flags=flags)
    for ref_file in Path(reference_dir).rglob("*.csv"):   # includes reference/food_tables/
        if ref_file.stat().st_size > MAX_REFERENCE_BYTES:
            raise ConfigError(f"reference/{ref_file.relative_to(reference_dir)} is too large to load")
    con = duckdb.connect()
    try:
        return _run(con, vocab, food_csv, symptoms_csv, my_foods_csv, my_servings_csv, exclude_days_csv,
                    symptoms_from, symptoms_to, reference_dir, online_matching, match_cache, ask,
                    my_matches_csv, _checked_lines(_symptom_text(symptoms_csv), checked) if checked else frozenset())
    except BaseException:
        con.close()   # never leave the in-memory copy of the logs open after a failure
        raise


def _run(con, vocab, food_csv, symptoms_csv, my_foods_csv, my_servings_csv, exclude_days_csv, symptoms_from, symptoms_to,
         reference_dir, online_matching=None, match_cache=None, ask=None, my_matches_csv=None,
         skip_lines=frozenset()):
    con.execute("SET temp_directory = ''")   # never spill health data to disk
    con.execute("SET memory_limit = '2GB'")  # a bounded budget, whatever the machine
    try:
        _run_sql(con, "03_load_reference.sql", reference_dir)
    except (duckdb.Error, OSError):
        raise ConfigError("the reference files in reference/ couldn't be loaded") from None
    components = {c for (c,) in con.execute("SELECT component FROM component_rules").fetchall()}

    food, first_day, last_day = read_food_log(food_csv)
    if not (symptoms_from and symptoms_to):
        raise InputError("Please give the dates you logged every symptom between (--symptoms-from, --symptoms-to)")
    sym_from = _date(symptoms_from, "--symptoms-from", "-", "date")
    sym_to = _date(symptoms_to, "--symptoms-to", "-", "date")
    if sym_from > sym_to:
        raise InputError("--symptoms-from is after --symptoms-to")
    if sym_to < first_day or sym_from > last_day:
        raise InputError("The symptom-logging dates don't overlap the food log")
    symptoms, flags = read_symptoms(symptoms_csv, vocab, skip_lines)
    if flags:
        con.close()
        return RunResult(con=None, red_flags=flags)
    my_foods = read_my_foods(my_foods_csv, components) if my_foods_csv else []
    my_servings = read_my_servings(my_servings_csv) if my_servings_csv else []
    my_matches = _resolve_my_matches(con, read_my_matches(my_matches_csv)) if my_matches_csv else []
    exclude = read_exclude_days(exclude_days_csv) if exclude_days_csv else []

    _load_table(con, "raw_food_log", FOOD_COLUMNS, food)
    _load_table(con, "raw_symptoms", SYMPTOM_COLUMNS, symptoms)
    _load_table(con, "my_foods", MY_FOOD_COLUMNS, my_foods)
    con.execute("ALTER TABLE my_foods ALTER grams_per_100g TYPE DOUBLE")
    _load_table(con, "my_servings", MY_SERVING_COLUMNS, my_servings)
    con.execute("ALTER TABLE my_servings ALTER grams TYPE DOUBLE")
    _load_table(con, "raw_exclude_days", EXCLUDE_COLUMNS, exclude)
    con.execute("CREATE OR REPLACE TABLE run_settings AS SELECT CAST(? AS DATE) AS symptoms_from, "
                "CAST(? AS DATE) AS symptoms_to", [sym_from, sym_to])

    con.execute("CREATE OR REPLACE TABLE my_matches (food_key VARCHAR PRIMARY KEY, food VARCHAR)")
    if my_matches:
        con.executemany("INSERT INTO my_matches VALUES (?, ?)", my_matches)
    con.execute("CREATE OR REPLACE TABLE ai_matches (food_name VARCHAR PRIMARY KEY, food VARCHAR NOT NULL, "
                "reason VARCHAR)")
    con.execute("CREATE OR REPLACE TABLE match_summary AS SELECT 0 AS from_cache, 0 AS asked, 0 AS not_asked, "
                "false AS unavailable, false AS ran, true AS saved")
    _run_sql(con, "04_clean.sql", reference_dir)
    _run_sql(con, "05_components.sql", reference_dir)
    if online_matching is not None:
        _match_with_ai(con, online_matching, match_cache, ask)
        _run_sql(con, "05_components.sql", reference_dir)   # again, now with the AI matches
    _run_sql(con, "06_score.sql", reference_dir)
    return RunResult(con=con)


def _match_with_ai(con, consented, cache_path, ask):
    """D8: names nothing else matched → saved answers, or the AI if the user agreed. Unknown otherwise."""
    names = [n for (n,) in con.execute("""SELECT DISTINCT e.food_name FROM food_entries e
                                          JOIN entry_match m USING (entry_id)
                                          WHERE m.match_source = 'unknown'
                                            AND lower(e.food_name) NOT IN (SELECT food_key FROM my_matches)
                                          """).fetchall()]
    foods = con.execute("SELECT food, name FROM reference_foods ORDER BY food").fetchall()
    try:
        cache = matching.MatchCache(cache_path or matching.DEFAULT_CACHE)
        found, report = matching.match_names(names, foods, cache, ask=ask, consented=consented)
    except matching.CacheError as e:
        raise InputError(f"--match-cache: {e}") from None
    except matching.ConsentNeeded as e:
        raise InputError(wording.MATCHING_CONSENT.format(n=e.n_names)) from None
    except ValueError as e:
        raise InputError(f"Food log: {e}") from None
    rows = [(n, m["food"], m.get("reason", "")) for n, m in found.items() if m.get("food")]
    if rows:
        con.executemany("INSERT INTO ai_matches VALUES (?, ?, ?)", rows)
    con.execute("CREATE OR REPLACE TABLE match_summary AS SELECT ? AS from_cache, ? AS asked, ? AS not_asked, "
                "? AS unavailable, true AS ran, ? AS saved",
                [report["from_cache"], report["asked"], report["not_asked"], report["unavailable"],
                 report["saved"]])
