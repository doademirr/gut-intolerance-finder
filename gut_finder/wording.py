"""Everything the user reads, in one place, so it can be reviewed without reading code.

Principle: every score or status the user sees comes with a plain-words "why",
so nothing looks broken. Safety messages show only the advice that matches what was logged, in calm
words (D17).
"""

# Cards counted by the weight of tagged food, not grams of the component itself.
DOSE_OF = {"fructans": " of foods with fructans", "gos": " of foods with GOS",
           "wheat_gluten": " of wheat, rye or barley foods", "coffee": " of coffee"}

# Shown when two suspects mostly happened together, so the tool can't tell which one it is.
TOGETHER = "Most of the times symptoms followed, you also had {other}, so the tool can't tell them apart yet."
TOGETHER_ABOVE = 0.5    # the share of a suspect's clues that were also clues for the other suspect

# Shown once above the suspects.
TESTING_NOTE = ("50% doesn't mean we're sure. The higher the score, the more likely we think it's a trigger. "
                "It's up to you: test now, or keep logging and wait for a higher score.")

SUSPECTS_CAN_CHANGE = ("Suspects can change as you log more. The ones that keep coming up matter most, "
                       "and the rest should drop away.")

# The second pathway (G21): symptoms are too frequent and nothing reached the testing point.
TOO_FREQUENT = "Your symptoms are too frequent for us to find a pattern from your log alone."
TOO_FREQUENT_STAPLES = "You had {foods} on most days, so one of these could be involved."
TOO_FREQUENT_NEXT = "A GP or dietitian can help you work out what to try next."
STAPLES_SHOWN = 5

# Shown whenever wheat and gluten or fructans is a possible suspect, because they share the same foods (G19).
COELIAC = "See a GP about a coeliac test before cutting out gluten. Keep eating gluten until the tests are finished."
COELIAC_CARDS = ("wheat_gluten", "fructans")
COELIAC_FROM = 0.5      # the same point as every other suggestion, so nothing is raised early

# Shown at the end of every report, and with every error that isn't about a danger sign.
WHEN_TO_GET_HELP = (
    "If you have a symptom that worries you, such as bleeding, black poo or vomiting blood, don't wait for "
    "this tool. Contact your GP or NHS 111, or call 999 in an emergency.")

# One message per danger sign (NHS guidance: IBS symptoms; bleeding from the bottom; vomiting blood).
RED_FLAG_MESSAGES = {
    "blood_in_stool": (
        "You've logged blood in your poo. If it could be period blood, change the entry and run the report "
        "again. If not, please get it checked. Call 999 or go to A&E if there's a lot of blood or it won't stop "
        "(for example, the toilet water turns red or you see large clots). If you have bloody diarrhoea, ask "
        "your GP for an urgent appointment or contact NHS 111. Otherwise, see your GP if it's been happening "
        "for 3 weeks."),
    "black_stool": (
        "You've logged black or dark red poo. Please ask your GP for an urgent appointment or contact NHS 111."),
    "weight_loss": (
        "You've logged weight loss. If you've lost a lot of weight without trying, please ask your GP for an "
        "urgent appointment or contact NHS 111."),
    "lump": (
        "You've logged a lump or swelling. If you've found a hard lump or swelling in your tummy, please ask "
        "your GP for an urgent appointment or contact NHS 111. If you meant bloating, change the word to "
        "\"bloating\" and run the report again."),
    "anaemia_signs": (
        "If you're short of breath, with a pounding heartbeat and paler skin than usual, please ask your GP "
        "for an urgent appointment or contact NHS 111."),
    "vomiting_blood": (
        "You've logged vomiting blood. Call 999 or go to A&E if you also feel unwell, confused, faint or "
        "dizzy, are breathing fast or shallowly, have cold, clammy or pale skin, tummy pain or black poo. If it's stopped "
        "and you feel otherwise well, ask your GP for an urgent appointment or contact NHS 111."),
    # Added whenever a danger sign was written in the user's own words rather than an exact list phrase:
    # free text can't be read precisely, so every NHS 999 condition is listed once.
    "emergency_check": (
        "Because this was written in your own words, please also check: call 999 or go to A&E if you're "
        "bleeding heavily or it won't stop (for example, the toilet water turns red or you see large clots), "
        "or if you've vomited blood, or vomit that looks black, brown or like coffee granules, and you also "
        "feel unwell, confused, faint or dizzy, are breathing fast or shallowly, have cold, clammy or pale "
        "skin, tummy pain or black poo."),
    "unrecognised_red_flag": (
        "One of your symptoms might describe something a doctor should check. If it does, please contact "
        "your GP or NHS 111, or call 999 if you're bleeding heavily or vomiting blood and feel faint or "
        "unwell. If not, change it to a word from the symptom list and run the report again."),
}

RED_FLAG_INTRO = ("Your symptom log includes something a doctor should look at, so this report doesn't show "
                  "any food suspects. Food patterns can wait; this can't.")

DISCLAIMER = ("Not medical advice. These are patterns in your own logs, worth testing with a GP or dietitian. "
              "They don't show that a food causes your symptoms, and people can react to a pattern they "
              "expect. The scores come from an experimental model whose settings are still provisional.")

# Why a weaker-evidence card can't score as high (G18). Shown with its result.
WEAK_EVIDENCE_WHY = {
    "histamine": ("Histamine intolerance is a debated condition with no reliable test: in a blinded trial, "
                  "people who believed they had it couldn't reproduce their own symptoms. Because the "
                  "research is thin, its score can't go above 60%."),
    "non_nutritive_sweeteners": ("No controlled study has shown that these sweeteners cause gut symptoms, so "
                                 "the tool only marks them as worth watching and never gives a score."),
}
WEAK_EVIDENCE_DEFAULT = ("The research on this component is thin, so its score can't go above 60%, however "
                         "strong the pattern in your log.")

# AI food matching (D8). Shown when food names would need sending and the user hasn't agreed yet.
MATCHING_CONSENT = (
    "{n} of your foods need looking up in the food tables. To do that, this tool sends each food's name, and "
    "nothing else (no dates, amounts or symptoms), to Anthropic's Claude AI, which picks the matching food "
    "from the published tables. The answers are saved on your computer, so a name is only sent again if "
    "its options in the tables change. "
    "Run the tool from the command line to agree.")
MATCHING_UNAVAILABLE = ("We couldn't look up {n} new foods today, so they're left out of this report for now. "
                        "We'll try again next time you run it.")
MATCHES_CORRECTED = "**Your corrections:** {n} {entries} use the food you chose."

# What the tool can't do . Shown at set-up and in every report.
LIMITATION = ("This tool reads each food as one line. It doesn't know what's inside a dish. For homemade meals, log the "
              "ingredients separately (egg, milk, flour, mince) rather than 'meat pie'. For takeaway and restaurant "
              "meals, log the closest dish: we'll use a typical value if we have one, and otherwise leave that time "
              "out of the check rather than guess. The tool checks lactose, polyols (sugar alcohols), excess fructose, "
              "fructans, GOS, wheat and gluten, and coffee. It doesn't check for eggs, nuts or other allergens, and it "
              "can't tell you a food is safe.")
LOW_SCORE = "A low score doesn't clear a food. It only means this log didn't show a pattern."

# Agreeing once at set-up. No agreement, no use.
BEST_WHEN = ("This tool works best when your symptoms come and go. If you have symptoms every day, it's harder to "
             "tell what's causing them, so we may ask you to try something to get a clearer answer.")
CONSENT_SCREEN = """Before you start

Your food and symptom logs stay on this computer. To look up a food, the tool sends its name, and nothing else
(no dates, amounts or symptoms), to Anthropic's Claude AI.

This isn't medical advice. The tool looks for patterns worth testing, and it can't diagnose anything.

Results depend on your logs, so log everything you eat and every symptom.

""" + BEST_WHEN + "\n\n" + LIMITATION + "\n"
CONSENT_ASK = "Type I agree to continue: "
CONSENT_REFUSED = "You didn't agree, so nothing was run. Your logs haven't been read."
CONSENT_NEEDS_TERMINAL = "Please run this once in a terminal to read and agree to how the tool works."

# The danger-sign stop is temporary (D20). Only that entry is confirmed, so a new sign stops the report again.
SIGN_CHECKED_HOW = ("Once a doctor or nurse has checked this, write yes next to that entry in `{file}`, saved next to "
                    "this report, and run the report again with `--checked-signs`. A new danger sign will stop the "
                    "report again.")

# Why a component has no score (one per result status).
STATUS_WHY = {
    "no_data": "no food values for it yet, so the tool can't tell how much you ate",
    "no_exposures": "you didn't have enough of it, in the time that could be checked, to test it",
    "not_enough_data": "there isn't enough symptom-free logging yet to compare against",
    "cannot_separate": ("your symptoms are so frequent that a food diary can't separate this from everything "
                        "else; a GP or dietitian can help"),
}
