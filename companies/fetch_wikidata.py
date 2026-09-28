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
import sys

import requests

from common import LANDING, norm_kvk, write_json

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
SELECT ?item ?itemLabel ?kvk ?revenue ?unitLabel ?revDate ?employees ?industryLabel ?nace ?cityLabel WHERE {
  ?item wdt:%(p)s ?kvk .
  OPTIONAL { ?item wdt:P452 ?industry . %(nace)s }
  OPTIONAL { ?item wdt:P159 ?city }
  OPTIONAL { ?item p:P2139 ?rs . ?rs psv:P2139 ?rv . ?rv wikibase:quantityAmount ?revenue ; wikibase:quantityUnit ?unit .
             OPTIONAL { ?rs pq:P585 ?revDate } }
  OPTIONAL { ?item wdt:P1128 ?employees }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }
}"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--kvk-property", default="P3220", help='Wikidata "KvK company ID"')
    ap.add_argument("--nace-property", default="P4496", help='Wikidata "NACE code rev.2"')
    a = ap.parse_args()
    prop, nace_p = a.kvk_property, a.nace_property
    nace = f"OPTIONAL {{ ?industry wdt:{nace_p} ?nace }}" if nace_p else ""
    r = requests.get("https://query.wikidata.org/sparql", headers={**UA, "Accept": "application/sparql-results+json"},
                     params={"query": QUERY % {"p": prop, "nace": nace}}, timeout=300)
    r.raise_for_status()
    best = {}
    for b in r.json()["results"]["bindings"]:
        kvk = norm_kvk(b["kvk"]["value"])
        if not kvk:
            continue
        rec = best.setdefault(kvk, {"kvk": kvk, "wikidata": b["item"]["value"].rsplit("/", 1)[-1],
                                    "name": b.get("itemLabel", {}).get("value"),
                                    "revenue": None, "revenueCurrency": None, "revenueYear": None, "employees": None,
                                    "industries": [], "naceCodes": [], "city": None})
        ind = b.get("industryLabel", {}).get("value")
        if ind and ind not in rec["industries"]:
            rec["industries"].append(ind)
        nc = b.get("nace", {}).get("value")
        if nc and nc not in rec["naceCodes"]:
            rec["naceCodes"].append(nc)
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
    rows = list(best.values())
    write_json(OUT, rows)
    print(f"Wikidata: {len(rows):,} companies with a KVK number, "
          f"{sum(1 for x in rows if x['revenue']):,} with revenue, "
          f"{sum(1 for x in rows if x['naceCodes']):,} with an industry (NACE) code -> {OUT}")


if __name__ == "__main__":
    main()
