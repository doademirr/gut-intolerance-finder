"""Find the reference foods whose names look most like a food-log name (D8, D14).

This only shortlists candidates; the AI matcher (gut_finder/matching.py) decides which one, if any, is the
same food. Word search with BM25, plus three adjustments found while building the test set (4 Nov 2026):
- marketing and serving words ("organic", "fresh", "original") are ignored;
- a table name starts with the food itself ("Milk, cow, …"), so a match on that first part scores extra;
- a log name may start with a brand ("McDonald's Australia, McCafe, Latte"), so the search also runs with the
  first part weakened and the two result lists are interleaved.
A food can also be searched under several names (its log name plus the AI's plain rewrites): shortlist() takes
each search's best result in turn, then each one's second best, and so on. So a food that ranks well in any one
search reaches the shortlist. Rewarding foods found by every search does the opposite: names sharing only a common
word ("Apricot, raw") then beat the exact row ("Bok choy, raw"), which only one search found.
"""
import math
import re
import unicodedata
from collections import Counter, defaultdict

SPELLING = {"yoghurt": "yogurt", "yoghurts": "yogurt", "capsicum": "pepper", "capsicums": "pepper",
            "courgette": "zucchini", "aubergine": "eggplant", "prawn": "shrimp", "prawns": "shrimp",
            "scallions": "scallion", "coriander": "cilantro"}
FILLER = {"organic", "australian", "australia", "pure", "premium", "original", "natural", "flavour", "flavor",
          "flavoured", "flavored", "style", "fine", "plus", "cal", "small", "medium", "large", "typically",
          "used", "fresh", "all", "commercial", "variety", "the", "and", "or", "with", "of", "in", "a"}
HEAD_BONUS = 3.0
BRAND_WEIGHT = 0.3
MAX_QUERY_CHARS = 300
POOL = 50                       # results taken from each search before merging


def words(text):
    text = unicodedata.normalize("NFKD", str(text)[:MAX_QUERY_CHARS])
    text = "".join(ch for ch in text if not unicodedata.combining(ch)).lower()
    out = []
    for w in re.sub(r"[^a-z0-9]+", " ", text).split():
        w = SPELLING.get(w, w)
        if len(w) > 3 and w.endswith("ies"):
            w = w[:-3] + "y"
        elif len(w) > 3 and w.endswith("es") and w[-3] in "sxzh":
            w = w[:-2]
        elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class Search:
    def __init__(self, names, k1=1.2, b=0.75):
        self.docs = [words(n) for n in names]
        self.heads = [set(words(n.split(",")[0])) - FILLER for n in names]
        self.avg = (sum(map(len, self.docs)) / len(self.docs)) if self.docs else 1
        df = Counter(w for d in self.docs for w in set(d))
        n = len(self.docs)
        self.idf = {w: math.log(1 + (n - c + 0.5) / (c + 0.5)) for w, c in df.items()}
        self.index = defaultdict(list)
        for i, d in enumerate(self.docs):
            for w in set(d):
                self.index[w].append(i)
        self.k1, self.b = k1, b

    def _scores(self, query, weak=frozenset()):
        q = set(words(query)) - FILLER
        scores = defaultdict(float)
        for w in q:
            weight = BRAND_WEIGHT if w in weak else 1.0
            for i in self.index.get(w, ()):
                d = self.docs[i]
                tf = d.count(w)
                scores[i] += weight * self.idf[w] * tf * (self.k1 + 1) / (
                    tf + self.k1 * (1 - self.b + self.b * len(d) / self.avg))
        food_words = q - weak   # a brand word never earns the "starts with the food" bonus
        for i in list(scores):
            if self.heads[i] and self.heads[i] <= food_words:
                scores[i] += HEAD_BONUS
        return scores

    def top(self, query, k=10):
        """Indexes of the k best candidates, best first."""
        def ranked(s):
            return sorted(s, key=lambda i: (-s[i], len(self.docs[i]), i))
        parts = [p.strip() for p in str(query)[:MAX_QUERY_CHARS].split(",")]
        full = ranked(self._scores(query))
        if len(parts) == 1:
            return full[:k]
        weak = ranked(self._scores(query, frozenset(words(parts[0]))))
        out = []
        for a, b in zip(weak + [None] * k, full + [None] * k):
            for i in (a, b):
                if i is not None and i not in out:
                    out.append(i)
            if len(out) >= k or (a is None and b is None):
                break
        return out[:k]

    def shortlist(self, name, terms=(), k=20):
        """Indexes of the k best candidates across the name and its plain rewrites, best first."""
        results = [self.top(query, POOL) for query in (name, *terms)]
        out = {}                    # a dict keeps the order and drops repeats
        for rank in range(POOL):
            for found in results:
                if rank < len(found):
                    out.setdefault(found[rank])
        return list(out)[:k]
