"""
Journal / ranking / association / register lists  ->  landing_zone/companies/listed_companies.json

Put any list of Dutch companies into  companies/lists/  (CSV or Excel exported from a journal or an association's member page, or a plain .txt
with one company name per line copied from a ranking) and run this. Every company keeps WHERE it was listed -- the source (file name or a
`source` column, e.g. "FD Top 500 2025"), the rank and the year -- so the dashboard can show "listed in: FD Top 500 2025 (#143)".

Understood columns (any order, Dutch or English, extra columns ignored):
    company | bedrijf | naam | name            (required)
    revenue | omzet                           ("1.234", "1,2 mld", "850 mln", "EUR 1.2 bn"; a unit in the header like "omzet (mln)" works too)
    staff | medewerkers | werknemers | fte | employees
    city | plaats | vestigingsplaats          website | url | internetadres
    industry | sector | branche | categorie   (an SBI letter A-U, or text such as "bouw", "energie", "logistiek")
    rank | positie | #      year | jaar      source | bron      source_url | bron_url

    python import_lists.py                              # every file in companies/lists/
    python import_lists.py my_ranking.csv --industry F   # one file; default industry section when it has no sector column
    python import_lists.py --resolve                    # also look each name up on Wikidata for website / KvK number / staff / revenue
"""
import argparse
import csv
import re
import sys
import time
from pathlib import Path

import requests

from common import LANDING, read_json, section_from_industry_names, write_json
from name_match import name_key

HERE = Path(__file__).resolve().parent
LISTS_DIR = HERE / "lists"
OUT = LANDING / "listed_companies.json"
UA = {"User-Agent": "cbs-knowledge-graph-pipeline/1.0 (company discovery; contact via GitHub)"}

ALIASES = {
    "company": ["company", "bedrijf", "bedrijfsnaam", "naam", "name", "onderneming", "organisatie"],
    "revenue": ["revenue", "omzet", "revenue_eur", "omzet_eur", "turnover", "netto-omzet"],
    "staff": ["staff", "medewerkers", "werknemers", "fte", "employees", "werkzame personen", "aantal medewerkers"],
    "city": ["city", "plaats", "vestigingsplaats", "woonplaats", "vestiging", "hoofdkantoor"],
    "website": ["website", "url", "internetadres", "site", "web"],
    "industry": ["industry", "sector", "branche", "branch", "categorie", "category", "sbi"],
    "rank": ["rank", "positie", "#", "nr", "ranking", "plek"],
    "year": ["year", "jaar"],
    "source": ["source", "bron"],
    "source_url": ["source_url", "bron_url", "bronurl", "link", "bronlink"],
}


def _col(headers):
    """header -> field name (first alias wins)."""
    m = {}
    for h in headers:
        hl = re.sub(r"\s*\(.*?\)\s*", "", (h or "").strip().lower())
        for field, al in ALIASES.items():
            if hl in al and field not in m.values():
                m[h] = field
                break
    return m


def parse_money(v, header=""):
    """'1.234' / '1,2 mld' / '850 mln' / 'EUR 1.2 bn' / '12.500.000' -> euros (float) or None. A unit in the header applies to bare numbers."""
    if v is None or str(v).strip() in ("", "-", "n.b.", "nb"):
        return None
    t = str(v).lower().replace("€", " ").replace("eur", " ").strip()
    unit = 1.0
    if re.search(r"mld|miljard|bn|billion", t) or re.search(r"mld|miljard|bn|billion", header.lower()):
        unit = 1e9
    elif re.search(r"mln|miljoen|million|\bm\b", t) or re.search(r"mln|miljoen|million|\(m\)|\bm\b", header.lower()):
        unit = 1e6
    m = re.search(r"(\d[\d.,]*)", t)
    if not m:
        return None
    s = m.group(1).rstrip(".,")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3):
        s = s.replace(".", "")
    try:
        return float(s) * unit
    except ValueError:
        return None


def parse_int(v):
    n = parse_money(v)
    return int(n) if n is not None and n >= 0 else None


def section_of(industry_text, default=None):
    t = (industry_text or "").strip()
    if re.fullmatch(r"[A-Ua-u]", t):
        return t.upper()
    m = re.match(r"^([A-U])\s", t)            # 'F Bouwnijverheid'
    if m:
        return m.group(1)
    return section_from_industry_names([t], extra_keywords=True) or default


def read_rows(path):
    p = str(path)
    if p.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            sys.exit(f"{path}: Excel file -- run  pip install openpyxl  (or save it as CSV).")
        ws = openpyxl.load_workbook(p, read_only=True, data_only=True).active
        rows = [[("" if c is None else c) for c in r] for r in ws.iter_rows(values_only=True)]
        if not rows:
            return [], []
        return [str(h) for h in rows[0]], [dict(zip([str(h) for h in rows[0]], r)) for r in rows[1:]]
    with open(p, encoding="utf-8-sig", newline="") as f:
        text = f.read()
    if p.lower().endswith(".txt") or not re.search(r"[;,\t]", text.splitlines()[0] if text.strip() else ""):
        rows = []
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rank = None
            m = re.match(r"^(\d+)[.)]?\s+(.*)$", line)             # '12. Company BV  1.234 mln'
            if m:
                rank, line = m.group(1), m.group(2)
            parts = re.split(r"\s{2,}|\t|\s[-–—]\s", line, maxsplit=1)
            rows.append({"company": parts[0].strip(" ;,"), "revenue": parts[1].strip() if len(parts) > 1 else "", "rank": rank or ""})
        return ["company", "revenue", "rank"], rows
    sample = text[:4096]
    dialect = csv.Sniffer().sniff(sample, delimiters=";,\t") if sample else csv.excel
    reader = csv.DictReader(text.splitlines(), dialect=dialect)
    return list(reader.fieldnames or []), list(reader)


def import_file(path, default_industry=None, default_source=None):
    headers, rows = read_rows(path)
    cmap = _col(headers)
    rev_header = next((h for h, f in cmap.items() if f == "revenue"), "")
    src_default = default_source or Path(path).stem.replace("_", " ").replace("-", " ").strip()
    out, skipped = [], 0
    for r in rows:
        g = lambda f: next((r.get(h) for h, ff in cmap.items() if ff == f), None)
        name = str(g("company") or "").strip()
        if not name or name.startswith("#") or name_key(name) is None:
            skipped += 1
            continue
        out.append({
            "name": name, "key": name_key(name),
            "section": section_of(str(g("industry") or ""), default_industry),
            "industryText": (str(g("industry") or "").strip() or None),
            "staff": parse_int(g("staff")), "revenue": parse_money(g("revenue"), rev_header),
            "revenueYear": (str(g("year") or "").strip() or None),
            "city": (str(g("city") or "").strip() or None), "website": (str(g("website") or "").strip() or None),
            "rank": (str(g("rank") or "").strip() or None),
            "source": (str(g("source") or "").strip() or src_default),
            "sourceUrl": (str(g("source_url") or "").strip() or None),
        })
    print(f"  {Path(path).name}: {len(out):,} companies" + (f" ({skipped} rows skipped: no usable name)" if skipped else ""))
    return out


# --------------------------------------------------------------- name -> Wikidata (free, authentic)
WD = "https://www.wikidata.org/w/api.php"


def _wd(params):
    r = requests.get(WD, params={**params, "format": "json"}, headers=UA, timeout=30)
    r.raise_for_status()
    return r.json()


def _claim(ent, pid, kind="value"):
    out = []
    for c in (ent.get("claims") or {}).get(pid, []):
        if c.get("rank") == "deprecated":
            continue
        dv = (c.get("mainsnak") or {}).get("datavalue") or {}
        out.append(dv.get("value"))
    return [x for x in out if x is not None]


def resolve_name(name):
    """A company name -> {qid, website, kvk, staff, revenue, revenueYear, city?} from Wikidata, or None.
    Accepts a hit only if it is a Dutch item (country = Netherlands) whose label / alias equals the name -- no fuzzy guessing."""
    key = name_key(name)
    hits = _wd({"action": "wbsearchentities", "search": name, "language": "nl", "limit": 5, "type": "item"}).get("search", [])
    if not hits:
        return None
    ents = _wd({"action": "wbgetentities", "ids": "|".join(h["id"] for h in hits), "props": "claims|labels|aliases",
                "languages": "nl|en"}).get("entities", {})
    for h in hits:
        e = ents.get(h["id"]) or {}
        names = [v["value"] for v in (e.get("labels") or {}).values()] + [a["value"] for v in (e.get("aliases") or {}).values() for a in v]
        if key not in {name_key(n) for n in names if n}:
            continue
        if not any((isinstance(v, dict) and v.get("id") == "Q55") for v in _claim(e, "P17")):
            continue                                     # not a Dutch item
        res = {"qid": h["id"]}
        site = _claim(e, "P856")
        res["website"] = site[0] if site else None
        kvk = _claim(e, "P3220")
        res["kvk"] = str(kvk[0]) if kvk else None
        emp = [v.get("amount") for v in _claim(e, "P1128") if isinstance(v, dict)]
        res["staff"] = int(float(max(emp, key=lambda x: float(x)))) if emp else None
        rev = [v for v in _claim(e, "P2139") if isinstance(v, dict) and str(v.get("unit", "")).endswith("Q4916")]
        res["revenue"] = float(rev[-1]["amount"]) if rev else None
        return res
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="*", help="list files (default: everything in companies/lists/)")
    ap.add_argument("--industry", help="default SBI section letter for files without a sector column (e.g. F for construction)")
    ap.add_argument("--resolve", action="store_true", help="look up names on Wikidata (website, KvK number, staff, revenue)")
    ap.add_argument("--max-resolve", type=int, default=600)
    a = ap.parse_args()
    files = [Path(f) for f in a.files] or sorted(p for p in LISTS_DIR.glob("*") if p.suffix.lower() in (".csv", ".xlsx", ".xlsm", ".txt") and not p.name.startswith(("_", ".")))
    if not files:
        print("No list files (put CSV / Excel / TXT files into companies/lists/) -- nothing to import.")
        return
    print(f"Importing {len(files)} list file(s) ...")
    allrows = []
    for f in files:
        allrows += import_file(f, a.industry)
    # one record per company; keep every source it appears in
    merged = {}
    for r in allrows:
        m = merged.setdefault(r["key"], {**r, "sources": []})
        m["sources"].append({"source": r["source"], "rank": r["rank"], "year": r["revenueYear"], "url": r["sourceUrl"]})
        for k in ("section", "staff", "revenue", "revenueYear", "city", "website"):
            if m.get(k) in (None, "") and r.get(k) not in (None, ""):
                m[k] = r[k]
    rows = list(merged.values())
    if a.resolve:
        done = {r["key"]: r.get("resolved") for r in read_json(OUT, [])}
        n = hit = 0
        for r in rows:
            if r["key"] in done and done[r["key"]] is not None:
                r["resolved"] = done[r["key"]]
            elif n < a.max_resolve and (not r.get("website") or (r.get("staff") is None and r.get("revenue") is None)):
                n += 1
                try:
                    r["resolved"] = resolve_name(r["name"])
                except Exception as e:
                    r["resolved"] = None
                    print(f"  ! Wikidata lookup for '{r['name']}' failed ({str(e)[:80]})")
                time.sleep(0.3)
            hit += bool(r.get("resolved"))
        print(f"Wikidata name lookup: {hit:,} of {len(rows):,} companies matched to a Dutch Wikidata item (exact name only)")
    write_json(OUT, rows)
    sized = sum(1 for r in rows if r.get("staff") or r.get("revenue") or (r.get("resolved") or {}).get("staff") or (r.get("resolved") or {}).get("revenue"))
    print(f"{len(rows):,} unique companies from {len(files)} list(s) -> {OUT}  ({sized:,} with staff or revenue; the website scan "
          f"and Wikidata can size more)")


if __name__ == "__main__":
    main()
