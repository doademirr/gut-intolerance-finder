"""Planted-trigger test on a real food log, for any scored card (D4).

The food log is real; the symptoms are made up, so the right answer is known. Each scenario is run many times:
- planted: a stretch where the known amount reaches the dose (or a share of it) is followed by a symptom, at a random moment
  inside that stretch, on top of random background symptoms. `trigger_rate` is the chance per window, so a stretch
  shorter or longer than one window gets a smaller or larger chance, which is what the scoring assumes. Background
  symptoms come on top, so the total chance in a stretch is above `trigger_rate`, and more so on a long window: at 70%
  and 20% background it's about 72% for a 2-hour window and 85% for a 23-hour one;
- no trigger: background symptoms only, so any suspect at or above the testing point is a false alarm. These runs are
  the same whichever card is planted, so never add them up across cards.

What this measures: whether the log holds enough of the card's trigger, and enough clear time around it, for a trigger to be
found. It's a best case: both the amounts and the meal times are the tool's own reading, including estimated times, so
a wrong reading moves the planted symptom with it and can't hurt the result. The check sheet covers wrong readings.

Run from the project root:
  .venv/bin/python tests/validate_real_log.py FOOD.csv --match-cache MATCHES.json [--my-servings S.csv] [--seeds 30]
Uses saved matches only, so nothing is sent anywhere. Prints totals, never food names. Nothing is saved, apart from
temporary files that are deleted at the end, in a private folder.
"""
import argparse
import csv
import math
import random
import shutil
import statistics
import sys
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from gut_finder import engine, wording  # noqa: E402

TESTING_POINT = 0.5             # G13
TOGETHER_ABOVE = wording.TOGETHER_ABOVE
BACKGROUND_HOURS = 7.5          # background chances are per 7.5 h for every card, so they mean the same symptom rate
TICK_MINUTES = 15               # D15
# (name, share of the card's dose at which the made-up person reacts, trigger rate, background chance per 7.5 h)
SCENARIOS = [
    ("reacts at the card's dose", 1, 0.7, 0.10),
    ("reacts at the card's dose", 1, 0.7, 0.20),
    ("reacts at the card's dose", 1, 0.7, 0.35),
    ("reacts less reliably", 1, 0.5, 0.20),
    ("reacts from half (stress test)", 0.5, 0.7, 0.20),
    ("no trigger", None, 0.0, 0.10),
    ("no trigger", None, 0.0, 0.20),
    ("no trigger", None, 0.0, 0.35),
]


def write_symptoms(path, onsets, rng):
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(engine.SYMPTOM_COLUMNS)
        for t in sorted(onsets):
            w.writerow([t.date().isoformat(), t.strftime("%H:%M"), "pain", rng.randint(3, 8)])   # counts on every card


def run_engine(args, symptoms_path, cache_path):
    return engine.run(args["food"], symptoms_path, symptoms_from=args["first"], symptoms_to=args["last"],
                      online_matching=True, match_cache=cache_path, ask=lambda system, user: None,
                      my_servings_csv=args["servings"])


def dose_ticks(args, folder):
    """The target card's reading at every 15-minute tick, from a run with no symptoms. Same for every scenario."""
    empty = folder / "empty.csv"
    write_symptoms(empty, [], random.Random(0))
    result = run_engine(args, empty, folder / "matches.json")
    try:
        from_cache, asked, not_asked, unavailable = result.con.execute(
            "SELECT from_cache, asked, not_asked, unavailable FROM match_summary").fetchone()
        if from_cache == 0 or asked > 0 or not_asked > 0 or unavailable:
            # old or incomplete matches would quietly change the experiment
            raise SystemExit(f"The saved matches don't cover this log ({from_cache} saved, "
                             f"{asked + not_asked} missing). Run the matcher first.")
        first = result.con.execute("SELECT first_day FROM settings").fetchone()[0]
        rows = result.con.execute("SELECT k, dose FROM tick_state WHERE component = ? ORDER BY k",
                                  [args["target"]]).fetchall()
        dose, window = result.con.execute("SELECT dose_threshold, block_len FROM scored_components WHERE component = ?",
                                          [args["target"]]).fetchone()
        # the card itself, its parent and its children: a planted trigger can't be a false alarm for those
        related = {r[0] for r in result.con.execute("""
            SELECT c.component FROM component_rules c, component_rules t
            WHERE t.component = ? AND (c.component = t.component OR c.component = t.parent OR c.parent = t.component)""",
            [args["target"]]).fetchall()}
    finally:
        result.con.close()
    return first, rows, dose, window, related


def stretches(rows, reacts_at):
    """Unbroken runs of ticks at or above reacts_at, as (first tick, last tick)."""
    out, start, last = [], None, None
    for k, dose in rows:
        if dose >= reacts_at - 1e-9:
            if start is None:
                start = k
            last = k
        elif start is not None:
            out.append((start, last))
            start = None
    if start is not None:
        out.append((start, last))
    return out


def one_run(job):
    args, first, rows, dose, window, scenario, seed, folder = job
    _, share, trigger_rate, background = scenario
    reacts_at = None if share is None else share * dose
    folder = Path(folder)
    rng = random.Random(f"{scenario}-{seed}")
    begin = datetime.combine(first, datetime.min.time())
    end = datetime.combine(date.fromisoformat(args["last"]), datetime.min.time()) + timedelta(days=1)

    onsets = []
    if reacts_at is not None:
        for from_k, to_k in stretches(rows, reacts_at):
            windows = (to_k - from_k + 1) / window
            if rng.random() < 1 - (1 - trigger_rate) ** windows:     # as sql/06_score.sql compounds it
                onsets.append(begin + timedelta(minutes=TICK_MINUTES * rng.randint(from_k, to_k)))
    rate = -math.log(1 - background) / BACKGROUND_HOURS     # per hour, as in gut_finder/synthetic.py
    t = begin
    while True:
        t += timedelta(hours=rng.expovariate(rate))
        if t >= end:
            break
        onsets.append(t)

    symptoms = folder / f"symptoms_{abs(hash((scenario, seed)))}.csv"
    cache = folder / f"matches_{abs(hash((scenario, seed)))}.json"
    shutil.copyfile(folder / "matches.json", cache)      # a run can rewrite its file, so each has its own
    write_symptoms(symptoms, [o for o in onsets if o < end], rng)
    result = run_engine(args, symptoms, cache)
    try:
        scores = result.con.execute("""SELECT component, coalesce(score, 0), n_exposures, clues, result_status
                                       FROM component_results""").fetchall()
        shared = dict(result.con.execute("SELECT component, shared FROM suspect_overlap WHERE other = ?",
                                         [args["target"]]).fetchall())
        return [(*r, shared.get(r[0], 0)) for r in scores]
    finally:
        result.con.close()
        symptoms.unlink()
        cache.unlink()


def summarise(scenario, results, target_card, related):
    name, share, trigger_rate, background = scenario
    related = related if share is not None else {target_card}   # with nothing planted, its family can be false alarms
    target = [next(r for r in rows if r[0] == target_card) for rows in results]
    others = Counter()
    for rows in results:
        for component in {r[0] for r in rows if r[0] not in related and r[1] >= TESTING_POINT}:
            others[component] += 1
    found = sum(r[1] >= TESTING_POINT for r in target)
    any_other = sum(any(r[0] not in related and r[1] >= TESTING_POINT for r in rows) for rows in results)
    # a false suspect the report names as happening alongside the real one (D26), so it can't mislead on its own
    def named(rows, r):
        target_shown = next(x for x in rows if x[0] == target_card)[1] >= TESTING_POINT
        return share is not None and target_shown and r[3] and r[5] / r[3] > TOGETHER_ABOVE
    alone = sum(any(r[0] not in related and r[1] >= TESTING_POINT and not named(rows, r) for r in rows)
                for rows in results)
    n = len(results)
    print(f"{name:<30} {trigger_rate:>7.0%} {background:>10.0%} | {found / n:>16.0%} {any_other / n:>16.0%} {alone / n:>7.0%} | "
          f"{statistics.median(r[2] for r in target):>9.0f} {statistics.median(r[3] for r in target):>7.0f} | "
          f"{dict(Counter(r[4] for r in target))} {dict(others)}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("food")
    parser.add_argument("--match-cache", required=True)
    parser.add_argument("--my-servings")
    parser.add_argument("--seeds", type=int, default=30)
    parser.add_argument("--target", default="lactose", help="the card to plant")
    parser.add_argument("--workers", type=int, default=4)
    cli = parser.parse_args()

    with open(cli.food, newline="", encoding="utf-8-sig") as f:
        days = sorted(row["Day"] for row in csv.DictReader(f))
    args = {"food": cli.food, "servings": cli.my_servings, "first": days[0], "last": days[-1], "target": cli.target}

    with tempfile.TemporaryDirectory(prefix="gut_validate_") as name:   # private to this user by default
        folder = Path(name)
        shutil.copyfile(cli.match_cache, folder / "matches.json")
        first, rows, dose, window, related = dose_ticks(args, folder)
        print(f"{cli.target}: stretches reaching {dose:g} g: {len(stretches(rows, dose))}; "
              f"reaching {dose / 2:g} g: {len(stretches(rows, dose / 2))}\n")
        print(f"{'scenario':<30} {'trigger':>7} {'background':>10} | {'target >= 50%':>16} {'other >= 50%':>16} {'alone':>7} | "
              f"{'exposures':>9} {'clues':>7} | statuses, false alarms by card")
        jobs = [(args, first, rows, dose, window, s, seed, name) for s in SCENARIOS for seed in range(cli.seeds)]
        with ProcessPoolExecutor(max_workers=cli.workers) as pool:
            done = list(pool.map(one_run, jobs))
    for i, scenario in enumerate(SCENARIOS):
        summarise(scenario, done[i * cli.seeds:(i + 1) * cli.seeds], cli.target, related)


if __name__ == "__main__":
    main()
