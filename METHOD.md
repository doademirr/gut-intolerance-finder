# Gut Intolerance Finder — Method

**What this is:** how the tool turns a food log and a symptom log into suspects: the engine steps, the confidence model, the global rules. This changes when product logic changes. The science behind each component (doses, windows, evidence) is in `SCIENCE.md`. Rules and numbers are stored as a parameters table that the SQL reads, so what's written here and in `SCIENCE.md` is exactly what the code uses.

*Not medical advice. The product names suspects to test, never a diagnosis.*

---

## Tenets (in priority order)

1. A danger sign stops the analysis.
2. Suspects to test, never a diagnosis.
3. Science sets the number, not preference.
4. Estimate when there's something real to base it on, and label it. If there's nothing, leave it out and say so.
5. Catching a real trigger beats avoiding a false alarm.
6. Never show a score or status without its reason in plain words.
7. Fix what breaks the main result; log rare cases that don't.

---

## 1. How the engine works

The same steps run for every component. Only the numbers on each component's card change.

1. **Match** each logged food to the reference table (AI matches the name, e.g. "Supermarket Mature Cheddar 30 g" → *cheddar cheese*). Foods with no match are shown as **unknown**, never skipped.
2. **Calculate** the grams of each component in each food from the reference table (hard-coded values, never AI-generated).
3. **Add up** each component's grams eaten inside that component's look-back window before each symptom (stacking).
4. **Weigh** every time the component went above its dose threshold: followed by a symptom inside the window (evidence for) or not (counter-example). See 1.1.
5. **Show** each suspect with its confidence level, at the component level (e.g. lactose). Confidence rises and falls as evidence comes in.
6. **Next step:** the report says when a coeliac test comes first. When symptoms are too frequent to find a pattern, it names the foods eaten on most days and points to a GP or dietitian (G21).

### 1.1 How confidence works (the detective model)

The product never says a food *is* the cause. It holds a **confidence level** for each suspect and moves it up or down as evidence comes in. This is **Bayesian updating**: start with a belief, then let every new clue move it. Results start vague and get clearer as the user keeps logging and tests foods.

| Step | Where it comes from | What it does |
|---|---|---|
| 1. Starting belief | The card's research: how often the component triggers symptoms in blinded trials, and its evidence strength (G18) | Sets where confidence starts. Fructans (blinded trials) start higher than coffee (no trial). |
| 2. Evidence for | The user's data: the component came before a symptom, at or above the card's dose, inside its window | Confidence rises |
| 3. Counter-example | The user's data: eaten at or above the dose, no symptom inside the window | Confidence drops |
| 4. Never 100% | G16 | Capped below certainty |

**Where the cards plug in:**
- **Dose** decides whether an exposure counts as a test at all. A splash of milk is far below the lactose dose, so it's neither evidence for nor a counter-example.
- **Window** decides which symptoms an exposure can be linked to.
- **Evidence strength** sets the starting point and the ceiling. Weak cards can't climb as high; very weak cards stay at "worth watching" (G18).
- **Days with no food logged** count neither way (G11). **Fuzzy times** (no logged time and no usual meal time to fall back on) are left out.

**Starting point vs day-1 data:** the starting belief comes only from the card's research. Imported history counts as clues, exactly like data logged later, so it is never counted twice. Someone who imports a clean year of history gets their answer on day 1; someone who logs the same year day by day reaches the same answer by the end of it. No data, no suspects: the starting belief alone is never shown as a result.

**Why counter-examples matter:** a food eaten every day comes before every symptom, but it also appears on many symptom-free days, so it sinks. They also separate foods eaten together: ice cream (lactose + sorbitol) credits both, then a big glass of milk with no symptoms lowers lactose only.

| Decision | Value |
|---|---|
| Fixed number of repeats before a suspect is shown | None. Repeats are evidence, not a gate. |
| Day 1 | A user can run the tool on past food logs and past symptoms, so suspects can show from the first run, usually at low confidence |
| What users see | A percentage **and** a plain-words band (e.g. "75%, strong pattern"), framed as a suspect worth testing (G16) |
| Showing the counts behind it (e.g. "came before 7 of your 9 episodes") | Shown with each suspect. When one suspect stands alone, say so in words, not extra points: "This component matched all 3 recorded symptom episodes, with no recorded symptom-free exposures above its dose. No other tracked component matched all 3." Used only when every statement is true, and only counting separate episodes (G17): three lunches during one stomach bug are one episode, not three. |
| Maths | **Points (log-odds), with each clue's worth set by the user's own background.** A suspect starts at the card's starting points. Each symptom after it adds more points the rarer symptoms are for that user (real-trigger rate ÷ user's background rate); each counter-example takes points away. Points convert to a percentage. Someone with symptoms after most meals gets little credit per clue, so no single food climbs falsely. Chosen over fixed points for everyone, which ignore the background rate (G13). |
| Starting points | **Trial % ÷ 10** (e.g. fructans 56% → 5.6%). The trial sets the order and the gaps between cards; the 10 is a tuning setting, not a research number. Not placebo-corrected: placebo is handled by wording (G16) and each user's own background rate. Typical user (symptoms in 1 of 5 windows): 3 clean times → 50–72%, 4 → 78–90%. Chosen over ÷ 4 because diary clues aren't always independent: a 3-day stomach bug while eating the same onion soup would give fructans 3 for 3 from one cause (87% at ÷ 4, 72% at ÷ 10). Checked by the planted-trigger **and** no-trigger tests.  **Cards with no trial number:** Moderate evidence → **2.3%** (same as sorbitol, the lowest tested card); Weak or Very weak → **0.7%** (one extra clean time needed). No v1 card is Weak or Very weak today. An untested card never starts above a tested one. Survey rates aren't used: they're self-reported, not blinded. |
| Suspects that happen together | If more than half of a suspect's clues were also clues for another suspect, the report names the other under it. Scores don't change. |
| When to suggest testing | From about **50%**, so a typical user's 3 clean times lead to a test suggestion without showing an inflated %. |
| Other settings | How often a real trigger causes symptoms (70%, tuned together with the ÷ 10); confidence cap (95%); each user's background rate, measured as in 1.2. |

### 1.2 Scoring units: dose-valid time, not meals

- Time is checked in 15-minute **ticks**. At each tick T, the **dose** is the grams eaten at times τ with τ + window start ≤ T < τ + window end. A symptom onset counts as evidence for a component only if the dose at that onset reaches the card's threshold. Doses expire when they leave the window. Window edges are rounded **inwards** for doses (a symptom is never credited outside the window; up to 15 min at each edge can be lost) and **outwards** for unknown time.
- An **exposure** is one continuous stretch of time where the known dose is at or above the threshold. On each side it needs time below the dose or time blocked by an unknown food; a stretch cut off by an unusable day or the start of the log is left out, because part of it was never observed. Symptom onset inside it → evidence for; none → counter-example. One stretch = one test, however many servings built it.
- **Episodes:** symptom onsets less than 3 h apart count as one episode (G17).
- **Background:** the rest of the covered time is cut into back-to-back blocks the length of the window; the share of blocks with a symptom onset is the user's background rate (smoothed: (k + 1) / (n + 2)). A stretch m windows long is m chances for **both** a real trigger and a coincidence: evidence for = ln((1 − (1 − t)^m) / (1 − (1 − b)^m)); counter-example = m · ln((1 − t) / (1 − b)). Compounding both the same way means a long stretch can never turn evidence backwards. Less than 150 hours of clear time → "not enough data", whatever the card's window, so a long-window card such as wheat isn't held to a higher bar. If the background is at or above the trigger rate, the report says the diary can't separate this component from background.
- **Cap** applied after all evidence is added: 95% for normal cards, 60% for Weak and Very weak ones (G18), which are also shown separately as "weaker leads".

### 1.3 From food names to grams

**Order of trust:** the user's own label values, then the alias list, then AI matches.

**One seam:** every source of component grams writes rows to one table, `component_intake` (entry, time, time source, component, grams, where the value came from). The scoring never cares where grams came from. Sources today: (a) the reference table matched by food name (`reference/food_aliases.csv`: aliases of 1–5 words, looked up exactly against every run of 1–5 consecutive words in the food name, accents removed; longest match wins); (b) the user's own label values (`--my-foods`), which replace (a) for the same food; (c) AI matches.

**AI matching** (`gut_finder/search.py`, `gut_finder/matching.py`): Claude Sonnet 5.5 rewrites each name as up to 3 plain table-style names ("wombok" becomes "Chinese cabbage"). These are search words only, never a match. Word search then finds the closest table foods for the name and its rewrites, taking each search's best result in turn, up to 20 options. Claude Sonnet 5.5 sees the real name and those options, and picks the one that is the same food, or none. A dish never matches one of its ingredients. Only the food name and the option names are sent: never dates, amounts or symptoms. Answers are saved in `~/.gut_finder/food_matches.json` (readable only by the user), so a name is sent again only if its options change or the model or instructions change. The user agrees once at set-up, before anything runs (wording in `wording.py`). A user can correct any match with `--my-matches`. A "none", a malformed answer or a service failure leaves the food unknown, never zero, and the report says so. The report doesn't list AI matches or ask users to check them: that's the tool's job, and it's too much effort for a user. The saved-matches file is only ever replaced if it is the file the tool read, and a saved answer only counts for exactly the options the search shows now, and only if its food is one of them; a save that can't be done safely is skipped, and the foods are looked up again next time.

**Unknown is never zero:** a food with no value is listed as unknown (G9), and an amount that can't be turned into grams is listed as unreadable, not guessed.

**Amounts that aren't grams:** the export keeps each amount in the unit it was logged in, so grams are worked out after a food is matched. The order is: weighed; the user's own answer (`--my-servings`: what one unit of a food weighs); the density published for the matched food (the Australian table gives 213 liquids a specific gravity, e.g. 100 ml of milk at 1.03 is 103 g); the density of that type of liquid, for foods matched to a table that publishes none (`reference/liquid_densities.csv`: milk, yoghurt, cream and custard, taken from the Australian table; condensed, evaporated, dried, whipped, frozen, clotted, spray and imitation forms stay unknown); a typical size (`reference/typical_portions.csv`). Otherwise it stays unknown. A plain "cup" has no stated size, so it stays unknown too. **Assume when a wrong guess can't change the result; ask when it could.** A typical size applies only to a food low enough in every component for the expected error to be small: a whey scoop is 25–30 g, so a food at 5 g per 100 g or less moves by at most 0.25 g per scoop against a 12 g dose, while sweet dried whey, which is mostly lactose, stays unknown. Where a wrong guess could matter, the report lists the food under "Worth weighing once". Known limit: accumulated error can still flip a dose near the line. The report says how many amounts were worked out and how.

**Unknown blocks time, it doesn't count as zero:** if an entry has no value for a component (food not recognised, value missing, amount not in grams, no usable time), the time it could affect is left out for that component, in both directions, unless the known grams alone reach the dose (the "at least" rule below). The reference table therefore stores measured zeros explicitly. A parent total (all polyols combined) is exact only when every child has a value. The report shows how much logged time could be checked.

**Published tables:** the reference values come from three tables bundled in `reference/food_tables/` (USDA SR Legacy, UK CoFID 2021, Australian AFCD Release 3; 12,268 foods), each food keyed as `<table>:<id>` (e.g. `uk:12-345`). The files keep values exactly as published; how they're read is set in one place, `sql/03_load_reference.sql`: a number is grams per 100 g; **"Tr" (trace) counts as 0**, because even 0.1 g per 100 g would need 12 kg of food to reach the 12 g lactose dose; "N" (present but not measured) and blanks stay **unknown**; values given per 100 ml (UK alcoholic drinks) stay **unknown**, since millilitres aren't grams. Any other value stops the load. The bundled files also keep each food's published group (USDA category, CoFID group code, AFCD classification code) and the AFCD's maltitol, xylitol and erythritol columns (one food has values: a no-added-sugar dark chocolate, 28 g maltitol per 100 g).

**"At least" rule, every card:** when part of a dose is unknown, the known part still counts. Unknown food can only add more, so if the known grams alone reach the card's dose, that time is an exposure: a symptom counts for the component, and no symptom counts against it. Below the dose it stays unknown, never zero. Inside one food, known polyols also add up into all polyols combined, marked "at least". Examples: 15 g lactose plus an unknown breakfast → at least 15 g, above the 12 g dose → exposure; 6 g lactose plus an unknown breakfast → could be 6 g or 20 g → unknown; 10 g sorbitol plus unknowns → an exposure for sorbitol (5 g dose), unknown for all polyols combined (20 g dose).

**Maltitol and isomalt in fresh food:** they are made in factories and added to sugar-free and reduced-sugar products (UK/EU and Australian rules tie them to those foods), so fresh food, plain dried fruit and 100% juice count as 0 g. "Fresh" is the published food groups listed in `reference/fresh_food_groups.csv`: fruit and 100% juice, vegetables, plain legumes, nuts and seeds, herbs and spices, plain meat, poultry, fish and eggs; groups for mixed dishes, meat products, fruit drinks and cordials are not listed. Inside those groups, a name stays unknown if it says the food may be sweetened (sweetened, sugar-free, no added sugar, diet, lite, reduced sugar or energy, low calorie, nectar, a named sweetener or sugar alcohol; "unsweetened" and "not sweetened" keep the 0), or shows a food that typically carries sugar alcohols when sold sugar-free (chocolate, sweets, gum, mints, biscuits, cakes, desserts, puddings, yoghurt, ice cream, bars, cereals, jams, spreads, protein products), or a drink, sauce, mix, flavour or flavoured product ("juice drink", "pumpkin pie mix", "peanuts, raisins and chocolate chips"). The rule is about added sweeteners, not about cooking: simple dishes made from fresh foods (fishcakes, potato pancakes) keep the 0. A value the table itself gives always wins. Xylitol and erythritol are not covered by this rule. Coverage: 5,908 of 12,268 table foods get the zero.

**"At most" rule, every card:** the other side of "at least". An unknown food can't hold more of a component than it weighs. So if known grams plus the weight of every unknown food in the window stay under the dose, that time is below the dose: 5 g of milk sugar plus a 5 g unknown powder can't reach 12 g. Every unknown food needs a weight and a time; one without either keeps the time unknown.

**Lactose in foods that can't contain milk:** milk is the only natural source of lactose, so plain fruit, vegetables, legumes, grains, nuts, seeds, herbs, meat, fish, eggs, oils and cocoa count as 0 g when the tables give no figure. Two checks must both pass, and a figure the table gives always wins.
- **The table food:** it's in a group listed in `reference/milk_free_groups.csv`, and its table name has no word from the `milk` list in `reference/lactose_name_words.csv`. That list covers what the sources warn about, such as sausages, bacon, breaded food, mashed potato, soups, sauces and protein powders, plus dishes.
- **The name in the log:** no word from the `milk` list, anywhere in the name, and every word has to be explained, either by the table name or by the `plain` list ("raw", "frozen", "organic"). A word nobody listed leaves the entry unknown, so "chicken korma" matched to chicken stays unknown even though "korma" could have been missed. Numbers count as words, because they can name a dish; percentages and pack weights don't. A first part sharing no word with the table name is read as a brand and skipped, as long as what's left still names the food, not just "raw".

**Own table:** foods the published tables lack. A published figure always comes first, so only gaps get a row. Plain foods (`reference/own_foods.csv`) give a figure with a source. A dish (`reference/own_dishes.csv`) gives a published recipe in `reference/own_dish_ingredients.csv`, with one table food per ingredient, and the figure is worked out at load: the amount in all ingredients ÷ the weight of the finished dish. A component with any ingredient's figure unknown stays unknown for that dish. The AI matcher sees these rows like any other, and the report names entries using a typical recipe.

**What dishes contain:** each common dish stores its recipe once, and each ingredient is tagged once with the allergens and triggers it typically contains. A dish inherits its ingredients' tags, so a new trigger never means redoing dishes. Separate "often" tags, each with a reason, cover versions that vary and what a dish is usually served with. A dish with a published figure keeps it; any other dish gets a figure for a component only when every ingredient's figure is known. Tags for fructans, GOS, wheat and gluten, and coffee feed the scoring (G20). Allergen tags aren't shown to users, and no tag never means "free from".

### 1.4 Coverage: what counts as observed time

Time counts only when both logs cover it. Food coverage = days with at least one entry; within a logged day, a meal that isn't logged counts as "ate nothing" (the tool never assumes food on the user's behalf; no partial-day rule). Symptom coverage = a period the user **must declare** (`--symptoms-from`, `--symptoms-to`: "I logged every symptom between these dates"). An optional `--exclude-days` file lets the user remove days that would mislead (illness, travel, not logging). A moment is excluded if its own day or any day in its look-back is uncovered or excluded.

### 1.5 Food times

Every food entry is kept and given the best available time, using this fallback order:

| Tier | Rule | Example |
|---|---|---|
| 1. Logged | Use the time Cronometer recorded | Oats, 8:02am |
| 2. Same meal, same day | Borrow the time from other foods in the same meal `Group` on the same day | Blueberries in breakfast, no time → 8:02am, like the oats |
| 3. Usual time for that meal | If nothing else in that meal has a time, use **this user's typical** time for that meal, calculated from all their timed entries in that group | Typical breakfast = 8:15am |
| 4. Day only | If the user has no timed entries at all, use the date only | A new user with no times |

Every row carries a **`time_source`** label (`logged` / `same_meal` / `usual_time` / `day_only`), so results can show how much of the evidence rests on estimated times (e.g. "eaten before 5 of 6 episodes, 2 with estimated times").

### 1.6 Safety gate

Symptoms must use a fixed list of words (`reference/symptom_words.csv`, with common variants and plurals); anything else stops the run, so nothing unrecognised is analysed. Before matching, text is normalised (look-alike Unicode folded, invisible characters removed). A first pass scans the symptoms file (up to its 5 MB limit; every error message also repeats the NHS advice) for red-flag words and stems (blood, bleed, black, weight, lump…) before any other check, so a broken file or a bad date can never hide them; the per-row check repeats this. The list can't be edited to switch a danger sign off (codes and stems are checked when it loads). Red flags are checked before scoring: if any are logged, the report shows urgent advice and **no suspects** (G14). The run also stops if the red-flag list itself is missing or incomplete.

**Display:** show only the advice that matches the sign found, in plain, calm words (not the whole NHS list, which would alarm people). For blood, first ask whether it could be period blood.  When a danger sign is written in the user's own words rather than as an exact list phrase, the advice adds an emergency check listing the NHS 999 conditions.

---

## 2. Global rules (apply to every component)

| # | Rule | Our value | Measured or estimated | Reasoning |
|---|---|---|---|---|
| G1 | Maximum food look-back before a symptom | 24 h | Measured | No card's window runs past 24 h, the longest being wheat and gluten at 1–24 h. Mouth-to-bottom transit takes roughly 12–48 h and clinical advice is to review the 48 h before symptoms, so a longer limit can return if a card needs it. |
| G2 | No global number. Each card's window start decides how soon after eating a symptom can be blamed on that meal. | — | — | A fixed cut-off would be wrong for some cards: coffee acts within minutes, while lactose takes at least 30 minutes (R94, R95, R96). |
| G3 | Where research gives no number, estimate from where in the gut the component acts | — | — | Small-intestine components act faster; colon (fermentation) components act slower. Estimates are approximate trends, not exact values. |
| G4 | Every value is labelled Measured or Estimated | — | — | Shows how much of a result rests on estimates, and which numbers to replace when better data arrives. |
| G5 | When unsure, windows lean wider rather than narrower | — | — | A too-narrow window silently misses a real trigger; a slightly wide one adds a little noise. Missing a trigger is the worse error, and the later testing step filters false alarms. |
| G6 | Doses add up within the window (stacking): every gram eaten inside a component's look-back window before a symptom counts, however many meals it came from. Dose thresholds on the cards are for this total. | Sum of grams | Measured (principle) | Several small servings in one sitting add up, and tolerance thresholds differ by person. Grams matter, not just whether a food was eaten. |
| G7 | Component grams come from a hard-coded reference table; AI only matches food names | — | — | AI is good at understanding messy food names and bad at exact quantities (it produces confident, made-up numbers). |
| G8 | Reference table starts with the most common triggers and grows over time | — | — | No need to cover every food on day one. |
| G9 | Foods not in the reference table are shown as "unknown" | — | — | A silent gap could hide the real trigger. |
| G11 | Days with no food logged are left out. Within a logged day, a meal that isn't logged counts as "ate nothing". | No partial-day rule | — | A whole unlogged day must never look like a day of eating nothing. Within a day, the tool can't assume food on the user's behalf: logging is the user's responsibility, and onboarding tells them so. |
| G12 | Each run reads the whole log from scratch, so nothing is counted twice | — | — | A later version with stored data must replace the days a new upload covers. |
| G13 | How confident we are in a suspect: evidence for raises confidence, counter-examples lower it, no fixed number of occasions (1.1) | See 1.1 | — | **Must account for a background rate:** in a blinded trial, about 1 in 4 patients reacted to a placebo sugar, so some symptoms follow *any* food. A suspect has to beat that background, not just appear before symptoms. Counter-examples do this: a food that also turns up on symptom-free days sinks. The planted-trigger test checks the maths. |
| G14 | Red-flag symptoms override everything: urgent medical advice, no analysis | List: NHS IBS symptoms, bleeding from the bottom, vomiting blood (`reference/symptom_words.csv`) | Measured (NHS) | Danger signs (e.g. blood in stool, black stool, vomiting blood) need a doctor, not a suspect list. |
| G16 | Never present a suspect as certain, and word results so they don't create an expectation of symptoms | — | Measured (principle) | About 1 in 4 people react to a placebo sugar in blinded trials. Telling someone "this food causes your symptoms" can itself bring symptoms on (the nocebo effect). Results are possibilities to test, in calm language. |
| G17 | Look-back windows are measured from symptom **onset**; a long-lasting symptom is one episode, not one per hour. Symptoms that start less than 3 hours apart are one episode. | — | — | A symptom can start within hours and last much longer. Logging every hour of one episode as new symptoms would wrongly blame foods eaten after it started. |
| G18 | Each card has an **evidence strength** (Strong / Moderate / Weak / Very weak) that limits how a suspect can be shown | Weak → "soft flag", capped at 60%, shown as a weaker lead; Very weak → "worth watching" only (cap 60%, no %) | — | Some components have blinded trials behind them (fructans, mannitol); others rest on less. The same pattern in the data shouldn't sound equally certain for both (G16). No v1 card is Weak or Very weak: histamine and non-nutritive sweeteners moved to v2, and wheat and gluten is Moderate, so it shows clearly enough to get people to a GP. |
| G19 | If wheat and gluten or fructans reaches the testing point (50%), advise seeing a GP about a coeliac test **before** cutting out gluten. A guided experiment never removes wheat, rye or barley until coeliac disease is ruled out. | — | Measured (NICE) | A coeliac test is only accurate while the person is still eating gluten (NICE: some gluten in more than one meal every day for at least 6 weeks before testing). Cutting it out first can hide the disease. Bread contains both fructans and gluten; counter-examples (e.g. onion and garlic with no symptoms) help tell them apart. |
| G21 | Too frequent to find a pattern. When symptoms happen at least 3 times a week and nothing reaches the testing point, the report says so, names the foods eaten on most days that a card checks, and points to a GP or dietitian. Wheat on most days adds the coeliac message. | At least 3 episodes a week over usable days; at least 14 usable days; most days means over half | Estimated | The detective should say when it can't find a pattern, not leave someone stuck. A symptom count alone isn't enough: at about 5 symptoms a week the planted test still found lactose and fructans every time. |
| G20 | Where research tests no threshold, a card's dose is about one normal portion, at or under the lowest amount tested, with one working figure per tag and no per-food table | Fructans 100 g, GOS 90 g, wheat and gluten 60 g of tagged food; excess fructose 6 g | Estimated | No open table measures these across everyday foods, and figures for one food disagree. A food's whole weight counts if it's tagged, so the tool can start rough and get more accurate over time. |
