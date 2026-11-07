# Gut Intolerance Finder

An experimental tool that looks for patterns between what you eat and your gut symptoms. It reads a Cronometer food log and a symptoms file, and suggests which food components might be worth testing.

**This isn't medical advice, and it can't diagnose anything.** It finds patterns in your own logs. Talk to a GP or dietitian before changing what you eat.

**Status:** early version. It runs from the command line, so you'll need to be comfortable with a terminal.

## What it checks

Lactose, polyols (sugar alcohols), excess fructose, fructans, GOS, wheat and gluten, and coffee.

It works best when your symptoms come and go. If you have symptoms at least 3 times a week and it can't find a pattern, the report says so and names the foods you eat on most days.

## What you get

A report like this, from the made-up demo data:

```
Sorbitol: pattern score 95%, strong pattern

- 89 of your 108 times above 5 g were followed by symptoms between 30 min and 8 h after eating.
- Symptoms followed 17% of comparable stretches without it.
```

## What you need

- Python 3.15
- A food log exported from Cronometer
- A symptoms file (see below)
- Your own Anthropic API key, to look up foods. It's billed to your account. A year of food costs about $4.50 the first time, and after that only new foods cost anything. Without a key, the tool still runs, but foods it can't find by name are left out.

## Install

```
git clone https://github.com/doademirr/gut-intolerance-finder.git
cd gut-intolerance-finder
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

## Try the demo

The demo uses made-up data and sends nothing anywhere.

```
.venv/bin/python -m gut_finder --demo
```

## Use your own logs

**1. Export your food log.** In Cronometer, export "Food & Recipe Entries" as a CSV.

**2. Make your symptoms file.** This is a spreadsheet with one row for each symptom, saved as a CSV. Start from `examples/symptoms_template.csv`. It looks like this:

| date | time | symptom | severity |
|---|---|---|---|
| 2026-01-05 | 14:30 | bloating | 6 |
| 2026-01-07 | 09:15 | diarrhoea | 7 |

- **date:** year-month-day.
- **time:** when the symptom started, on a 24-hour clock.
- **symptom:** a plain word such as bloating, gas, pain, cramps, diarrhoea, constipation, nausea, heartburn or reflux. The full list is in `reference/symptom_words.csv`.
- **severity:** 1 to 10. You can leave this blank.

Log every symptom, including the small ones. The tool treats a day with nothing logged as a day with no symptoms.

**3. Add your API key.** Copy `.env.example` to `.env`, add your key, then load it:

```
set -a; source .env; set +a
```

**4. Run it.** Give the first and last days you logged every symptom:

```
.venv/bin/python -m gut_finder --food FOOD.csv --symptoms SYMPTOMS.csv \
  --symptoms-from 2026-01-01 --symptoms-to 2026-03-31
```

The first time, the tool explains how it works and asks you to agree. Your report is saved as `data/private/report.md`.

## If the report asks you for something

**A food logged as a serving.** "1 serving" has no weight, so the tool can't tell how much you had. The report lists these foods in `servings_to_check.csv`. Type in what one serving weighs in grams, then run the tool again with `--my-servings servings_to_check.csv` added.

**A danger sign.** If you log something a doctor should check, such as blood in your poo, the report stops and tells you what to do. Once a doctor or nurse has checked it, write yes next to that entry in `danger_signs_to_confirm.csv`, then run the tool again with `--checked-signs danger_signs_to_confirm.csv` added.

## Your privacy

- Your food and symptom logs stay on your computer, and reports are readable only by you.
- To look up a food, the tool sends its name to Anthropic's Claude AI using your own API key, and nothing else: no dates, amounts or symptoms. Answers are saved on your computer, so a name isn't sent twice.
- Food names can still say something about you, so read the set-up screen before you agree.

## How it works

The tool reads your whole log once. It finds every time a component went over its dose and checks whether a symptom followed inside that component's window. It also checks how often symptoms happen without it, then gives each component a score.

| Folder or file | What's in it |
|---|---|
| `gut_finder/` | The Python code |
| `sql/` | The analysis, written in SQL and run by DuckDB |
| `reference/` | Food tables and the rules for each component |
| `examples/` | Templates for your files |
| `tests/` | Automated tests |
| `ARCHITECTURE.md` | A one-page overview of how it's built |
| `METHOD.md` | How the score works, how food names become grams, and how time is counted |
| `SCIENCE.md`, `REFERENCES.md` | The research behind each component, and its sources |

## Run the tests

```
.venv/bin/python tests/test_engine.py
```

`tests/` also has `test_matching.py`, `test_food_tables.py` and `test_reference.py`.

## What's next

Planned next: guided experiments, choosing your own AI model, and looking at single foods as well as components.

## Contributing

Issues and suggestions are welcome. Please never post your own food or symptom logs in an issue.

## Credits

Food values come from USDA FoodData Central, the UK's McCance and Widdowson's CoFID 2021 and Food Standards Australia New Zealand's AFCD Release 3. Their licences and credits are in `reference/food_tables/SOURCES.md`.

## Licence

The code is under the MIT licence (`LICENSE`). The food data keeps its own licences.
