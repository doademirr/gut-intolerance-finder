"""Checks for AI food matching (gut_finder/search.py, gut_finder/matching.py, engine + sql/05 + report).

No real AI is called: a stand-in "ask" function plays the model, so the tests are free, fast and repeatable.
Run from the project root:  .venv/bin/python tests/test_matching.py
"""
import json
import os
import re
import stat
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from gut_finder import engine, matching, report  # noqa: E402
from gut_finder.search import Search  # noqa: E402
import test_engine as te  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix="gut_finder_matching_"))
FOODS = [("usda:1", "Milk, whole, fluid"), ("uk:2", "Coffee, cappuccino, latte"),
         ("au:3", "Coffee, flat white/latte/cappuccino, with regular fat cow's milk"),
         ("usda:4", "McDONALD'S, Cheeseburger"), ("usda:5", "McDONALD'S, BIG MAC"),
         ("uk:6", "Yogurt, Greek style, plain"), ("au:7", "Sorbet, fruit")]


def picker(want):
    """A stand-in model: picks the first option containing `want` (or 0). Records every message it gets."""
    calls = []

    def ask(system, user):
        calls.append((system, user))
        options = re.findall(r"^(\d+)\. (.*)$", user, re.M)
        for n, name in options:
            if want and want.lower() in name.lower():
                return {"best": int(n), "reason": "stand-in"}
        return {"best": 0, "reason": "stand-in: none"}
    ask.calls = calls
    return ask


def cache(name="c.json"):
    path = TMP / name
    path.unlink(missing_ok=True)
    return matching.MatchCache(path)


# --- Search ------------------------------------------------------------------------------------------------

def test_search_finds_the_food_behind_a_brand():
    s = Search([n for _, n in FOODS])
    top = [FOODS[i][1] for i in s.top("McDonald's Australia, McCafe, Latte, Small", 3)]
    assert any("latte" in t.lower() for t in top[:2]), top
    assert Search([]).top("anything") == []


def test_an_exact_row_found_by_one_search_beats_names_that_only_share_a_common_word():
    filler = [f"Fruit{i}, raw" for i in range(30)]
    names = filler + ["Bok choy, raw", "Pak choi, steamed", "Cabbage, chinese (pak-choi), raw"]
    s = Search(names)
    picked = [names[i] for i in s.shortlist("Bok Choy, Raw", ["Pak choi, raw", "Chinese cabbage (pak-choi), raw"], 5)]
    assert "Bok choy, raw" in picked and "Cabbage, chinese (pak-choi), raw" in picked, picked


def test_each_search_gets_its_turn_and_nothing_is_listed_twice():
    s = Search(["Milk, whole", "Cheese, cheddar", "Bread, white"])
    picked = s.shortlist("milk", ["cheese", "milk"], 3)
    assert sorted(picked) == [0, 1] and picked[0] == 0, picked


# --- Matching ----------------------------------------------------------------------------------------------

def test_answers_become_matches_and_zero_means_none():
    found, rep = matching.match_names(["Full Cream Milk", "Choc Sorbet"], FOODS, cache(), picker("milk, whole"),
                                      consented=True)
    assert found["Full Cream Milk"]["food"] == "usda:1"
    assert found["Choc Sorbet"]["food"] is None
    assert rep["asked"] == 2 and not rep["unavailable"]


def test_untrustworthy_answers_leave_the_food_unknown_and_unsaved():
    bad_answers = ({"best": 99, "reason": "x"}, {"best": -1, "reason": "x"}, {"best": True, "reason": "x"},
                   {"best": "1", "reason": "x"}, {"best": 1}, {"best": 1, "reason": []},
                   {"best": 1, "reason": "x", "extra": 1}, {}, ["1"], None)
    for bad in bad_answers:
        c = cache()
        found, rep = matching.match_names(["Full Cream Milk"], FOODS, c, lambda s, u, b=bad: b, consented=True)
        assert "Full Cream Milk" not in found and "Full Cream Milk" not in c.entries, bad
        assert rep["asked"] == 1


def test_only_the_name_and_options_are_sent():
    ask = picker("milk")
    matching.match_names(["Full Cream Milk"], FOODS, cache(), ask, consented=True)
    system, user = ask.calls[0]
    assert system == matching.SYSTEM
    assert user.startswith("Diary entry: Full Cream Milk\n\nOptions:\n1. ")
    assert all(re.fullmatch(r"\d+\. .+", line) for line in user.split("\n")[3:]), user


def test_saved_answers_are_reused_and_private():
    c = cache("reuse.json")
    first = picker("milk, whole")
    matching.match_names(["Full Cream Milk"], FOODS, c, first, consented=True)
    mode = stat.S_IMODE(os.stat(c.path).st_mode)
    assert mode == 0o600, oct(mode)
    again = picker("milk, whole")
    found, rep = matching.match_names(["Full Cream Milk"], FOODS, matching.MatchCache(c.path), again,
                                      consented=False)          # nothing new to send: no consent needed
    assert again.calls == [] and rep["from_cache"] == 1 and found["Full Cream Milk"]["food"] == "usda:1"


def test_saved_answer_for_a_food_no_longer_in_the_tables_is_asked_again():
    c = cache("stale.json")
    c.fingerprint = matching.fingerprint()                         # same model and search: only the entry check applies
    c.entries["Full Cream Milk"] = {"food": "usda:999", "reason": "old", "shown": "from an older table"}
    ask = picker("milk, whole")
    found, _ = matching.match_names(["Full Cream Milk"], FOODS, c, ask, consented=True)
    assert len(ask.calls) == 1 and found["Full Cream Milk"]["food"] == "usda:1"


def test_saved_answer_that_was_never_an_option_is_asked_again():
    c = cache("offlist.json")
    c.fingerprint = matching.fingerprint()
    real_shortlist = [FOODS[i] for i in Search([n for _, n in FOODS]).shortlist("Full Cream Milk", (), matching.N_OPTIONS)]
    assert "usda:5" not in {k for k, _ in real_shortlist}                     # Big Mac isn't on milk's shortlist
    c.entries["Full Cream Milk"] = {"food": "usda:5", "reason": "tampered", "shown": matching.shown_hash(real_shortlist)}
    ask = picker("milk, whole")
    found, rep = matching.match_names(["Full Cream Milk"], FOODS, c, ask, consented=True)
    assert len(ask.calls) == 1 and found["Full Cream Milk"]["food"] == "usda:1" and rep["from_cache"] == 0


def test_save_never_overwrites_a_file_that_appeared_or_changed_during_the_run():
    # 1. No file at the start; another program writes one while the AI is being asked.
    path = TMP / "appeared.json"
    path.unlink(missing_ok=True)
    c = matching.MatchCache(path)
    inner = picker("milk, whole")

    def ask(system, user):
        path.write_text('{"someone": "else"}')
        return inner(system, user)
    found, rep = matching.match_names(["Full Cream Milk"], FOODS, c, ask, consented=True)
    assert path.read_text() == '{"someone": "else"}' and rep["saved"] is False
    assert found["Full Cream Milk"]["food"] == "usda:1"                 # this run still uses the answer
    # 2. Our file at the start; it is swapped for another file during the run.
    path = TMP / "swapped.json"
    path.unlink(missing_ok=True)
    matching.match_names(["Full Cream Milk"], FOODS, matching.MatchCache(path), picker("milk"), consented=True)
    c = matching.MatchCache(path)

    def swap(system, user):
        other = TMP / "other.tmp"
        other.write_text("precious")
        os.replace(other, path)
        return inner(system, user)
    _, rep = matching.match_names(["Greek Yoghurt"], FOODS, c, swap, consented=True)
    assert path.read_text() == "precious" and rep["saved"] is False
    # 3. Normal case: saved.
    _, rep = matching.match_names(["Full Cream Milk"], FOODS, cache("normal.json"), picker("milk"), consented=True)
    assert rep["saved"] is True


def raises(exc, fn, *a):
    try:
        fn(*a)
    except exc:
        return
    raise AssertionError(f"didn't raise {exc.__name__}")


def test_cache_path_must_be_a_real_saved_matches_file():
    raises(matching.CacheError, matching.MatchCache, TMP / "food.csv")              # wrong kind of name
    other = TMP / "notes.json"
    other.write_text('{"date": "2026-01-01"}')                                     # someone else's JSON
    raises(matching.CacheError, matching.MatchCache, other)
    garbage = TMP / "garbage.json"
    garbage.write_text("{not json")
    raises(matching.CacheError, matching.MatchCache, garbage)
    assert garbage.read_text() == "{not json"                                      # left untouched
    link = TMP / "link.json"
    link.unlink(missing_ok=True)
    link.symlink_to(other)
    raises(matching.CacheError, matching.MatchCache, link)
    fifo = TMP / "fifo.json"
    fifo.unlink(missing_ok=True)
    os.mkfifo(fifo)
    raises(matching.CacheError, matching.MatchCache, fifo)                         # refused, not a hang


def test_damaged_entries_are_dropped_not_trusted():
    path = TMP / "entries.json"
    path.write_text(json.dumps({"marker": matching.CACHE_MARKER, "fingerprint": "f", "entries": {
        "ok": {"food": "usda:1", "reason": "fine", "shown": "abc"}, "bad food": {"food": [], "reason": "x", "shown": "a"},
        "bad reason": {"food": "usda:1", "reason": 5, "shown": "a"}, "not a dict": "usda:1",
        "no shown": {"food": "usda:1", "reason": "x"}, "x" * 300: {"food": None, "shown": "a"}},
        "terms": {"ok": ["whole milk"], "bad": "whole milk", "worse": [1], "x" * 300: ["a"]}}))
    loaded = matching.MatchCache(path)
    assert list(loaded.entries) == ["ok"] and loaded.terms == {"ok": ["whole milk"]}
    for broken in (["broken"], "x", 1, True, None):                  # the whole list is the wrong shape
        path.write_text(json.dumps({"marker": matching.CACHE_MARKER, "fingerprint": "f", "entries": broken}))
        assert matching.MatchCache(path).entries == {}, broken


def test_a_new_row_on_a_names_shortlist_means_it_is_asked_again():
    c = cache("fingerprint.json")
    matching.match_names(["Full Cream Milk"], FOODS, c, picker("milk, whole"), consented=True)
    ask = picker("milk, whole")
    matching.match_names(["Full Cream Milk"], FOODS + [("au:8", "Milk, cow, fluid, regular fat")],
                         matching.MatchCache(c.path), ask, consented=True)
    assert len(ask.calls) == 1                      # its options changed, so the saved answer wasn't reused


def test_a_new_row_elsewhere_in_the_tables_costs_nothing():
    c = cache("elsewhere.json")
    matching.match_names(["Full Cream Milk"], FOODS, c, picker("milk, whole"), consented=True)
    ask = picker("milk, whole")
    found, rep = matching.match_names(["Full Cream Milk"], FOODS + [("own:1", "Dragon fruit, raw")],
                                      matching.MatchCache(c.path), ask, consented=False)
    assert ask.calls == [] and rep["from_cache"] == 1 and found["Full Cream Milk"]["food"] == "usda:1"


def test_a_new_model_or_instructions_mean_every_answer_is_asked_again():
    c = cache("newmodel.json")
    matching.match_names(["Full Cream Milk"], FOODS, c, picker("milk, whole"), consented=True)
    loaded = matching.MatchCache(c.path)
    loaded.fingerprint = "an older model"
    ask = picker("milk, whole")
    matching.match_names(["Full Cream Milk"], FOODS, loaded, ask, consented=True)
    assert len(ask.calls) == 1 and "an older model" != loaded.fingerprint


# --- Rewriting a name into plain table words -----------------------------------------------------------------

REWRITE_FOODS = [("usda:1", "Cabbage, chinese (pe-tsai), raw"), ("au:2", "Leek, raw"), ("uk:3", "Beans, raw")]


def rewriter(names):
    calls = []

    def rewrite(name):
        calls.append(name)
        return {"names": names}
    rewrite.calls = calls
    return rewrite


def test_a_regional_name_reaches_its_table_row_through_the_rewrite():
    assert Search([n for _, n in REWRITE_FOODS]).shortlist("Wombok") == []     # the name alone finds nothing
    ask = picker("cabbage")
    found, _ = matching.match_names(["Wombok"], REWRITE_FOODS, cache(), ask, consented=True,
                                    rewrite=rewriter(["Chinese cabbage, raw"]))
    assert found["Wombok"]["food"] == "usda:1"


def test_the_picker_sees_the_real_name_never_the_rewrite():
    ask = picker("cabbage")
    matching.match_names(["Wombok"], REWRITE_FOODS, cache(), ask, consented=True,
                         rewrite=rewriter(["Chinese cabbage, raw"]))
    assert ask.calls[0][1].startswith("Diary entry: Wombok\n") and "Chinese cabbage, raw" not in ask.calls[0][1]


def test_a_rewrite_is_saved_and_never_paid_for_twice():
    c = cache("terms.json")
    first = rewriter(["Chinese cabbage, raw"])
    matching.match_names(["Wombok"], REWRITE_FOODS, c, picker("cabbage"), consented=True, rewrite=first)
    again = rewriter(["Chinese cabbage, raw"])
    ask = picker("cabbage")                         # a new row: the pick is asked again, the rewrite isn't
    matching.match_names(["Wombok"], REWRITE_FOODS + [("own:9", "Cabbage, wombok, raw")], matching.MatchCache(c.path),
                         ask, consented=True, rewrite=again)
    assert first.calls == ["Wombok"] and again.calls == [] and len(ask.calls) == 1


def test_a_bad_rewrite_is_not_saved_and_the_name_is_searched_as_written():
    for bad in (None, {}, ["a"], {"names": "a"}, {"names": [1]}, {"names": ["a"], "extra": 1}):
        c = cache()
        found, _ = matching.match_names(["Beans"], REWRITE_FOODS, c, picker("beans"), consented=True,
                                        rewrite=lambda n, b=bad: b)
        assert found["Beans"]["food"] == "uk:3" and c.terms == {}, bad


def test_rewrites_are_capped_in_number_and_length():
    kept = matching.valid_terms({"names": ["  a  ", "", "b" * 101, "c", "d", "e"]})
    assert kept == ["a", "c", "d"], kept


def test_a_broken_character_in_a_rewrite_never_stops_the_save():
    assert matching.valid_terms({"names": ["\ud800", "beans"]}) == ["beans"]
    path = TMP / "surrogate.json"
    path.write_text(json.dumps({"marker": matching.CACHE_MARKER, "fingerprint": "f", "entries": {},
                                "terms": {"x": ["\ud800"]}}))
    loaded = matching.MatchCache(path)
    assert loaded.terms == {"x": []} and loaded.save()


def test_a_rewrite_is_kept_when_the_pick_after_it_fails():
    def down(system, user):
        raise matching.MatchingUnavailable("down")
    c = cache("kept.json")
    found, rep = matching.match_names(["Wombok"], REWRITE_FOODS, c, down, consented=True,
                                      rewrite=rewriter(["Chinese cabbage, raw"]))
    assert found == {} and rep["unavailable"] and c.terms == {"Wombok": ["Chinese cabbage, raw"]}
    assert matching.MatchCache(c.path).terms == {"Wombok": ["Chinese cabbage, raw"]}


def test_a_failed_rewrite_stops_the_run_like_a_failed_pick():
    def down(name):
        raise matching.MatchingUnavailable("down")
    ask = picker("cabbage")
    found, rep = matching.match_names(["Wombok"], REWRITE_FOODS, cache(), ask, consented=True, rewrite=down)
    assert found == {} and rep["unavailable"] and ask.calls == []


def test_a_busy_rewrite_is_tried_again():
    tries = []

    def busy_once(name):
        tries.append(name)
        if len(tries) == 1:
            raise matching.MatchingBusy("busy")
        return {"names": ["Chinese cabbage, raw"]}
    waits, matching.RETRY_WAITS = matching.RETRY_WAITS, (0, 0)
    try:
        found, _ = matching.match_names(["Wombok"], REWRITE_FOODS, cache(), picker("cabbage"), consented=True,
                                        rewrite=busy_once)
    finally:
        matching.RETRY_WAITS = waits
    assert len(tries) == 2 and found["Wombok"]["food"] == "usda:1"


def test_no_more_options_are_shown_than_the_limit():
    foods = [(f"usda:{i}", f"Beans, type {i}") for i in range(40)]
    ask = picker(None)
    matching.match_names(["Beans"], foods, cache(), ask, consented=True, rewrite=rewriter(["Beans, cooked"]))
    assert len(re.findall(r"^\d+\. ", ask.calls[0][1], re.M)) == matching.N_OPTIONS


def test_the_real_request_is_one_sonnet_accepts():
    import anthropic
    sent = []

    class Reply:
        stop_reason, content = "end_turn", [type("B", (), {"type": "thinking"})(),
                                            type("B", (), {"type": "text", "text": '{"names": ["a"]}'})()]

    class Fake:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            sent.append(kw)
            return Reply()
    real, anthropic.Anthropic = anthropic.Anthropic, Fake
    try:
        ask = matching.anthropic_asker()
        answer = matching.anthropic_rewriter(ask)("Wombok")
    finally:
        anthropic.Anthropic = real
    assert answer == {"names": ["a"]}               # read by block type: a thinking block can come first
    assert sent[0]["model"] == "claude-sonnet-5-5"
    assert "thinking" not in sent[0]                # {"type": "disabled"} is rejected by this model
    assert sent[0]["output_config"]["format"]["schema"] == matching.REWRITE_SCHEMA
    assert sent[0]["messages"] == [{"role": "user", "content": "Diary entry: Wombok"}]
    assert sent[0]["system"] == matching.REWRITE_SYSTEM


def test_a_refused_answer_leaves_the_food_unknown():
    import anthropic

    class Reply:
        stop_reason, content = "refusal", []

    class Fake:
        def __init__(self, **kw):
            self.messages = self

        def create(self, **kw):
            return Reply()
    real, anthropic.Anthropic = anthropic.Anthropic, Fake
    c = cache()
    try:
        found, rep = matching.match_names(["Beans"], REWRITE_FOODS, c, None, consented=True)
    finally:
        anthropic.Anthropic = real
    assert found == {} and c.entries == {} and c.terms == {} and not rep["unavailable"]


def test_long_names_are_never_cut_and_sent():
    name = "Milk, " + "x" * matching.MAX_NAME_CHARS + ", lactose-free"
    ask = picker("milk")
    found, rep = matching.match_names([name], FOODS, cache(), ask, consented=True)
    assert ask.calls == [] and name not in found and rep["too_long"] == 1


def test_time_limit_leaves_the_rest_unknown():
    old = matching.MAX_MATCH_SECONDS
    matching.MAX_MATCH_SECONDS = -1
    try:
        ask = picker("milk")
        found, rep = matching.match_names(["A milk", "B milk"], FOODS, cache(), ask, consented=True)
        assert ask.calls == [] and found == {} and rep["unavailable"] and rep["not_asked"] == 2
    finally:
        matching.MAX_MATCH_SECONDS = old


def test_no_consent_stops_before_anything_is_sent():
    ask = picker("milk")
    try:
        matching.match_names(["Full Cream Milk"], FOODS, cache(), ask, consented=False)
    except matching.ConsentNeeded as e:
        assert e.n_names == 1 and ask.calls == []
    else:
        raise AssertionError("sent without consent")


def test_service_failure_leaves_foods_unknown_and_keeps_earlier_answers():
    c = cache("fail.json")
    n = {"calls": 0}

    def flaky(system, user):                  # names are looked up in parallel, so fail by name, not by order
        n["calls"] += 1
        if not user.startswith("Diary entry: A milk"):
            raise matching.MatchingUnavailable("down")
        return {"best": 1, "reason": "ok"}
    found, rep = matching.match_names(["A milk", "B milk", "C milk"], FOODS, c, flaky, consented=True)
    assert rep["unavailable"] and rep["asked"] == 1 and rep["not_asked"] == 2
    assert "B milk" not in found and "C milk" not in found
    assert list(matching.MatchCache(c.path).entries) == ["A milk"]   # the one answer is saved


def test_many_names_are_looked_up_in_parallel_and_saved_as_they_go():
    import threading
    import time as _time
    c = cache("parallel.json")
    seen, lock, saves = set(), threading.Lock(), []
    real_save = c.save

    def slow(system, user):
        with lock:
            seen.add(threading.get_ident())
        _time.sleep(0.05)
        return {"best": 1, "reason": "ok"}

    def counting_save():
        saves.append(len(c.entries))
        return real_save()
    c.save = counting_save
    names = [f"Milk {i}" for i in range(120)]
    start = _time.perf_counter()
    found, rep = matching.match_names(names, FOODS, c, slow, consented=True)
    took = _time.perf_counter() - start
    assert len(found) == 120 and rep["asked"] == 120 and not rep["unavailable"]
    assert len(seen) > 1 and took < 120 * 0.05 / 2, (len(seen), took)    # several at once, not one by one
    assert len(saves) >= 3 and saves[0] < 120, saves                     # saved part-way, then at the end
    assert len(matching.MatchCache(c.path).entries) == 120


def test_a_busy_service_is_tried_again_and_the_food_still_gets_its_answer():
    tries = {"n": 0}

    def busy_twice(system, user):
        tries["n"] += 1
        if tries["n"] < 3:
            raise matching.MatchingBusy("overloaded")
        return {"best": 1, "reason": "ok"}
    old, matching.RETRY_WAITS = matching.RETRY_WAITS, (0.01, 0.01)
    try:
        found, rep = matching.match_names(["Full Cream Milk"], FOODS, cache("busy.json"), busy_twice, consented=True)
    finally:
        matching.RETRY_WAITS = old
    assert tries["n"] == 3 and "Full Cream Milk" in found and not rep["unavailable"], (tries, rep)


def test_a_service_that_stays_busy_stops_the_run_after_the_limited_tries():
    tries = {"n": 0}

    def always_busy(system, user):
        tries["n"] += 1
        raise matching.MatchingBusy("overloaded")
    old, matching.RETRY_WAITS = matching.RETRY_WAITS, (0.01, 0.01)
    try:
        found, rep = matching.match_names(["Milk"], FOODS, cache("busy2.json"), always_busy, consented=True)
    finally:
        matching.RETRY_WAITS = old
    assert tries["n"] == 3 and found == {} and rep["unavailable"], (tries, rep)


def test_nothing_is_sent_after_the_run_stops():
    import threading
    import time as _time
    sent, lock = [], threading.Lock()

    def down(system, user):
        with lock:
            sent.append(_time.monotonic())
            first = len(sent) == 1
        if first:
            raise matching.MatchingUnavailable("down")      # stops the run
        raise matching.MatchingBusy("overloaded")           # these would try again, but mustn't
    old, matching.RETRY_WAITS = matching.RETRY_WAITS, (0.3, 0.3)
    try:
        names = [f"Milk {i}" for i in range(40)]
        found, rep = matching.match_names(names, FOODS, cache("stop.json"), down, consented=True)
    finally:
        matching.RETRY_WAITS = old
    assert found == {} and rep["unavailable"]
    assert len(sent) <= matching.WORKERS, len(sent)         # one go each at most: no retries, no new names
    assert sent[-1] - sent[0] < 0.25, sent                  # all before the first retry would have fired


def test_the_time_limit_also_stops_requests_already_running():
    import time as _time

    def slow(system, user):
        _time.sleep(0.2)
        return {"best": 1, "reason": "ok"}
    old, matching.MAX_MATCH_SECONDS = matching.MAX_MATCH_SECONDS, 0.02
    start = _time.perf_counter()
    try:
        found, rep = matching.match_names(["Milk 1", "Milk 2", "Milk 3"], FOODS, cache("limit.json"), slow,
                                          consented=True)
    finally:
        matching.MAX_MATCH_SECONDS = old
    took = _time.perf_counter() - start
    assert found == {} and rep["unavailable"] and rep["not_asked"] == 3, rep
    assert took < 0.15, took                                # returns at the limit, not when the requests end


def test_no_retry_is_sent_after_the_time_limit():
    import time as _time
    sent = []

    def busy(system, user):
        sent.append(_time.monotonic())
        raise matching.MatchingBusy("overloaded")
    old = matching.MAX_MATCH_SECONDS, matching.RETRY_WAITS
    matching.MAX_MATCH_SECONDS, matching.RETRY_WAITS = 0.02, (0.05, 0.05)
    try:
        found, rep = matching.match_names(["Milk 1"], FOODS, cache("retry_limit.json"), busy, consented=True)
        _time.sleep(0.15)                                   # long enough for a retry to fire if one were coming
    finally:
        matching.MAX_MATCH_SECONDS, matching.RETRY_WAITS = old
    assert found == {} and rep["unavailable"] and len(sent) == 1, (found, rep, sent)


def test_an_answer_that_finishes_during_a_save_past_the_limit_is_not_used():
    import time as _time
    c = cache("late.json")
    real_save, finished = c.save, {}

    def slow_save():                                        # a save that runs past the time limit
        _time.sleep(0.08)
        return real_save()
    c.save = slow_save

    def timed(system, user):
        _time.sleep(0.01 if "Milk 0" in user else 0.04)
        finished[user.splitlines()[0]] = _time.monotonic()
        return {"best": 1, "reason": "ok"}
    old = matching.MAX_MATCH_SECONDS, matching.SAVE_EVERY
    matching.MAX_MATCH_SECONDS, matching.SAVE_EVERY = 0.03, 1
    try:
        found, rep = matching.match_names(["Milk 0", "Milk 1"], FOODS, c, timed, consented=True)
    finally:
        matching.MAX_MATCH_SECONDS, matching.SAVE_EVERY = old
    assert list(found) == ["Milk 0"] and rep["unavailable"], (found, rep)


def test_nothing_is_sent_after_a_crash_during_the_run():
    import time as _time
    c = cache("crash.json")
    sent = []

    def crash():
        raise RuntimeError("save crashed")
    c.save = crash

    def first_ok_then_busy(system, user):
        sent.append(user)
        if "Milk 0" in user:
            return {"best": 1, "reason": "ok"}
        _time.sleep(0.01)
        raise matching.MatchingBusy("overloaded")
    old = matching.RETRY_WAITS, matching.SAVE_EVERY
    matching.RETRY_WAITS, matching.SAVE_EVERY = (0.05, 0.05), 1
    try:
        try:
            matching.match_names(["Milk 0", "Milk 1"], FOODS, c, first_ok_then_busy, consented=True)
            raise AssertionError("the crash should reach the caller")
        except RuntimeError:
            pass
        n = len(sent)
        _time.sleep(0.15)                                   # long enough for a retry to fire if one were coming
    finally:
        matching.RETRY_WAITS, matching.SAVE_EVERY = old
    assert len(sent) == n, sent


def test_never_more_lookups_at_once_than_the_limit():
    import threading
    import time as _time
    now, most, lock = [0], [0], threading.Lock()

    def counted(system, user):
        with lock:
            now[0] += 1
            most[0] = max(most[0], now[0])
        _time.sleep(0.01)
        with lock:
            now[0] -= 1
        return {"best": 1, "reason": "ok"}
    matching.match_names([f"Milk {i}" for i in range(60)], FOODS, cache("ceiling.json"), counted, consented=True)
    assert most[0] == matching.WORKERS, most


def test_missing_key_fails_closed():
    old = os.environ.pop("ANTHROPIC_API_KEY", None)
    try:
        found, rep = matching.match_names(["Full Cream Milk"], FOODS, cache(), None, consented=True)
        assert rep["unavailable"] and found == {}
    finally:
        if old is not None:
            os.environ["ANTHROPIC_API_KEY"] = old


# --- End to end: engine, sql/05, report --------------------------------------------------------------------

def _logs(extra_name="Moo Dairy, Full Cream Milk"):
    food = te.filler(30)
    for d in range(1, 31):
        food.append([f"2026-01-{d:02d}", "8:00 AM", "Breakfast", extra_name, "250.00 g", "x"])
    f = te.write_csv("m_food.csv", engine.FOOD_COLUMNS, food)
    s = te.write_csv("m_symptoms.csv", engine.SYMPTOM_COLUMNS, [["2026-01-05", "10:00", "bloating", "2"]])
    return f, s


def test_engine_uses_ai_matches_for_component_grams():
    f, s = _logs()
    ask = picker("Test milk, whole")          # fixture food usda:900001, lactose 4.8 g per 100 g
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                     online_matching=True, match_cache=TMP / "e2e.json", ask=ask)
    try:
        rows = res.con.execute("""SELECT m.match_source, m.ref_food, round(ci.grams, 2) FROM food_entries e
                                  JOIN entry_match m USING (entry_id)
                                  JOIN component_intake ci USING (entry_id)
                                  WHERE e.food_name = 'Moo Dairy, Full Cream Milk' AND ci.component = 'lactose'
                                  LIMIT 1""").fetchall()
        assert rows == [("ai", "usda:900001", 12.0)], rows      # 250 g × 4.8 / 100
        text = report.build(res)
        assert "foods matched by AI" not in text and "Check your matches" not in text, "users don't check the tool's work"
        assert [u for _, u in ask.calls][0].startswith("Diary entry: Moo Dairy, Full Cream Milk")
    finally:
        res.con.close()


def test_a_match_on_nothing_but_a_brand_is_not_counted_as_no_lactose():
    # the AI matched both to chicken, but after the brand nothing in the first name says chicken
    rows = [["2026-01-01", "8:00 AM", "Breakfast", n, "100.00 g", "x"]
            for n in ("Some Brand, Organic Raw", "Some Brand, Test Chicken, Raw")]
    f = te.write_csv("m_food.csv", engine.FOOD_COLUMNS, rows)
    s = te.write_csv("m_symptoms.csv", engine.SYMPTOM_COLUMNS, [])
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-01", reference_dir=te.REF, online_matching=True,
                     match_cache=TMP / "e2e_brand.json", ask=picker("Test chicken, breast, raw"))
    try:
        got = dict(res.con.execute("""SELECT e.food_name, c.grams FROM food_entries e LEFT JOIN component_intake c
                                      ON c.entry_id = e.entry_id AND c.component = 'lactose'""").fetchall())
        assert got == {"Some Brand, Organic Raw": None, "Some Brand, Test Chicken, Raw": 0}, got
    finally:
        res.con.close()


def test_own_label_values_beat_ai_matches():
    f, s = _logs()
    m = te.write_csv("m_my.csv", engine.MY_FOOD_COLUMNS, [["full cream milk", "lactose", "5"]])
    ask = picker("Test milk, whole")
    res = engine.run(f, s, m, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                     online_matching=True, match_cache=TMP / "e2e_mine.json", ask=ask)
    try:
        src = res.con.execute("""SELECT DISTINCT m.match_source FROM food_entries e JOIN entry_match m USING
                                 (entry_id) WHERE e.food_name = 'Moo Dairy, Full Cream Milk'""").fetchall()
        assert src == [("my_foods",)], src
        assert ask.calls == []                    # nothing to look up, so nothing sent
    finally:
        res.con.close()


def test_engine_without_consent_explains_and_stops():
    f, s = _logs()
    try:
        engine.run(f, s, None, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                   online_matching=False, match_cache=TMP / "e2e_none.json", ask=picker("milk"))
    except engine.InputError as e:
        assert "command line to agree" in str(e) and "no dates, amounts or symptoms" in str(e)
    else:
        raise AssertionError("ran without consent")


def test_matched_food_without_a_value_is_unknown_not_zero():
    f, s = _logs("Bakery White Loaf")
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                     online_matching=True, match_cache=TMP / "e2e_novalue.json", ask=picker("Test bread, white"))
    try:
        rows = res.con.execute("""SELECT DISTINCT m.match_source, m.ref_food, x.unknown FROM food_entries e
                                  JOIN entry_match m USING (entry_id) JOIN entry_component x USING (entry_id)
                                  WHERE e.food_name = 'Bakery White Loaf' AND x.component = 'lactose'""").fetchall()
        assert rows == [("ai", "usda:900002", True)], rows   # matched, but the table has no lactose value
    finally:
        res.con.close()


def test_engine_refuses_a_cache_path_that_is_an_input():
    f, s = _logs()
    try:
        engine.run(f, s, None, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                   online_matching=True, match_cache=f, ask=picker("milk"))
    except engine.InputError as e:
        assert "--match-cache" in str(e)
    else:
        raise AssertionError("used the food log as the cache")
    assert f.read_text().startswith("Day,")          # the food log is untouched


def test_a_none_answer_stays_unknown_not_zero():
    f, s = _logs("Mystery Snack")
    res = engine.run(f, s, None, None, "2026-01-01", "2026-01-30", reference_dir=te.REF,
                     online_matching=True, match_cache=TMP / "e2e_unknown.json", ask=picker(None))
    try:
        unknown = res.con.execute("""SELECT bool_and(x.unknown) FROM entry_component x JOIN food_entries e
                                     USING (entry_id) WHERE e.food_name = 'Mystery Snack'""").fetchone()[0]
        assert unknown is True
    finally:
        res.con.close()


if __name__ == "__main__":
    tests = [f for name, f in sorted(globals().items()) if name.startswith("test_") and callable(f)
             and f.__module__ == __name__]
    for t in tests:
        t()
        print("ok  ", t.__name__)
    print(f"{len(tests)} passed")
