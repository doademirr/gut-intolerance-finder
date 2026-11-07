# Food tables: sources and credits

The food values in this folder come from three published food composition tables. They're kept exactly as
published, apart from choosing columns. `gut_finder/build_food_tables.py` rebuilds them from the downloads.
How the engine reads them (trace = 0, "N" and blanks = unknown, per-100 ml rows = unknown) is set in
`sql/03_load_reference.sql` and explained in D14.

| File | Source | Licence |
|---|---|---|
| `usda_sr_legacy.csv` | U.S. Department of Agriculture, Agricultural Research Service. FoodData Central: SR Legacy (April 2018). fdc.nal.usda.gov | Public domain (CC0 1.0) |
| `uk_cofid_2021.csv` | Public Health England (2021). McCance and Widdowson's The Composition of Foods Integrated Dataset 2021. gov.uk | Open Government Licence v3.0 |
| `au_afcd_r3.csv` | Food Standards Australia New Zealand (2025). Australian Food Composition Database – Release 3. foodstandards.gov.au | FSANZ Data User Licence Agreement (CC BY-SA 3.0 AU based); see `au_afcd_r3_LICENCE.md` |

Contains public sector information licensed under the Open Government Licence v3.0.

Dish recipes in `reference/own_dish_ingredients.csv` come from USDA FNDDS 2019–2020 (R25), AUSNUT 2023 (R31), © Food Standards Australia New Zealand, and the published recipes R32–R36, all in `REFERENCES.md`. FSANZ doesn't endorse this project.

Lactose for some foods with a blank published figure is in `reference/own_figures.csv`, each with its source: copied from another table here, or from R37 and R38 in `REFERENCES.md`. The tables themselves aren't changed.

## Changes made

- **All three:** only the food ID, name, lactose, sugar alcohols, fructose and glucose are kept (sugar
  alcohols only exist in the Australian table). Numbers are written as plain decimals; "Tr" and "N" are kept as published.
- **Australian density:** `au_afcd_r3_density.csv` keeps the published specific gravity of each liquid from the
  AFCD Food Details file. The table gives solids 0, so they're left out.
- **UK:** values are per 100 g except alcoholic drinks (food groups starting with Q), which the table gives per
  100 ml; those rows are marked `100ml`. The published file gives two foods the same code (13-669, roasted
  aubergine and raw watercress), so repeated codes get their order in the file added (`13-669#1`, `13-669#2`).
- **USDA:** lactose is nutrient 1013, fructose 1012, glucose 1011. A food with no lactose record has a blank (unknown), not 0.

Food composition values are averages and vary between batches and brands. None of these organisations
endorses this project.
