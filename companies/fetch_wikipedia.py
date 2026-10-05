"""
Wikipedia industry categories  ->  landing_zone/companies/wikipedia_candidates.json        FREE, no key, CC BY-SA

Why: Wikidata only lists a Dutch company with size data if somebody filled it in. Wikipedia's category tree
("Nederlands bedrijf" -> "Nederlands energiebedrijf", "Nederlandse bank", "Nederlands bouwbedrijf" ...) names the notable Dutch companies
per industry. We walk that tree (nl.wikipedia.org API), keep the company pages whose category points at one of OUR focus industries
(focus_industries.json), and look each page up on Wikidata (fetch_wikidata.py) for staff, revenue, website, KvK number.

Every candidate keeps its citation: the category path it was found under ("Wikipedia: Nederlands bedrijf > Nederlands energiebedrijf").

    python fetch_wikipedia.py                    # focus industries only
    python fetch_wikipedia.py --all-industries   # keep every industry
"""
import argparse
import sys
import time

import requests

from common import LANDING, load_focus, section_from_industry_names, write_json

API = "https://nl.wikipedia.org/w/api.php"
UA = {"User-Agent": "cbs-knowledge-graph-pipeline/1.0 (company discovery; contact via GitHub)"}
OUT = LANDING / "wikipedia_candidates.json"


def api(params, session=None, retries=3):
    s = session or requests
    for attempt in range(retries):
        try:
            r = s.get(API, params={**params, "format": "json", "formatversion": "2"}, headers=UA, timeout=60)
            r.raise_for_status()
            return r.json()
        except (requests.RequestException, ValueError) as e:
            if attempt == retries - 1:
                raise
            print(f"  (Wikipedia attempt {attempt + 1} failed: {e}; retrying)", flush=True)
            time.sleep(5 * (attempt + 1))


def members(category, session=None):
    """(subcategory titles, page titles) of one category; follows API continuation."""
    subs, pages, cont = [], [], {}
    while True:
        d = api({"action": "query", "list": "categorymembers", "cmtitle": "Categorie:" + category,
                 "cmtype": "subcat|page", "cmlimit": "500", **cont}, session)
        for m in d.get("query", {}).get("categorymembers", []):
            title = m["title"]
            if m.get("ns") == 14:
                subs.append(title.split(":", 1)[1])
            elif m.get("ns") == 0 and not title.lower().startswith(("lijst van", "lijst met")):
                pages.append(title)
        cont = d.get("continue") or {}
        if not cont:
            return subs, pages


def qids_for(titles, session=None):
    """page titles -> {title: Wikidata QID} (50 titles per API call)."""
    out = {}
    for i in range(0, len(titles), 50):
        d = api({"action": "query", "prop": "pageprops", "ppprop": "wikibase_item", "titles": "|".join(titles[i:i + 50])}, session)
        for p in d.get("query", {}).get("pages", []):
            q = (p.get("pageprops") or {}).get("wikibase_item")
            if q:
                out[p["title"]] = q
        time.sleep(0.1)
    return out


def walk(roots, depth, max_pages, session=None):
    """BFS over the category tree -> {page title: [category path ...]} (each page keeps the first path it was found under)."""
    seen_cats, found, queue = set(), {}, [(r, 0, [r]) for r in roots]
    while queue and len(found) < max_pages:
        cat, d, path = queue.pop(0)
        if cat in seen_cats:
            continue
        seen_cats.add(cat)
        try:
            subs, pages = members(cat, session)
        except Exception as e:
            print(f"  ! category '{cat}' skipped ({e})", flush=True)
            continue
        for t in pages:
            found.setdefault(t, path)
        if d < depth:
            queue += [(s, d + 1, path + [s]) for s in subs if s not in seen_cats]
        time.sleep(0.1)
        if len(seen_cats) % 25 == 0:
            print(f"  {len(seen_cats):,} categories read, {len(found):,} pages so far", flush=True)
    return found


def section_hint(path):
    """Leaf-most category that maps to an industry wins ('Nederlands energiebedrijf' -> D)."""
    for name in reversed(path):
        s = section_from_industry_names([name], extra_keywords=True)
        if s:
            return s
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all-industries", action="store_true", help="keep companies of every industry, not only the focus industries")
    ap.add_argument("--depth", type=int)
    ap.add_argument("--max-pages", type=int)
    a = ap.parse_args()
    F = load_focus()
    depth, cap = a.depth or F["wiki_depth"], a.max_pages or F["wiki_max_pages"]
    print(f"Wikipedia: walking {F['wiki_categories']} (depth {depth}, up to {cap:,} pages) ...", flush=True)
    found = walk(F["wiki_categories"], depth, cap)
    keep = {}
    for title, path in found.items():
        sec = section_hint(path)
        if a.all_industries or sec is None or sec in F["sections"]:      # unknown industry stays: Wikidata / website may tell
            keep[title] = (path, sec)
    print(f"Wikipedia: {len(found):,} company pages found, {len(keep):,} in our focus industries (or industry unknown)")
    qids = qids_for(list(keep))
    rows = [{"qid": q, "title": t, "categories": keep[t][0], "section": keep[t][1],
             "source": "Wikipedia (nl): Categorie:" + " > ".join(keep[t][0][-2:])} for t, q in qids.items()]
    write_json(OUT, rows)
    by = {}
    for r in rows:
        by[r["section"] or "?"] = by.get(r["section"] or "?", 0) + 1
    print(f"Wikipedia candidates with a Wikidata item: {len(rows):,} -> {OUT}\n  per industry section: "
          + ", ".join(f"{k}:{v}" for k, v in sorted(by.items(), key=lambda x: -x[1])))


if __name__ == "__main__":
    try:
        main()
    except requests.RequestException as e:
        print(f"Wikipedia discovery skipped: {e}")
        sys.exit(0)        # optional enrichment: never fail the pipeline
