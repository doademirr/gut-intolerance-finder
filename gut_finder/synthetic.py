"""Synthetic food and symptom logs with known answers (D4).

Two kinds of dataset:
- planted: one component is a real trigger. After a serving at or above its dose, a symptom follows
  with probability `trigger_rate`, somewhere inside the card's window.
- no trigger: symptoms only come from the background, at random times.

Both use the test-only foods in tests/fixtures/ref, never real composition values, so the tests check
the engine's logic, not the science. Output is in the same format as a real upload.
"""
import csv
import math
import random
from datetime import date, datetime, timedelta

from gut_finder.engine import FOOD_COLUMNS, SYMPTOM_COLUMNS

# Fixture foods: name as a user might log it → grams of the component per 100 g (tests/fixtures/ref)
PEAR = ("Pear, raw", "sorbitol", 2.0)              # 250–350 g serving → 5–7 g sorbitol (dose 5 g)
MUSHROOM = ("Mushrooms, white, sliced", "mannitol", 3.0)  # 150–350 g → 4.5–10.5 g mannitol (dose 8 g)
NEUTRAL = ["Rice, white", "Oats, rolled", "Chicken breast", "Eggs, boiled", "Carrots, raw"]


def _clock(dt):
    return dt.strftime("%I:%M %p").lstrip("0")   # Cronometer style, e.g. "2:21 PM"


def generate(seed, days=120, plant="sorbitol", trigger_rate=0.7, background_per_window=0.2,
             window_hours=7.5, onset_hours=(1.0, 7.5), missing_time_share=0.1, start=date(2026, 1, 1)):
    """Return (food_rows, symptom_rows). plant=None makes a no-trigger dataset."""
    rng = random.Random(seed)
    food, symptoms = [], []
    triggers = {"sorbitol": PEAR, "mannitol": MUSHROOM}
    meals = [("Breakfast", 8), ("Lunch", 12.5), ("Dinner", 19)]

    for d in range(days):
        day = start + timedelta(days=d)
        for meal, hour in meals:
            t = datetime.combine(day, datetime.min.time()) + timedelta(hours=hour + rng.uniform(-0.75, 0.75))
            t = t.replace(second=0, microsecond=0)
            items = [(rng.choice(NEUTRAL), None, 0.0, rng.randint(100, 250))]
            if rng.random() < 0.35:
                items.append((*PEAR, rng.randint(250, 350)))
            if meal != "Breakfast" and rng.random() < 0.25:
                items.append((*MUSHROOM, rng.randint(150, 350)))
            for name, component, per100, grams in items:
                time_text = "" if rng.random() < missing_time_share else _clock(t)
                food.append([day.isoformat(), time_text, meal, name, f"{grams:.2f} g", "Test"])
                if plant and component == plant and triggers[plant][2] * grams / 100 >= _dose(plant):
                    if rng.random() < trigger_rate:
                        onset = t + timedelta(hours=rng.uniform(*onset_hours))
                        symptoms.append(onset)

    # Background: random onsets at a steady rate, so that a window of `window_hours` has a symptom with
    # probability `background_per_window`.
    rate = -math.log(1 - background_per_window) / window_hours   # per hour
    t = datetime.combine(start, datetime.min.time())
    end = t + timedelta(days=days)
    while True:
        t += timedelta(hours=rng.expovariate(rate))
        if t >= end:
            break
        symptoms.append(t)

    symptom_rows = [[s.date().isoformat(), s.strftime("%H:%M"), "bloating", str(rng.randint(3, 8))]
                    for s in sorted(symptoms) if s < end]
    return food, symptom_rows


def _dose(component):
    return {"sorbitol": 5.0, "mannitol": 8.0}[component]


def write(food, symptoms, food_path, symptoms_path):
    with open(food_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(FOOD_COLUMNS)
        w.writerows(food)
    with open(symptoms_path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(SYMPTOM_COLUMNS)
        w.writerows(symptoms)
