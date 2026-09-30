"""
Find our target companies inside free text (vendor reference pages, job ads, news).

Matching is deliberately conservative: a company is only "found" when its distinctive name appears
as whole words. Legal suffixes (B.V., N.V., Holding, Group...) are ignored on both sides, and names
that are too short or too generic to be trusted are never matched -- a wrong customer claim is worse
than a missed one.
"""
import re
import unicodedata

_STRIP = {"bv", "nv", "vof", "cv", "holding", "holdings", "group", "groep", "international", "nederland",
          "netherlands", "europe", "the", "koninklijke", "royal", "de", "het", "en", "and", "co", "ltd", "gmbh", "sa"}
_TOO_GENERIC = {"nederlandse", "nederlands", "dutch", "gemeente", "stichting", "vereniging", "services", "solutions",
                "technology", "technologies", "systems", "consulting", "industries", "energie", "logistics",
                "transport", "bouw", "food", "foods", "trading", "management", "capital", "finance", "media",
                "health", "care", "bank", "verzekeringen", "groothandel", "retail", "fashion", "automotive"}


def fold(s):
    """lower-case, accent-free, punctuation -> single spaces."""
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower().replace("&", " en ")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", s)).strip()


_LEGAL = re.compile(r"(?<![a-z0-9])(?:b v|n v|v o f|c v|b\.v|n\.v)(?![a-z0-9])")


def name_key(name):
    f = _LEGAL.sub(" ", fold(name))          # 'B.V.' / 'N.V.' fold to 'b v' / 'n v'
    toks = [t for t in f.split() if t not in _STRIP]
    key = " ".join(toks)
    if len(key) < 5 or key in _TOO_GENERIC or (len(toks) == 1 and toks[0] in _TOO_GENERIC):
        return None
    return key


def build_index(companies):
    """[{kvk, name, legalName, tradeNames}] -> {key: kvk}. A key that would belong to two companies is dropped."""
    idx, clash = {}, set()
    for c in companies:
        for nm in [c.get("name"), c.get("legalName"), *(c.get("tradeNames") or [])]:
            k = name_key(nm) if nm else None
            if not k or not c.get("kvk"):
                continue
            if k in idx and idx[k] != c["kvk"]:
                clash.add(k)
            idx.setdefault(k, c["kvk"])
    for k in clash:
        idx.pop(k, None)
    return idx


def find_companies(text, index):
    """text -> {kvk: (key, start, end)} for companies whose name appears as whole words."""
    t = _LEGAL.sub(" ", fold(text))
    found = {}
    for key, kvk in index.items():
        if key not in t:
            continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", t)
        if m and kvk not in found:
            found[kvk] = (key, m.start(), m.end(), t)
    return found


def snippet(t, start, end, pad=90):
    return "..." + t[max(0, start - pad):min(len(t), end + pad)] + "..."
