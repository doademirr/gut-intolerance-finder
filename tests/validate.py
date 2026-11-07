"""Validation sweep: how often the engine finds a planted trigger, and how often it raises a false alarm,
across different log lengths and background symptom rates (D4; Codex design review).

Run from the project root:  .venv/bin/python tests/validate.py [seeds_per_scenario]
Slower than the tests (a few minutes). Prints one table; nothing is saved.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tests.test_engine import run  # noqa: E402
from gut_finder import synthetic  # noqa: E402

SEEDS = int(sys.argv[1]) if len(sys.argv) > 1 else 50
TESTING_POINT = 0.5


def scores(food, symptoms):
    con = run(food, symptoms).con
    return dict(con.execute("SELECT component, coalesce(score, 0) FROM component_results").fetchall())


print(f"{'days':>4} {'background':>10} | {'found (planted ≥ 50%)':>22} {'other ≥ 50% (planted)':>22} | "
      f"{'false alarm (no trigger)':>24}")
for days in (30, 60, 120):
    for bg in (0.1, 0.2, 0.35):
        found = other = alarm = 0
        for seed in range(SEEDS):
            s = scores(*synthetic.generate(seed, days=days, plant="sorbitol", background_per_window=bg))
            found += s.get("sorbitol", 0) >= TESTING_POINT
            other += any(v >= TESTING_POINT for k, v in s.items() if k not in ("sorbitol", "polyols_total"))
            n = scores(*synthetic.generate(10_000 + seed, days=days, plant=None, background_per_window=bg))
            alarm += any(v >= TESTING_POINT for v in n.values())
        print(f"{days:>4} {bg:>10.2f} | {found / SEEDS:>22.0%} {other / SEEDS:>22.0%} | {alarm / SEEDS:>24.0%}")
