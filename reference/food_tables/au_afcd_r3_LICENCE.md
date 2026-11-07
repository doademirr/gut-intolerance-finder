# au_afcd_r3.csv and au_afcd_r3_density.csv: licence and notices

`au_afcd_r3.csv` and `au_afcd_r3_density.csv` are derivative works of the **Australian Food Composition Database – Release 3**
(Food Standards Australia New Zealand, December 2025). It is distributed under the FSANZ **Data User Licence
Agreement**, which is based on the Creative Commons Attribution-ShareAlike 3.0 Australia licence:
https://www.foodstandards.gov.au/science-data/monitoringnutrients/afcd/datauserlicenceagreement

Anyone who receives these files receives them under that licence directly from FSANZ. This licence covers
these files only. The rest of this repository is a separate work with its own licence.

**This work is based on Australian data, and Australian data may not be appropriate for use in other
countries.**

## Limitation of Data Statement

There are limitations associated with food composition databases. Food composition data used in the database
or databases may represent an average of the nutrient content of a particular sample of foods and ingredients,
determined at a particular time. The nutrient composition of foods and ingredients can vary substantially
between batches and brands because of a number of factors, including changes in season, processing practices
and ingredient source and methods of calculation.

## Changes made

- Kept only: Public Food Key, Food Name, Lactose (g), Sorbitol (g) and Mannitol (g), from the sheet
  "All solids & liquids per 100 g" of the file "AFCD Release 3 - Nutrient profiles.xlsx".
- Renamed the columns (`food_id`, `name`, `lactose`, `sorbitol`, `mannitol`), added `per` (always `100g`),
  removed extra spaces in names, wrote numbers as plain decimals, and sorted rows by food ID.
- No values were changed. Made with `gut_finder/build_food_tables.py`.

This project isn't connected to, sponsored by or endorsed by FSANZ.
