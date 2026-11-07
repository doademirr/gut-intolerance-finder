# Gut Intolerance Finder — Architecture

**Summary:** a local command-line tool. It reads a Cronometer food export and a symptoms file, works out how many grams of each food component were eaten in the hours before each symptom, compares that with symptom-free time, and writes a report of suspects to test. It never diagnoses.

## Highlights

- **AI never produces an amount.** Food values come from published tables, the user's own labels, or a stated rule (such as no maltitol in fresh food). AI only picks which table food a name refers to.
- **Unknown is never zero.** Time that can't be checked is left out in both directions, so a gap can't clear a real suspect.
- **Only food names leave the computer,** and only for AI matching. Never dates, amounts or symptoms.
- **The safety gate runs first.** A red-flag symptom means medical advice and no suspects, until the user confirms that entry has been checked by a doctor or nurse.
- **Two pathways.** The tool first asks whether it can find a pattern. With at least 3 symptoms a week and nothing at the testing point, it says so and names the foods eaten on most days.
- **Consent comes first.** The user agrees once at set-up, and nothing runs without it.
- **Confidence is Bayesian,** scaled by each user's own background symptom rate, and capped below certainty.
- **Planted-trigger testing.** Synthetic logs with known answers. Across 900 synthetic datasets (3 Nov 2026), the planted trigger was found 90–100% of the time, with false alarms at 0–6%. Synthetic data follows the model's own assumptions, so this proves the logic, not real-world accuracy.

## Stack

| Layer | Choice | Why |
|---|---|---|
| Analysis logic | SQL in DuckDB (in memory) | Readable by a PM or analyst, repeatable, no server |
| Glue, checks, report | Python | Moves data in and out, fails closed on bad input |
| Food matching | Claude Sonnet 5.5 rewrite + word search + Claude Sonnet 5.5 pick | Understands messy names; picks from options only |
| Food values | USDA SR Legacy, UK CoFID 2021, Australian AFCD Release 3, plus a small own table | Open licences, 12,268 foods; own table for gaps and common dishes, with ingredient tags |
| Rules and numbers | CSV files in `reference/` | Change a value without changing code |

## Data flow

```
Cronometer CSV + symptoms CSV (+ optional label values, servings, corrections, excluded days)
        │
        ▼
gut_finder/engine.py    checks every file; a red flag stops here with medical advice
        │
        ▼
sql/03_load_reference   rule cards, food tables, fresh-food and no-milk rules
sql/04_clean            food entries, best-available times, symptom episodes, day coverage
sql/05_components       food name → table food → grams of each component
        │                  └─ unmatched names → matching.py → Claude Sonnet (rewrite, search.py, pick)
        │                     (names only; answers saved in ~/.gut_finder/)
        ▼
sql/06_score            15-minute ticks → exposures, background rate, confidence
        │
        ▼
gut_finder/report.py    plain-words report, written locally; all wording in wording.py
```

## Folder map

| Path | Holds |
|---|---|
| `gut_finder/` | Python: command line, engine, matching, search, report, wording, synthetic data, food-table build |
| `sql/` | The logic, in run order. `01` is for exploration only (`ui.py`); the engine runs `03`–`06` |
| `reference/` | Rule cards and word lists as CSVs; `food_tables/` holds the published tables with credits and licence notices |
| `examples/` | Templates for the user's own files |
| `tests/` | Automated checks, with small made-up fixtures in `tests/fixtures/`; `validate.py` runs the synthetic sweep |
| `data/synthetic/` | Demo data with planted triggers |
| `data/private/` | Real logs. Never in git |

**More detail:** `METHOD.md` (how it works), `SCIENCE.md` (the research behind each component) and `REFERENCES.md` (its sources).
