"""
Wikidata -> landing_zone/companies/wikidata_revenue.json        FREE, no API key.

Exact revenue (and employees) for the larger / well-known Dutch companies
that Wikidata has linked to a KVK number. Small companies are not in
Wikidata -- they get an ESTIMATED revenue band later instead.

Wikidata properties used: P3220 = "KvK company ID", P4496 = "NACE code rev.2"
(industry), P2139 = total revenue, P1128 = employees, P159 = headquarters.
Override with --kvk-property / --nace-property if Wikidata ever renames them.

    python fetch_wikidata.py
"""
import argparse
import re
import sys
import time

import requests

from common import LANDING, lenient_json, norm_kvk, read_json, write_json
from erp_catalog import slug

UA = {"User-Agent": "cbs-knowledge-graph-pipeline/1.0 (company layer; contact via GitHub)"}
OUT = LANDING / "wikidata_revenue.json"


def find_property(search, must_contain, what, required=True):
    """Look a Wikidata property up by name at run time instead of hard-coding its id."""
    r = requests.get("https://www.wikidata.org/w/api.php", headers=UA, timeout=30, params={
        "action": "wbsearchentities", "search": search, "type": "property",
        "language": "en", "format": "json", "limit": 10})
    r.raise_for_status()
    for hit in r.json().get("search", []):
        label = ((hit.get("label") or "") + " " + (hit.get("description") or "")).lower()
        if any(m in label for m in must_contain):
            print(f"Using Wikidata property {hit['id']} ({hit.get('label')}) for {what}")
            return hit["id"]
    if required:
        sys.exit(f"Could not find the {what} property on Wikidata; pass it explicitly")
    print(f"(no Wikidata property found for {what} -- continuing without it)")
    return None


QUERY = """
SELECT ?item ?itemLabel ?kvk ?revenue ?unitLabel ?revDate ?employees ?industryLabel ?nace ?cityLabel ?website WHERE {
  %(anchor)s
  OPTIONAL { ?item wdt:P452 ?industry . %(nace)s }
  OPTIONAL { ?item wdt:P159 ?city }
  OPTIONAL { ?item wdt:P856 ?website }   # official website -> used by erp_enrich.py --detect
  OPTIONAL { ?item p:P2139 ?rs . ?rs psv:P2139 ?rv . ?rv wikibase:quantityAmount ?revenue ; wikibase:quantityUnit ?unit .
             OPTIONAL { ?rs pq:P585 ?revDate } }
  OPTIONAL { ?item wdt:P1128 ?employees }
  FILTER NOT EXISTS { ?item wdt:P576 [] }      # dissolved / defunct companies are no sales targets
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }
}"""



# Which items to fetch. 1) companies that list a Dutch KvK number (the original route).
KVK_ANCHOR = "?item wdt:%(p)s ?kvk ."
# 2) Dutch companies WITHOUT a KvK number on Wikidata: country (or headquarters country) = Netherlands, a kind of business,
#    and either 100+ employees or a published revenue. Their KvK number is found later from the company's own website
#    (Dutch law requires it there). No KvK number yet -> the record is keyed 'wd-<QID>'.
# Two SIMPLE, selective queries (a transitive "kind of business" test over all Dutch items timed out on the public endpoint):
#   a) Dutch items with 100+ employees   b) Dutch items with a published revenue
# Non-companies that sneak in (municipalities, places ...) are dropped afterwards by their kind (NON_COMPANY).
NL_ANCHORS = [
    """?item wdt:P17 wd:Q55 .
  ?item wdt:P1128 ?e0 . FILTER(?e0 >= 100)
  FILTER NOT EXISTS { ?item wdt:%(p)s [] }
  BIND(?item AS ?kvk)""",
    """?item wdt:P17 wd:Q55 .
  ?item wdt:P2139 ?r0 .
  FILTER NOT EXISTS { ?item wdt:%(p)s [] }
  BIND(?item AS ?kvk)""",
]
NON_COMPANY = re.compile(r"(?i)gemeente|municipality|city|town|village|dorp|stad\b|country|province|provincie|human settlement|"
                         r"neighbourhood|wijk|island|eiland|river|lake|street|straat|human|person|persoon|building|gebouw|"
                         r"railway station|station\b|church|kerk|museum building|polder|waterschap|water board")

def run_sparql(query, retries=3):
    # (Wikidata's public endpoint stops queries after ~60 s; a timeout shows up here as an HTTP 500/504)
    """Public Wikidata endpoint: sometimes times out / 5xx, and labels can contain raw control characters."""
    for attempt in range(retries):
        try:
            r = requests.get("https://query.wikidata.org/sparql", headers={**UA, "Accept": "application/sparql-results+json"},
                             params={"query": query}, timeout=300)
            r.raise_for_status()
            return lenient_json(r)
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            print(f"Wikidata attempt {attempt + 1} failed ({e}); retrying in {30 * (attempt + 1)}s", flush=True)
            time.sleep(30 * (attempt + 1))


# Second, lighter query: what KIND of thing each item is ("airline", "supermarket chain") and its one-line
# description. Used only to work out the CBS industry for companies with no NACE code / industry on Wikidata.
QUERY_TYPES = """
SELECT ?item ?typeLabel ?desc WHERE {
  %(anchor)s
  OPTIONAL { ?item wdt:P31 ?type }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) IN ("en", "nl")) }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }
}"""


def collect(prop, nace, nl_mode, anchor_tpl=None):
    anchor = (anchor_tpl or KVK_ANCHOR) % {"p": prop}
    data = run_sparql(QUERY % {"anchor": anchor, "nace": nace}, retries=1 if nl_mode else 3)
    best = {}
    for b in data["results"]["bindings"]:
        qid = b["item"]["value"].rsplit("/", 1)[-1]
        kvk = ("wd-" + qid) if nl_mode else norm_kvk(b["kvk"]["value"])
        if not kvk:
            continue
        rec = best.setdefault(kvk, {"kvk": kvk, "wikidata": qid, "kvkFromWebsite": nl_mode,
                                    "name": b.get("itemLabel", {}).get("value"),
                                    "revenue": None, "revenueCurrency": None, "revenueYear": None, "employees": None,
                                    "industries": [], "naceCodes": [], "city": None, "website": None,
                                    "types": [], "descriptions": []})
        ind = b.get("industryLabel", {}).get("value")
        if ind and ind not in rec["industries"]:
            rec["industries"].append(ind)
        nc = b.get("nace", {}).get("value")
        if nc and nc not in rec["naceCodes"]:
            rec["naceCodes"].append(nc)
        if not rec["website"] and b.get("website"):
            rec["website"] = b["website"]["value"]
        if not rec["city"] and b.get("cityLabel"):
            rec["city"] = b["cityLabel"]["value"]
        if "employees" in b:
            try:
                rec["employees"] = max(rec["employees"] or 0, int(float(b["employees"]["value"])))
            except ValueError:
                pass
        if "revenue" in b:
            year = (b.get("revDate", {}).get("value") or "")[:4] or None
            if rec["revenueYear"] is None or (year and year > rec["revenueYear"]):
                rec.update(revenue=float(b["revenue"]["value"]), revenueYear=year,
                           revenueCurrency=b.get("unitLabel", {}).get("value"))
    try:
        by_qid = {r["wikidata"]: r for r in best.values()}
        for b in run_sparql(QUERY_TYPES % {"anchor": anchor}, retries=1 if nl_mode else 3)["results"]["bindings"]:
            rec = by_qid.get(b["item"]["value"].rsplit("/", 1)[-1])
            if rec is None:
                continue
            t = b.get("typeLabel", {}).get("value")
            if t and not re.fullmatch(r"Q\d+", t) and t not in rec["types"]:
                rec["types"].append(t)
            d = b.get("desc", {}).get("value")
            if d and d not in rec["descriptions"]:
                rec["descriptions"].append(d)
    except Exception as e:   # optional enrichment
        print(f"(kind/description lookup skipped: {e})")
    if nl_mode:     # drop places / municipalities / persons that carry an employee count
        for k in [k for k, r in best.items() if any(NON_COMPANY.search(t) for t in r["types"])]:
            del best[k]
    for rec in best.values():   # SPARQL row order varies between runs -> sort, so the industry picked is stable
        rec["naceCodes"].sort(); rec["industries"].sort(); rec["types"].sort(); rec["descriptions"].sort()
    return best


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvk-property", default="P3220", help='Wikidata "KvK company ID"')
    ap.add_argument("--nace-property", default="P4496", help='Wikidata "NACE code rev.2"')
    ap.add_argument("--no-nl-wide", action="store_true", help="skip the wider search (Dutch companies without a KvK number)")
    a = ap.parse_args()
    prop, nace_p = a.kvk_property, a.nace_property
    nace = f"OPTIONAL {{ ?industry wdt:{nace_p} ?nace }}" if nace_p else ""
    best = collect(prop, nace, False)
    if not a.no_nl_wide:
        have_qid = {r["wikidata"] for r in best.values()}
        for i, tpl in enumerate(NL_ANCHORS, 1):      # each query on its own: one failing must not stop the other
            try:
                extra = collect(prop, nace, True, tpl)
                new = {k: v for k, v in extra.items() if v["wikidata"] not in have_qid}
                have_qid |= {v["wikidata"] for v in new.values()}
                best.update(new)
                print(f"Wikidata (wider net, part {i}/2): {len(new):,} more Dutch companies without a KvK number on Wikidata")
            except Exception as e:
                print(f"(wider Wikidata search part {i}/2 skipped: {str(e)[:300]})")
    # Companies named by Wikipedia's industry categories (fetch_wikipedia.py): fetch their Wikidata details in batches by QID.
    cands = read_json(LANDING / "wikipedia_candidates.json", [])
    if cands and not a.no_nl_wide:
        by_q = {c["qid"]: c for c in cands}
        have_qid = {r["wikidata"] for r in best.values()}
        todo = [q for q in by_q if q not in have_qid]
        added = 0
        for i in range(0, len(todo), 120):
            batch = todo[i:i + 120]
            tpl = "VALUES ?item { " + " ".join("wd:" + q for q in batch) + " }\n  BIND(?item AS ?kvk)"
            try:
                extra = collect(prop, nace, True, tpl)
            except Exception as e:
                print(f"(Wikipedia candidates batch {i // 120 + 1} skipped: {str(e)[:200]})")
                continue
            for k, v in extra.items():
                if v["wikidata"] in have_qid:
                    continue
                c = by_q.get(v["wikidata"], {})
                v["listSources"] = [c["source"]] if c.get("source") else []
                v["wikipediaSection"] = c.get("section")
                best[k] = v
                have_qid.add(v["wikidata"])
                added += 1
            time.sleep(1)
        print(f"Wikidata (from Wikipedia categories): {added:,} more companies of {len(todo):,} candidates")
    if cands:        # companies already found by KvK number also get their Wikipedia citation / industry hint
        by_q = {c["qid"]: c for c in cands}
        for r in best.values():
            c = by_q.get(r["wikidata"])
            if c and not r.get("listSources"):
                r["listSources"] = [c["source"]]
                r["wikipediaSection"] = c.get("section")
    # Journal / ranking / association lists (import_lists.py): every company keeps the list it came from.
    listed = read_json(LANDING / "listed_companies.json", [])
    if listed:
        by_q = {r["wikidata"]: r for r in best.values() if r.get("wikidata")}
        merged_n = new_n = 0
        for L in listed:
            res = L.get("resolved") or {}
            cite = [f"{s['source']}" + (f" (#{s['rank']})" if s.get("rank") else "") + (f" {s['year']}" if s.get("year") else "")
                    for s in L.get("sources", [])]
            rec = by_q.get(res.get("qid")) if res.get("qid") else None
            if rec is None:
                kvk = res.get("kvk") or ("list-" + slug(L["name"]))
                rec = best.get(kvk)
            if rec is None:
                kvk = res.get("kvk") or ("list-" + slug(L["name"]))
                rec = best[kvk] = {"kvk": kvk, "wikidata": res.get("qid"), "kvkFromWebsite": not res.get("kvk"), "name": L["name"],
                                   "revenue": None, "revenueCurrency": None, "revenueYear": None, "employees": None,
                                   "industries": [L["industryText"]] if L.get("industryText") else [], "naceCodes": [],
                                   "city": None, "website": None, "types": [], "descriptions": []}
                new_n += 1
            else:
                merged_n += 1
            if rec.get("employees") is None:
                rec["employees"] = L.get("staff") or res.get("staff")
            if rec.get("revenue") is None:
                lr = L.get("revenue") or res.get("revenue")
                if lr:
                    rec.update(revenue=float(lr), revenueCurrency="euro", revenueYear=L.get("revenueYear"),
                               revenueSource=(cite[0] if (L.get("revenue") and cite) else "wikidata (exact)"))
            rec["website"] = rec.get("website") or L.get("website") or res.get("website")
            rec["city"] = rec.get("city") or L.get("city")
            rec["listSources"] = sorted(set((rec.get("listSources") or []) + cite))
            if L.get("section") and not rec.get("listSection"):
                rec["listSection"] = L["section"]
        print(f"Lists (journals / associations): {new_n:,} new companies, {merged_n:,} matched to companies we already had")
    rows = list(best.values())
    write_json(OUT, rows)
    print(f"Wikidata: {len(rows):,} companies ({sum(1 for x in rows if str(x['kvk']).startswith('wd-')):,} still without a KvK number), "
          f"{sum(1 for x in rows if x['revenue']):,} with revenue, "
          f"{sum(1 for x in rows if x['naceCodes']):,} with an industry (NACE) code -> {OUT}")


if __name__ == "__main__":
    main()
