"""
Which ERP does each company use?  ->  landing_zone/companies/erp.json

NONE of KVK / GLEIF / Wikidata / CBS says which ERP a company runs, so this step
collects EVIDENCE from two places and records how strong each piece of evidence is:

  1. --csv FILE      Facts you already have or research: vendor customer references,
                     annual-report mentions, sales-team knowledge, a purchased
                     technographics export (BuiltWith, HG Insights, ...).
                     Columns:  kvk, erp, [confidence], [source], [evidence_url], [note]
                     confidence defaults to 'high'.  See erp_evidence.template.csv.
  2. --detect        Free, best-effort scan of each company's own website (home page plus
                     careers / about / privacy pages -- robots.txt respected):
                       medium  = a link, script or sub-domain that belongs to an ERP product
                                 (e.g. *.exactonline.nl, afasinsite, netsuite.com)
                       low     = the product is merely NAMED in the text (a job ad asking for
                                 "SAP experience", "we run Dynamics 365", ...)
                     Most companies say nothing about their ERP on their website, so expect a
                     low hit rate -- that is why CSV evidence exists.

Every result keeps its source + evidence, so the dashboard can show WHY it thinks a company
uses an ERP, and never presents a low-confidence guess as fact.

    python erp_enrich.py --csv my_erp_facts.csv
    python erp_enrich.py --detect --max 300
    python erp_enrich.py --csv my_erp_facts.csv --detect
"""
import argparse
import csv
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

from common import LANDING, norm_kvk, read_json, write_json

OUT = LANDING / "erp.json"
UA = "cbs-knowledge-graph-pipeline/1.0 (ERP research; contact via GitHub)"
RANK = {"high": 3, "medium": 2, "low": 1}

# name -> (vendor, [aliases usable in a CSV], [strong: host/link/script fragments], [weak: phrases in text])
CATALOG = {
    "SAP S/4HANA": ("SAP", ["sap", "sap s4hana", "sap s/4hana", "s/4hana", "sap erp", "sap ecc", "sap r/3"],
                    ["s4hana.ondemand.com", "sapbydesign", "hana.ondemand.com"],
                    [r"s/?4 ?hana", r"sap erp", r"sap ecc", r"sap ?(?:s4|r/3)"]),
    "SAP Business One": ("SAP", ["sap business one", "sap b1", "business one"], [],
                         [r"sap business one", r"sap b1\b"]),
    "Microsoft Dynamics 365": ("Microsoft", ["dynamics", "dynamics 365", "d365", "business central", "dynamics nav",
                                              "navision", "dynamics ax", "microsoft dynamics"],
                               ["dynamics.com", "businesscentral.dynamics.com"],
                               [r"dynamics 365", r"business central", r"dynamics (?:nav|ax|365)", r"navision"]),
    "Oracle NetSuite": ("Oracle", ["netsuite", "oracle netsuite"], ["netsuite.com"], [r"netsuite"]),
    "Oracle ERP Cloud / JD Edwards": ("Oracle", ["oracle", "oracle erp", "oracle fusion", "jd edwards", "oracle ebs",
                                                   "e-business suite", "peoplesoft"], [],
                                      [r"oracle fusion", r"oracle erp", r"jd ?edwards", r"e-business suite", r"peoplesoft"]),
    "Exact": ("Exact", ["exact", "exact online", "exact globe", "exact synergy"],
              ["exactonline.nl", "exactonline.com", "exactonline.be"], [r"exact (?:online|globe|synergy)"]),
    "AFAS": ("AFAS", ["afas", "afas profit", "afas online", "afas insite"],
             ["afasinsite", "afasonlineconnector", "afas.online"], [r"afas (?:profit|software|online|insite)"]),
    "Unit4": ("Unit4", ["unit4", "agresso", "unit4 erp"], ["unit4.com"], [r"unit4", r"agresso"]),
    "Infor": ("Infor", ["infor", "infor ln", "infor m3", "infor cloudsuite", "baan"], ["infor.com"],
              [r"infor (?:ln|m3|cloudsuite|syteline|visual)", r"\bbaan\b"]),
    "IFS": ("IFS", ["ifs", "ifs cloud", "ifs applications"], ["ifs.com"], [r"ifs (?:cloud|applications|erp)"]),
    "Visma": ("Visma", ["visma", "visma net", "visma severa"], [], [r"visma net"]),
    "Sage": ("Sage", ["sage", "sage 100", "sage x3", "sage intacct"], [], [r"sage (?:x3|100|intacct|200)"]),
    "Odoo": ("Odoo", ["odoo"], ["odoo.com"], [r"\bodoo\b"]),
    "Epicor": ("Epicor", ["epicor", "epicor kinetic"], ["epicor.com"], [r"epicor"]),
    "QAD": ("QAD", ["qad", "qad adaptive erp"], [], [r"qad (?:adaptive|erp)"]),
    "Ridder": ("Ridder", ["ridder", "ridder iq"], ["ridder.nl", "ridder.com"], [r"ridder ?(?:iq|erp)"]),
    "Workday Financials": ("Workday", ["workday", "workday financials"], ["myworkday.com"], [r"workday financial"]),
    "Acumatica": ("Acumatica", ["acumatica"], [], [r"acumatica"]),
}
_ALIAS = {}
for _name, (_v, _aliases, _s, _w) in CATALOG.items():
    _ALIAS[_name.lower()] = _name
    for _a in _aliases:
        _ALIAS.setdefault(_a.lower(), _name)

PAGE_HINT = re.compile(r"vacatur|career|werken-?bij|jobs?\b|over-?ons|about|privacy|cookie|nieuws|news|cases?", re.I)


def canonical(name):
    """Free-text ERP name -> (catalog name, vendor). Unknown names are kept as typed."""
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if key in _ALIAS:
        c = _ALIAS[key]
        return c, CATALOG[c][0]
    return (name or "").strip(), (name or "").strip()


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


# ---------------------------------------------------------------- CSV evidence
def read_csv_evidence(path):
    out = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        for row in csv.DictReader(f, dialect=dialect):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            kvk, erp = norm_kvk(row.get("kvk")), row.get("erp")
            if not kvk or not erp:
                continue
            conf = (row.get("confidence") or "high").lower()
            name, vendor = canonical(erp)
            out.append({"kvk": kvk, "erp": name, "vendor": vendor,
                        "confidence": conf if conf in RANK else "high",
                        "source": row.get("source") or "csv (manual)",
                        "evidence": row.get("note") or None, "evidenceUrl": row.get("evidence_url") or None})
    return out


# ----------------------------------------------------------- website detection
def scan_html(html, base_url):
    """Pure function: (page HTML) -> list of hits. Testable without any network."""
    low = html.lower()
    hits = []
    for name, (vendor, _aliases, strong, weak) in CATALOG.items():
        s = next((f for f in strong if f in low), None)
        if s:
            hits.append({"erp": name, "vendor": vendor, "confidence": "medium",
                         "source": "website technology fingerprint", "evidence": f"references '{s}'",
                         "evidenceUrl": base_url})
            continue
        for pat in weak:
            m = re.search(pat, low)
            if m:
                a, b = max(0, m.start() - 60), min(len(low), m.end() + 60)
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html[a:b])).strip()
                hits.append({"erp": name, "vendor": vendor, "confidence": "low",
                             "source": "website text mention", "evidence": f"...{snippet}...",
                             "evidenceUrl": base_url})
                break
    return hits


def _robots_ok(session, url, cache):
    host = urlparse(url).netloc
    if host not in cache:
        rp = RobotFileParser()
        try:
            r = session.get(f"{urlparse(url).scheme}://{host}/robots.txt", timeout=8)
            rp.parse(r.text.splitlines() if r.status_code == 200 else [])
        except Exception:
            rp.parse([])
        cache[host] = rp
    return cache[host].can_fetch(UA, url)


def detect_site(session, kvk, website, max_pages=4):
    if not website:
        return []
    url = website if re.match(r"https?://", website, re.I) else "https://" + website
    cache, seen, todo, found = {}, set(), [url], {}
    while todo and len(seen) < max_pages:
        u = todo.pop(0)
        if u in seen or not _robots_ok(session, u, cache):
            continue
        seen.add(u)
        try:
            r = session.get(u, timeout=10, headers={"User-Agent": UA})
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                continue
            html = r.text[:600_000]
        except Exception:
            continue
        for h in scan_html(html, u):
            if RANK[h["confidence"]] > RANK.get(found.get(h["erp"], {}).get("confidence", ""), 0):
                found[h["erp"]] = {**h, "kvk": kvk}
        if len(seen) == 1:  # from the home page, follow a few careers / about / privacy links on the same host
            for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
                full = urljoin(r.url, href)
                if urlparse(full).netloc == urlparse(r.url).netloc and PAGE_HINT.search(urlparse(full).path):
                    todo.append(full)
        time.sleep(0.3)
    return list(found.values())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="your own ERP facts: kvk, erp, [confidence], [source], [evidence_url], [note]")
    ap.add_argument("--detect", action="store_true", help="scan company websites for ERP fingerprints")
    ap.add_argument("--max", type=int, default=500, help="max websites to scan (default 500)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--companies", default=str(LANDING / "companies.json"))
    a = ap.parse_args()
    if not a.csv and not a.detect:
        sys.exit("Nothing to do: pass --csv FILE and/or --detect.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {(r["kvk"], r["erp"]): r for r in read_json(OUT, [])}   # keep earlier results, upgrade if stronger

    def add(rec):
        rec.setdefault("checkedAt", now)
        k = (rec["kvk"], rec["erp"])
        old = merged.get(k)
        if not old or RANK[rec["confidence"]] >= RANK[old["confidence"]]:
            merged[k] = rec

    if a.csv:
        rows = read_csv_evidence(a.csv)
        for r in rows:
            add(r)
        print(f"CSV: {len(rows):,} ERP facts read from {a.csv}")

    if a.detect:
        import requests
        companies = [c for c in read_json(a.companies, []) if c.get("website")]
        # largest first: those are the accounts that matter and the most likely to mention an ERP
        companies.sort(key=lambda c: -(c.get("revenue") or 0) - (c.get("staff") or 0))
        companies = companies[: a.max]
        print(f"Scanning {len(companies):,} websites ({a.workers} at a time, robots.txt respected)...")
        hits = scanned = 0
        with requests.Session() as sess, ThreadPoolExecutor(a.workers) as ex:
            futs = {ex.submit(detect_site, sess, c["kvk"], c["website"]): c for c in companies}
            for f in as_completed(futs):
                scanned += 1
                res = f.result()
                for r in res:
                    add(r)
                hits += bool(res)
                if scanned % 50 == 0:
                    print(f"  {scanned:,}/{len(companies):,} scanned, {hits:,} with an ERP signal", flush=True)
        print(f"Website scan: {hits:,} of {scanned:,} sites mention or reference an ERP.")

    out = sorted(merged.values(), key=lambda r: (r["kvk"], -RANK[r["confidence"]]))
    write_json(OUT, out)
    by = {}
    for r in out:
        by.setdefault(r["confidence"], set()).add(r["kvk"])
    print(f"{len(out):,} ERP records for {len({r['kvk'] for r in out}):,} companies -> {OUT}")
    print("  by best confidence:", {k: len(v) for k, v in sorted(by.items(), key=lambda x: -RANK[x[0]])})


if __name__ == "__main__":
    main()
