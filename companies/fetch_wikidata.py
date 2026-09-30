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

from common import LANDING, lenient_json, norm_kvk, write_json

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
  ?item wdt:%(p)s ?kvk .
  OPTIONAL { ?item wdt:P452 ?industry . %(nace)s }
  OPTIONAL { ?item wdt:P159 ?city }
  OPTIONAL { ?item wdt:P856 ?website }   # official website -> used by erp_enrich.py --detect
  OPTIONAL { ?item p:P2139 ?rs . ?rs psv:P2139 ?rv . ?rv wikibase:quantityAmount ?revenue ; wikibase:quantityUnit ?unit .
             OPTIONAL { ?rs pq:P585 ?revDate } }
  OPTIONAL { ?item wdt:P1128 ?employees }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }
}"""


def run_sparql(query, retries=3):
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
  ?item wdt:%(p)s ?kvk .
  OPTIONAL { ?item wdt:P31 ?type }
  OPTIONAL { ?item schema:description ?desc . FILTER(LANG(?desc) IN ("en", "nl")) }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvk-property", default="P3220", help='Wikidata "KvK company ID"')
    ap.add_argument("--nace-property", default="P4496", help='Wikidata "NACE code rev.2"')
    a = ap.parse_args()
    prop, nace_p = a.kvk_property, a.nace_property
    nace = f"OPTIONAL {{ ?industry wdt:{nace_p} ?nace }}" if nace_p else ""
    data = run_sparql(QUERY % {"p": prop, "nace": nace})
    best = {}
    for b in data["results"]["bindings"]:
        kvk = norm_kvk(b["kvk"]["value"])
        if not kvk:
            continue
        rec = best.setdefault(kvk, {"kvk": kvk, "wikidata": b["item"]["value"].rsplit("/", 1)[-1],
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
        for b in run_sparql(QUERY_TYPES % {"p": prop})["results"]["bindings"]:
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
    for rec in best.values():   # SPARQL row order varies between runs -> sort, so the industry picked is stable
        rec["naceCodes"].sort(); rec["industries"].sort(); rec["types"].sort(); rec["descriptions"].sort()
    rows = list(best.values())
    write_json(OUT, rows)
    print(f"Wikidata: {len(rows):,} companies with a KVK number, "
          f"{sum(1 for x in rows if x['revenue']):,} with revenue, "
          f"{sum(1 for x in rows if x['naceCodes']):,} with an industry (NACE) code -> {OUT}")


if __name__ == "__main__":
    main()
