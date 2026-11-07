"""Turn the engine's tables into a plain-words Markdown report (D5, D8; G9, G16, G18).

A fill-in template, no AI. Wording rules:
- suspects are patterns worth testing, never causes (G16, D5);
- weak-evidence cards never read as strong; very weak cards are only "worth watching" (G18);
- everything the engine couldn't use is listed, never hidden (G9, G11).
"""
from datetime import date

from gut_finder import wording


AMOUNT_LABELS = (("your answer", "from your answers"), ("published density", "from a published density"),
                 ("typical density", "from a typical density"), ("typical size", "from a typical size"))


def red_flag_advice(flags):
    """Only the advice that matches what was logged, once per sign, with the lines it came from (D17)."""
    out, order = [], []
    for _, code in flags:
        if code not in order:
            order.append(code)
    if "emergency_check" in order:   # the emergency check always comes last, once
        order = [c for c in order if c != "emergency_check"] + ["emergency_check"]
    for code in order:
        where = ", ".join(str(row) for row, c in flags if c == code)
        message = wording.RED_FLAG_MESSAGES.get(code, wording.RED_FLAG_MESSAGES["unrecognised_red_flag"])
        out += [f"- {message} *(Symptoms file, line {where}.)*", ""]
    return out


def md(text):
    """Make user-supplied text safe to print: one line, no control characters, Markdown escaped."""
    out = " ".join("".join(ch if ch.isprintable() else " " for ch in str(text)).split())
    for ch in "\\`*_[]<>|#=-+!":
        out = out.replace(ch, "\\" + ch)
    return out


def band(score, flag_mode, testing_point):
    if flag_mode == "watch":
        return "worth watching" if score >= testing_point else None
    if score >= 0.75 and flag_mode != "soft":
        return "strong pattern"
    if score >= testing_point and flag_mode != "soft":
        return "worth testing"
    if score >= 0.25:
        return "possible"
    return "early hint"


def _window(start_h, end_h):
    def fmt(h):
        return f"{int(h * 60)} min" if h < 1 else f"{h:g} h"
    return f"between {fmt(start_h)} and {fmt(end_h)} after eating"


def coeliac_needed(results):
    """G19: the advice shows once wheat and gluten or fructans reaches the testing point."""
    return any(r["component"] in wording.COELIAC_CARDS and (r["score"] or 0) >= wording.COELIAC_FROM for r in results)


def servings_to_check(res):
    """Foods logged as a serving or similar where the weight could change a result, most logged first.

    "1.25 servings" says nothing until we know what one serving weighs, so these are the amounts worth asking about.
    Returns (food name, unit, entries, median grams when the same food was also logged in grams)."""
    return res.con.execute("""
        SELECT e.food_name, e.unit, count(*) AS n,
               (SELECT round(median(g.grams)) FROM food_entries g
                WHERE g.food_name = e.food_name AND g.grams IS NOT NULL) AS also_logged
        FROM food_entries e
        WHERE e.entry_id IN (SELECT entry_id FROM component_intake
                             WHERE grams IS NULL AND component IN (SELECT component FROM scored_components))
        GROUP BY 1, 2 ORDER BY n DESC, 1, 2""").fetchall()


def build(res, demo=False, servings_file=None, signs_file=None):
    lines = ["# Gut Intolerance Finder: your report", ""]
    if demo:
        lines += ["> **Demo:** synthetic data and test-only foods. Not a real person, not real food values.", ""]

    if res.red_flags:   # D17: built without touching the logs, so nothing can hide it
        lines += [f"Made on {date.today():%d %B %Y}.", "", "## Please speak to a doctor first", "",
                  wording.RED_FLAG_INTRO, ""]
        lines += red_flag_advice(res.red_flags)
        if signs_file:
            lines += [wording.SIGN_CHECKED_HOW.format(file=signs_file), ""]
        return "\n".join(lines) + "\n"

    con = res.con
    q = lambda sql, *a: con.execute(sql, list(a)).fetchall()  # noqa: E731
    first, last, n_days = q("SELECT min(day), max(day), count(*) FROM day_coverage")[0]
    usable, no_food, excluded = q("SELECT count(*) FILTER (WHERE usable), count(*) FILTER (WHERE NOT food_logged), "
                                  "count(*) FILTER (WHERE excluded) FROM day_coverage")[0]
    sym_from, sym_to = q("SELECT symptoms_from, symptoms_to FROM run_settings")[0]
    n_rows, n_eps = q("SELECT (SELECT count(*) FROM symptom_onsets), (SELECT count(*) FROM symptom_episodes)")[0]

    lines += [f"Made on {date.today():%d %B %Y}. Food log: {first:%d %b %Y} to {last:%d %b %Y} "
              f"({n_days} days, {usable} usable). Symptom episodes: {n_eps} (from {n_rows} entries).", "",
              f"*{wording.DISCLAIMER}*", ""]

    testing_point = q("SELECT value FROM global_rules WHERE name = 'testing_point'")[0][0]
    rows = con.execute("""
        SELECT r.*, c.window_start_h, c.window_end_h, c.dose_threshold
        FROM component_results r JOIN component_rules c USING (component)
        ORDER BY r.score DESC NULLS LAST, r.display_name""").fetchall()
    cols = [d[0] for d in con.description]
    results = [dict(zip(cols, r)) for r in rows]
    scored = [r for r in results if r["result_status"] == "scored"]

    # "Only one suspect" sentence (§1.1): strictly when true.
    def stands_alone(r):
        n = r["clues"]
        return (r["counters"] == 0 and n >= 2 and n == r["eligible_episodes"] == n_eps
                and all(o["clues"] < n for o in scored if o["component"] != r["component"]))

    shown = [r for r in scored if r["score"] >= testing_point and r["flag_mode"] == "suspect"]
    weak = [r for r in scored if r["score"] >= testing_point and r["flag_mode"] != "suspect"]
    too_frequent, wheat_on_most_days = q("SELECT too_frequent, wheat_on_most_days FROM symptom_frequency")[0]
    second_pathway = bool(too_frequent) and not shown and not weak      # G21

    if coeliac_needed(results) or (second_pathway and wheat_on_most_days):
        lines += ["## Before you cut anything out", "", wording.COELIAC, ""]

    lines += ["## Suspects worth testing", ""]
    if second_pathway:
        staples = [md(r[0]) for r in q("SELECT food_name FROM staple_foods ORDER BY days DESC, food_name LIMIT ?",
                                       wording.STAPLES_SHOWN)]
        lines.append(wording.TOO_FREQUENT)
        if staples:
            foods = staples[0] if len(staples) == 1 else ", ".join(staples[:-1]) + " and " + staples[-1]
            lines.append(wording.TOO_FREQUENT_STAPLES.format(foods=foods))
        lines += [wording.TOO_FREQUENT_NEXT, ""]
    elif not shown:
        lines += ["Nothing reaches the testing point yet. That's a normal result: it can mean no pattern, "
                  "or not enough logs yet.", ""]
    if shown or weak:
        lines += [wording.TESTING_NOTE, "", wording.SUSPECTS_CAN_CHANGE, ""]

    def together(r):
        """The other suspects this one mostly happened alongside (D26)."""
        suspects = {o["component"]: o["display_name"] for o in scored if o["score"] >= testing_point}
        return [suspects[other] for other, shared in q(
                    "SELECT other, shared FROM suspect_overlap WHERE component = ? ORDER BY shared DESC, other",
                    r["component"])
                if other in suspects and r["clues"] and shared / r["clues"] > wording.TOGETHER_ABOVE]

    def describe(r):
        label = band(r["score"], r["flag_mode"], testing_point)
        score = "" if r["flag_mode"] == "watch" else f"pattern score {r['score']:.0%}, "
        weak_note = " (weak evidence)" if r["flag_mode"] == "soft" else ""
        lines.extend([f"### {md(r['display_name'])}: {score}{label}{weak_note}", "",
                  f"- {r['clues']} of your {r['n_exposures']} times above {r['dose_threshold']:g} g"
                  f"{wording.DOSE_OF.get(r['component'], '')} were followed "
                  f"by symptoms {_window(r['window_start_h'], r['window_end_h'])}.",
                  f"- Symptoms followed {r['background_rate']:.0%} of comparable stretches without it.",
                      ])
        if r["estimated_time_exposures"]:
            lines.append(f"- {r['estimated_time_exposures']} of these times rely on estimated meal times.")
        for other in together(r):
            lines.append("- " + wording.TOGETHER.format(other=md(other)))
        if stands_alone(r):
            lines.append(f"- This component matched all {r['clues']} recorded symptom episodes, with no recorded "
                         f"symptom-free exposures above its dose. No other tracked component matched all "
                         f"{r['clues']}.")
        if r["checked_share"] < 0.9:
            lines.append(f"- Only {r['checked_share']:.0%} of your usable logged time could be checked for this "
                         f"component (foods not recognised, values missing or amounts not in grams).")
        if r["flag_mode"] != "suspect":
            lines.append("- " + wording.WEAK_EVIDENCE_WHY.get(r["component"], wording.WEAK_EVIDENCE_DEFAULT))
        if r["status"] != "Agreed":
            lines.append("- Some of this card's values are still provisional.")
        lines.append("")

    for r in shown:
        describe(r)
    if weak:
        lines += ["## Weaker leads", "",
                  "These rest on weaker research, so they can't score as high or count as strong patterns.", ""]
        for r in weak:
            describe(r)

    lines += ["## Everything else that was checked", "", wording.LOW_SCORE, "",
              "| Component | Result | Times above dose | Followed by symptoms |", "|---|---|---|---|"]
    weak_in_table = []
    for r in results:
        if r in shown or r in weak:
            continue
        if r["result_status"] == "scored":
            text = band(r["score"], r["flag_mode"], testing_point) or "no pattern"
            text = text if r["flag_mode"] == "watch" else f"{r['score']:.0%}, {text}"
            if r["flag_mode"] != "suspect":
                text += " (weak evidence, see below)"
                weak_in_table.append(r)
        else:
            text = wording.STATUS_WHY[r["result_status"]]
        lines.append(f"| {md(r['display_name'])} | {text} | {r['n_exposures']} | {r['clues']} |")
    for r in weak_in_table:
        lines += ["", f"*{md(r['display_name'])}:* " +
                  wording.WEAK_EVIDENCE_WHY.get(r["component"], wording.WEAK_EVIDENCE_DEFAULT)]
    not_covered = q("""SELECT display_name FROM component_rules
                       WHERE component NOT IN (SELECT component FROM component_results) ORDER BY display_name""")
    if not_covered:
        lines += ["", "**Not covered yet** (their rules aren't finished): " +
                  ", ".join(md(n) for (n,) in not_covered) + "."]

    unknown = q("""SELECT e.food_name, count(*) AS n FROM food_entries e JOIN entry_match m USING (entry_id)
                   WHERE m.match_source = 'unknown' GROUP BY 1 ORDER BY n DESC, 1""")
    unreadable = q("""SELECT e.unit, count(*) FROM food_entries e JOIN entry_match m USING (entry_id)
                      JOIN entry_amounts a USING (entry_id)
                      WHERE m.match_source <> 'unknown' AND a.grams IS NULL GROUP BY 1 ORDER BY 2 DESC""")
    worked_out = dict(q("""SELECT amount_source, count(*) FROM entry_amounts
                           WHERE amount_source <> 'logged' GROUP BY 1"""))
    all_to_weigh = servings_to_check(res)
    to_weigh = [row[:3] for row in all_to_weigh[:5]]
    n_to_weigh = len({row[0] for row in all_to_weigh})
    no_milk = q("SELECT count(*) FROM component_intake WHERE starts_with(source, 'No milk in this food')")[0][0]
    typical = q("SELECT count(DISTINCT entry_id) FROM component_intake WHERE starts_with(source, 'Own table, typical recipe')")[0][0]
    day_only = q("SELECT count(*) FROM food_entries WHERE time_source = 'day_only'")[0][0]
    words = q("SELECT symptom, count(*) FROM symptom_onsets GROUP BY 1 ORDER BY 2 DESC, 1")
    n_entries = q("SELECT count(*) FROM food_entries")[0][0]
    ms = q("SELECT from_cache, asked, not_asked, unavailable, ran, saved FROM match_summary")[0]

    corrected = q("SELECT count(*) FROM entry_match WHERE match_source = 'corrected'")[0][0]
    lines += ["", "## About your data", "", wording.LIMITATION, "",
              f"- **Foods not recognised:** {sum(n for _, n in unknown)} of {n_entries} entries "
              f"({len(unknown)} different foods). They could hide a trigger; the full list is at the end.",
              f"- **Amounts not in grams** (recognised foods only. A food known to have none of a component counts "
              f"as 0; otherwise the dose is unknown and that time is left out, unless other foods alone reach "
              f"the dose): {sum(n for _, n in unreadable)}" +
              (" (" + ", ".join(f"{md(u)}: {n}" for u, n in unreadable[:6]) + ")" if unreadable else ""),
              *([f"- **Amounts worked out for you:** {sum(worked_out.values())} "
                 f"{'entry' if sum(worked_out.values()) == 1 else 'entries'} (" +
                 ", ".join(f"{worked_out[k]} {label}" for k, label in AMOUNT_LABELS if k in worked_out) +
                 "). Anything from a density or a typical size is an estimate, not a weighed amount."]
                if worked_out else []),
              *([f"- **Worth weighing once:** {n_to_weigh} {'food is' if n_to_weigh == 1 else 'foods are'} "
                 f"logged as a serving or similar, so we can't tell how much you had. " +
                 (f"Fill in the grams in `{servings_file}`, saved next to this report, and use it with "
                  f"`--my-servings`. " if servings_file else "Add what one weighs to `--my-servings`. ") +
                 "Most logged: " +
                 ", ".join(f"{md(f)} ({md(u)}, {n} {'entry' if n == 1 else 'entries'})" for f, u, n in to_weigh) + "."]
                if to_weigh else []),
              *([f"- **No lactose by rule:** {no_milk} {'entry' if no_milk == 1 else 'entries'} with no published "
                 f"lactose figure count as 0, because plain fruit, vegetables, grains, meat and fish don't contain "
                 f"milk. If you cooked one with milk, butter or cheese, log those too."] if no_milk else []),
              *([f"- **Typical recipes:** {typical} {'entry uses' if typical == 1 else 'entries use'} a typical recipe, "
                 f"not a measured food. Your version may hold more or less of what's checked here, so log homemade meals as "
                 f"ingredients where you can."] if typical else []),
              f"- **Entries with no usable time:** {day_only}. Their days are left out for the components they contain, "
              f"unless other foods alone reach the dose.",
              f"- **Days with no food logged** (left out): {no_food}. **Days you excluded:** {excluded}. "
              f"On logged days, a meal that isn't logged counts as nothing eaten.",
              f"- **Symptom logging period you declared:** {sym_from:%d %b %Y} to {sym_to:%d %b %Y}. Every "
              f"symptom in this period is taken to be in your file.",
              "- **Symptoms counted:** " + (", ".join(f"{md(w)} ({n})" for w, n in words) or "none")]
    if corrected:
        lines.append("- " + wording.MATCHES_CORRECTED.format(n=corrected, entries="entry" if corrected == 1 else "entries"))
    if ms[4] and ms[3]:   # AI matching ran but couldn't reach the service (D8)
        lines.append("- **" + wording.MATCHING_UNAVAILABLE.format(n=ms[2]) + "**")
    lines.append("")

    lines += ["## When to get help", "", wording.WHEN_TO_GET_HELP, ""]
    if unknown:
        lines += ["## Appendix: foods not recognised", "", "| Food | Entries |", "|---|---|"]
        lines += [f"| {md(name)} | {n} |" for name, n in unknown]
    return "\n".join(lines) + "\n"
