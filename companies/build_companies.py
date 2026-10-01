"""
Merge KVK + GLEIF + Wikidata on the KVK number -> landing_zone/companies/companies.json

Each company gets its CBS links worked out here:
    section / branchLabel   from the KVK main SBI code  (-> the CBS :Branch node, e.g. 'I Horeca')
    sizeBand / ictSizeBand  from KVK staff count        (-> the CBS size groups KPI 2/3 use)
    city                    from the KVK visit address  (-> the CBS :Gemeente node, matched by name)
and, where available:
    lei, legal name, parent companies                   from GLEIF (joined on KVK number)
    revenue (+ year, currency), employees               from Wikidata (joined on KVK number) -> revenueSource 'wikidata'

Sales focus (defaults): only companies with 100+ working persons, and --
where revenue is published -- above EUR 10m.

    python build_companies.py            # real data
    python build_companies.py --test     # the KVK test-environment companies
    python build_companies.py --min-staff 0
    python build_companies.py --min-revenue 0    # keep companies below EUR 10m revenue too
    python build_companies.py --free     # no KVK needed: Wikidata companies (industry via their NACE code,
                                         # staff via Wikidata employees) + GLEIF. Larger companies only.
"""
import argparse
import re
from pathlib import Path
from collections import Counter

from common import (LANDING, norm_kvk, SBI2008_SECTION_LABEL, is_euro, read_json, read_jsonl, sbi_section,
                    section_from_industry_names, size_bands, write_json)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--min-staff", type=int, default=100,
                    help="sales focus: only companies with at least this many working persons (default 100 -- "
                         "lowers it for smaller firms)")
    ap.add_argument("--free", action="store_true", help="build from Wikidata + GLEIF only (no KVK profiles)")
    ap.add_argument("--min-revenue", type=float, default=10e6,
                    help="sales focus: drop companies whose PUBLISHED revenue is below this (default EUR 10m). "
                         "Companies without published revenue are kept -- the dashboard estimates theirs "
                         "(staff x industry revenue per worker) and applies the same threshold. 0 = keep all.")
    a = ap.parse_args()

    gleif = {r["kvk"]: r for r in read_jsonl(LANDING / "gleif_nl.jsonl") if r.get("kvk")}
    parents = read_json(LANDING / "gleif_parents.json", {})
    wiki = {r["kvk"]: r for r in read_json(LANDING / "wikidata_revenue.json", [])}

    # Manual industry overrides (companies/industry_overrides.csv: wikidata | kvk | name | section A-U | note).
    # They win over every automatic guess -- add a row whenever a company lands in "(no industry)" or the wrong one.
    overrides = {}
    ov_path = Path(__file__).resolve().parent / "industry_overrides.csv"
    if ov_path.exists():
        import csv
        with open(ov_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                sec = (r.get("section") or "").strip().upper()
                if re.fullmatch(r"[A-U]", sec):
                    for key in ((r.get("wikidata") or "").strip(), norm_kvk(r.get("kvk")) if r.get("kvk") else ""):
                        if key:
                            overrides[key] = sec
        print(f"industry overrides: {len(overrides):,} keys from {ov_path.name}")

    if a.free:
        # Stand-in 'profiles' from Wikidata: NACE code -> SBI division (SBI is the Dutch
        # version of NACE, same division numbers), Wikidata employees -> staff.
        if not wiki:
            raise SystemExit("No Wikidata data yet -- run: python fetch_wikidata.py")
        profiles = []
        for w in wiki.values():
            code = next((c for c in w.get("naceCodes", []) if sbi_section(c)), None)
            letter_only = next((c for c in w.get("naceCodes", []) if re.fullmatch(r"[A-U]", c.strip())), None)
            by_name = via = None
            if not (code or letter_only):
                for via_, names, extra in (("wikidata industry name", w.get("industries"), False),
                                           ("wikidata kind (instance of)", w.get("types"), True),
                                           ("wikidata description", w.get("descriptions"), True)):
                    by_name = section_from_industry_names(names, extra_keywords=extra)
                    if by_name:
                        via = via_
                        break
            profiles.append({
                "kvk": w["kvk"], "wikidata": w.get("wikidata"), "name": w.get("name"), "tradeNames": [],
                "industryByName": bool(by_name), "industryVia": via,
                "mainSbi": code or letter_only or by_name, "mainSbiDesc": ", ".join(w.get("industries", [])[:2]) or (w.get("descriptions") or [None])[0],
                "sbi": [{"code": c} for c in w.get("naceCodes", [])],
                "staff": w.get("employees"), "address": {"city": w.get("city")}, "websites": [w["website"]] if w.get("website") else [],
                "freeSource": True,
            })
    else:
        profiles = read_jsonl(LANDING / ("kvk_profiles.test.jsonl" if a.test else "kvk_profiles.jsonl"))

    out, skipped = [], Counter()
    for p in profiles:
        if p.get("notFound") or not p.get("kvk"):
            skipped["not found at KVK"] += 1
            continue
        staff = p.get("staff")
        # Free mode: Wikidata often lacks a staff count -- keep such a company only when
        # its published revenue already proves it's large (>= --min-revenue).
        _w = wiki.get(p["kvk"]) or {}
        w_rev = _w.get("revenue") if is_euro(_w.get("revenueCurrency")) else None
        unknown_ok = a.free and staff is None and w_rev is not None and w_rev >= (a.min_revenue or 0)
        if a.min_staff and not unknown_ok and (staff is None or staff < a.min_staff):
            skipped[f"fewer than {a.min_staff} staff (or unknown)"] += 1
            continue
        ms = (p.get("mainSbi") or "").strip()
        section = ms if re.fullmatch(r"[A-U]", ms) else sbi_section(ms)
        ov = overrides.get(p.get("wikidata") or "") or overrides.get(p["kvk"])
        if ov:
            section = ov
        band, ict_band = size_bands(staff)
        g = gleif.get(p["kvk"], {})
        w = wiki.get(p["kvk"], {})
        par = parents.get(g.get("lei"), {}) if g.get("lei") else {}
        rev = w.get("revenue") if is_euro(w.get("revenueCurrency")) else None
        if a.min_revenue and rev is not None and rev < a.min_revenue:
            skipped[f"published revenue below EUR {a.min_revenue / 1e6:,.0f}m"] += 1
            continue
        name = p.get("name") or g.get("name")
        if not name or re.fullmatch(r"Q\d+", name.strip()):    # Wikidata item without a label shows up as 'Q81307'
            name = g.get("name")
        if not name or re.fullmatch(r"Q\d+", name.strip()):
            skipped["no readable company name (unlabelled Wikidata item)"] += 1
            continue
        out.append({
            "kvk": p["kvk"],
            "name": name,
            "legalName": g.get("name"),
            "tradeNames": p.get("tradeNames", []),
            "mainSbi": p.get("mainSbi"),
            "mainSbiDesc": p.get("mainSbiDesc"),
            "sbiCodes": [s["code"] for s in p.get("sbi", []) if s.get("code")],
            "section": section,
            "branchLabel": SBI2008_SECTION_LABEL.get(section),
            "staff": staff,
            "sizeBand": band,
            "ictSizeBand": ict_band,
            "legalForm": p.get("legalForm"),
            "city": (p.get("address") or {}).get("city") or ((g.get("legalAddress") or {}).get("city")),
            "postcode": (p.get("address") or {}).get("postcode"),
            "street": " ".join(x for x in [(p.get("address") or {}).get("street"),
                                            str((p.get("address") or {}).get("number") or "")] if x).strip() or None,
            "website": (p.get("websites") or [None])[0],
            "lei": g.get("lei"),
            "leiStatus": g.get("leiStatus"),
            "parentLei": (par.get("direct") or {}).get("lei"),
            "parentName": (par.get("direct") or {}).get("name"),
            "ultimateParentLei": (par.get("ultimate") or {}).get("lei"),
            "ultimateParentName": (par.get("ultimate") or {}).get("name"),
            "revenue": rev,
            "revenueYear": w.get("revenueYear") if rev else None,
            "revenueSource": "wikidata (exact)" if rev else None,
            "wikidata": w.get("wikidata"),
            "noMarketing": p.get("noMarketing"),
            "sources": [s for s, ok in (("kvk", not p.get("freeSource")), ("gleif", bool(g)), ("wikidata", bool(w))) if ok],
            "industrySource": ("manual override" if ov else (p.get("industryVia") or "wikidata industry name" if p.get("industryByName") else "wikidata NACE")
                               if p.get("freeSource") else "kvk SBI") if section else None,
        })

    if a.free:
        unmapped = Counter()
        for c in out:
            if not c["branchLabel"]:
                w_ = wiki.get(c["kvk"], {})
                for n in ((w_.get("industries") or [])[:2] + (w_.get("types") or [])[:2] + (w_.get("descriptions") or [])[:1]) or ["(nothing on Wikidata)"]:
                    unmapped[n] += 1
        if unmapped:
            print("  industry names / kinds / descriptions on Wikidata that could NOT be mapped to a CBS industry (add keywords to INDUSTRY_KEYWORDS in common.py):")
            for n, k in unmapped.most_common(40):
                print(f"    {k:4}  {n}")
        no_ind = sum(1 for c in out if not c["branchLabel"])
        by_name = sum(1 for c in out if c.get("industrySource") and c["industrySource"].startswith("wikidata") and c["industrySource"] != "wikidata NACE")
        print(f"  free mode: industry from NACE code: {sum(1 for c in out if c.get('industrySource') == 'wikidata NACE'):,}, "
              f"from industry name: {by_name:,}, none: {no_ind:,} (of {len(out):,})")
    # Companies the sales team knows but the free sources do not list (e.g. a mid-size manufacturer with no Wikidata
    # record). companies/extra_companies.csv: company | kvk | section A-U | staff | revenue_eur | revenue_year | city | website | note
    # They bypass the staff / revenue filters (a person chose them). With no KVK number they get the id 'manual-<name>'.
    extra_path = Path(__file__).resolve().parent / "extra_companies.csv"
    if extra_path.exists() and not a.test:
        import csv
        from erp_catalog import slug
        from name_match import name_key
        have_kvk = {c["kvk"] for c in out}
        have_name = {name_key(c["name"]) for c in out if c.get("name")}
        added, dup = [], []
        with open(extra_path, newline="", encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                r = {(k or "").strip().lower(): (v or "").strip() for k, v in r.items()}
                nm = r.get("company")
                if not nm or nm.startswith("#"):
                    continue
                kvk = norm_kvk(r["kvk"]) if r.get("kvk") else "manual-" + slug(nm)
                if kvk in have_kvk or (name_key(nm) and name_key(nm) in have_name):
                    dup.append(nm)
                    continue
                sec = (r.get("section") or "").upper()
                sec = sec if re.fullmatch(r"[A-U]", sec) else None
                num = lambda v: float(v.replace(".", "").replace(",", ".")) if v else None
                staff = int(num(r.get("staff"))) if r.get("staff") else None
                rev = num(r.get("revenue_eur"))
                band, ict_band = size_bands(staff)
                out.append({"kvk": kvk, "name": nm, "legalName": None, "tradeNames": [], "mainSbi": sec, "mainSbiDesc": r.get("note") or None,
                            "sbiCodes": [], "section": sec, "branchLabel": SBI2008_SECTION_LABEL.get(sec), "staff": staff,
                            "sizeBand": band, "ictSizeBand": ict_band, "legalForm": None, "city": r.get("city") or None,
                            "postcode": None, "street": None, "website": r.get("website") or None, "lei": None, "leiStatus": None,
                            "parentLei": None, "parentName": None, "ultimateParentLei": None, "ultimateParentName": None,
                            "revenue": rev, "revenueYear": r.get("revenue_year") or None,
                            "revenueSource": "added by sales (csv)" if rev else None, "wikidata": None, "noMarketing": None,
                            "sources": ["manual"], "industrySource": "manual (sales)" if sec else None})
                have_kvk.add(kvk); added.append(nm)
        print(f"extra companies: {len(added):,} added from {extra_path.name}"
              + (f"; {len(dup):,} already in the list: {dup[:10]}" if dup else ""))
        for nm in added:
            c = next(x for x in out if x["name"] == nm)
            if c["staff"] is None and c["revenue"] is None:
                print(f"  ! {nm}: no staff and no revenue -- KPI 10 cannot size it; add one of them")
    write_json(LANDING / ("companies.test.json" if a.test else "companies.json"), out)
    print(f"{len(out):,} companies built  (skipped: {dict(skipped) or 'none'})")
    print(f"  with LEI: {sum(1 for c in out if c['lei']):,}   with parent: {sum(1 for c in out if c['parentLei']):,}"
          f"   with exact revenue: {sum(1 for c in out if c['revenue']):,}")
    print("  per CBS industry section:")
    for sec, n in sorted(Counter(c["branchLabel"] or "(no SBI)" for c in out).items(), key=lambda x: -x[1]):
        print(f"    {n:6,}  {sec}")


if __name__ == "__main__":
    main()
