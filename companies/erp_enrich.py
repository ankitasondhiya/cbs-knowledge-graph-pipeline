"""
Which ERP does each company use?  ->  landing_zone/companies/erp.json

NONE of KVK / GLEIF / Wikidata / CBS says which ERP a company runs, so this step
collects EVIDENCE from several places and records how strong each piece of evidence is:

  1. --csv FILE         Facts you already have or research: your CRM, sales/partner knowledge, a bought
                        technographics export (HG Insights, BuiltWith, ...). Default confidence: high.
                        Columns:  kvk, erp, [confidence], [source], [evidence_url], [note]
  2. --references FILE  VENDOR CUSTOMER-REFERENCE PAGES. List the case-study / customer-logo pages of AFAS,
                        Exact, SAP partners, Unit4 ... (vendor, url, erp). We open them (robots.txt respected),
                        follow case-study links on the same site and look for YOUR target companies by name.
                        A company named in a case-study headline or URL = high; named elsewhere on the page = medium.
  3. --detect           Free, best-effort scan of each company's own website (home page plus careers / about /
                        privacy pages): a link/script belonging to an ERP product = medium, the product merely named
                        in the text = low. Most companies say nothing about their ERP on their website.

Job-vacancy evidence and "why now" triggers live in signals_enrich.py (it merges into the same erp.json).

Every result keeps its source + evidence + the ERP's lifecycle (current / legacy / unknown), so the dashboard can
show WHY it thinks a company uses an ERP and flag migration candidates.

    python erp_enrich.py --csv my_erp_facts.csv
    python erp_enrich.py --references erp_reference_sources.csv
    python erp_enrich.py --detect --max 300
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
from erp_catalog import CATALOG, canonical, lifecycle_of, mentions, slug  # noqa: F401  (slug re-exported for load_companies)
from name_match import build_index, find_companies, fold, name_key, snippet

OUT = LANDING / "erp.json"
UA = "cbs-knowledge-graph-pipeline/1.0 (ERP research; contact via GitHub)"
RANK = {"high": 3, "medium": 2, "low": 1}

PAGE_HINT = re.compile(r"vacatur|career|werken-?bij|jobs?\b|over-?ons|about|privacy|cookie|nieuws|news|cases?", re.I)


# ---------------------------------------------------------------- CSV evidence
ACCOUNT_STATUS = {"customer": "customer", "klant": "customer", "prospect": "prospect", "lead": "prospect",
                  "lost": "lost", "verloren": "lost", "partner": "partner", "competitor": "competitor",
                  "do-not-contact": "do-not-contact", "do not contact": "do-not-contact", "dnc": "do-not-contact",
                  "niet benaderen": "do-not-contact"}


def read_csv_evidence(path, companies=None):
    """Team knowledge -> (erp_records, accounts, unmatched_names).

    Sales people know company NAMES, not KVK numbers, so a row may carry either `kvk` or `company`
    (matched to the company list by name; unmatched names are reported, never guessed).
    A row may hold an ERP fact, an account status/owner (customer, prospect, lost, partner, competitor,
    do-not-contact), or both:  kvk | company | erp | confidence | source | evidence_url | note | account_status | owner
    """
    index = build_index(companies or [])
    out, accounts, unmatched = [], {}, []
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        for row in csv.DictReader(f, dialect=dialect):
            row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            kv = row.get("kvk") or ""
            kvk = kv if re.match(r"(?i)^(wd|manual)-", kv) else (norm_kvk(kv) if kv else None)   # keep synthetic ids as they are
            if not kvk and row.get("company"):
                key = name_key(row["company"])
                kvk = index.get(key) if key else None
                if not kvk:
                    unmatched.append(row["company"])
                    continue
            erp = row.get("erp")
            status = ACCOUNT_STATUS.get((row.get("account_status") or "").lower())
            if not kvk or not (erp or status or row.get("owner")):
                continue
            if status or row.get("owner"):
                accounts[kvk] = {"status": status, "owner": row.get("owner") or None, "note": row.get("note") or None}
            conf = (row.get("confidence") or row.get("erp_confidence") or "high").split(";")[0].strip().lower()
            for one in [x.strip() for x in re.split(r"[;,]", erp or "") if x.strip()]:     # "AFAS; Exact" -> two records
                name, vendor, life = canonical(one)
                out.append({"kvk": kvk, "erp": name, "vendor": vendor, "lifecycle": life,
                            "confidence": conf if conf in RANK else "high",
                            "source": row.get("source") or row.get("erp_source") or "our own knowledge (csv)",
                            "evidence": row.get("note") or None, "evidenceUrl": row.get("evidence_url") or None})
    return out, accounts, unmatched


# ----------------------------------------------------------- website detection
def scan_html(html, base_url):
    """Pure function: (page HTML) -> list of hits. Testable without any network."""
    out = []
    for name, kind, _frag, evidence in mentions(html):
        out.append({"erp": name, "vendor": CATALOG[name]["vendor"], "lifecycle": lifecycle_of(name),
                    "confidence": "medium" if kind == "strong" else "low",
                    "source": "website technology fingerprint" if kind == "strong" else "website text mention",
                    "evidence": evidence, "evidenceUrl": base_url})
    return out


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


VAC_PATH = re.compile(r"vacatur|vacanc|/jobs?\b|/job/|werken|career|carri|functie|opening|solliciteer", re.I)
CAREER_INDEX = re.compile(r"vacatur|vacancies|career|carri|werken-?bij|jobs?\b", re.I)


def _site(host):
    """'werkenbij.philips.com' -> 'philips.com' (careers sites often live on a sub-domain)."""
    p = host.lower().split(":")[0].split(".")
    return ".".join(p[-2:]) if len(p) >= 2 else host


def detect_site(session, kvk, website, max_pages=10):
    """Home page -> about / careers / privacy pages -> from a careers index, individual vacancy pages.
    An ERP product named on the company's OWN vacancy page ('ervaring met SAP S/4HANA') is medium evidence."""
    if not website:
        return []
    url = website if re.match(r"https?://", website, re.I) else "https://" + website
    cache, seen, todo, found = {}, set(), [url], {}
    home_site = _site(urlparse(url).netloc)

    def links(html, base, pattern, cap):
        out = []
        for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
            full = urljoin(base, href).split("?")[0]
            pu = urlparse(full)
            if pu.scheme in ("http", "https") and _site(pu.netloc) == home_site and pattern.search(pu.netloc + pu.path) \
                    and full not in seen and full not in todo and len(out) < cap:
                out.append(full)
        return out

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
        path = urlparse(r.url).netloc + urlparse(r.url).path
        on_vacancy = bool(VAC_PATH.search(path))
        for h in scan_html(html, u):
            if on_vacancy and h["confidence"] == "low":
                h = {**h, "confidence": "medium", "source": "company vacancy page text mention"}
            if RANK[h["confidence"]] > RANK.get(found.get(h["erp"], {}).get("confidence", ""), 0):
                found[h["erp"]] = {**h, "kvk": kvk}
        if len(seen) == 1:
            todo += links(html, r.url, PAGE_HINT, 6)
        if CAREER_INDEX.search(path):
            todo[:0] = links(html, r.url, VAC_PATH, 4)       # individual job ads first
        time.sleep(0.3)
    return list(found.values())


# ------------------------------------------------- vendor customer-reference pages
REF_LINK = re.compile(r"klant|case|referent|customer|success|verhaal|verhalen|stor(?:y|ies)|reference|client|portfolio", re.I)


def _plain(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def _headline(html):
    """Text of <title>, <h1>, <h2>: where a case study names its customer."""
    parts = re.findall(r"(?is)<(?:title|h1|h2)[^>]*>(.*?)</(?:title|h1|h2)>", html)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", p) for p in parts)


def match_reference_page(html, url, index, erp, vendor_label):
    """Pure function: one vendor reference page -> ERP evidence records for the target companies named on it."""
    from erp_catalog import canonical as _canon
    name, vendor, life = _canon(erp)
    body = _plain(html)
    head_found = find_companies(_headline(html), index)
    url_fold = fold(urlparse(url).path)
    recs = []
    for kvk, (key, s0, e0, folded) in find_companies(body, index).items():
        in_head = kvk in head_found or re.search(r"(?<![a-z0-9])" + re.escape(key) + r"(?![a-z0-9])", url_fold)
        recs.append({"kvk": kvk, "erp": name, "vendor": vendor, "lifecycle": life,
                     "confidence": "high" if in_head else "medium",
                     "source": f"{vendor_label} customer reference page",
                     "evidence": snippet(folded, s0, e0), "evidenceUrl": url})
    return recs


def scan_references(session, sources_csv, companies, max_pages=150):
    index = build_index(companies)
    if not index:
        print("  (no company names usable for matching -- run build_companies.py first)")
        return []
    with open(sources_csv, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096); f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f, dialect=dialect)]
    found = []
    for row in rows:
        start, vendor = row.get("url"), row.get("vendor") or "vendor"
        erp = row.get("erp") or vendor
        if not start or vendor.startswith("#") or start.startswith("#"):
            continue
        cache, seen, todo, hits, pages = {}, set(), [start], 0, 0
        host = urlparse(start).netloc
        while todo and pages < max_pages:
            u = todo.pop(0)
            if u in seen or not _robots_ok(session, u, cache):
                continue
            seen.add(u)
            try:
                r = session.get(u, timeout=15, headers={"User-Agent": UA})
                if r.status_code != 200:
                    continue
                html = r.text[:1_500_000]
            except Exception:
                continue
            pages += 1
            recs = match_reference_page(html, r.url, index, erp, vendor)
            found += recs; hits += len(recs)
            if urlparse(r.url).netloc == host:
                for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
                    full = urljoin(r.url, href).split("?")[0]
                    if urlparse(full).netloc == host and REF_LINK.search(urlparse(full).path) and full not in seen:
                        todo.append(full)
            time.sleep(0.5)
        print(f"  {vendor} ({erp}): {pages} pages read, {hits} of your companies named", flush=True)
    return found


def merge_records(new_records, path=OUT):
    """Merge ERP evidence into erp.json: one record per (company, ERP), the strongest evidence wins.
    Shared with signals_enrich.py so both steps add to the same file."""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {(r["kvk"], r["erp"]): r for r in read_json(path, [])}
    for rec in new_records:
        rec.setdefault("checkedAt", now)
        rec.setdefault("lifecycle", lifecycle_of(rec["erp"]))
        old = merged.get((rec["kvk"], rec["erp"]))
        if not old or RANK[rec["confidence"]] >= RANK[old["confidence"]]:
            merged[(rec["kvk"], rec["erp"])] = rec
    out = sorted(merged.values(), key=lambda r: (r["kvk"], -RANK[r["confidence"]]))
    write_json(path, out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", help="your own ERP facts: kvk, erp, [confidence], [source], [evidence_url], [note]")
    ap.add_argument("--references", help="CSV of vendor customer-reference pages: vendor, url, erp (see erp_reference_sources.template.csv)")
    ap.add_argument("--ref-max-pages", type=int, default=150, help="max pages read per reference source (default 150)")
    ap.add_argument("--detect", action="store_true", help="scan company websites for ERP fingerprints")
    ap.add_argument("--max", type=int, default=500, help="max websites to scan (default 500)")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--companies", default=str(LANDING / "companies.json"))
    a = ap.parse_args()
    if not a.csv and not a.detect and not a.references:
        sys.exit("Nothing to do: pass --csv FILE, --references FILE and/or --detect.")

    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    merged = {(r["kvk"], r["erp"]): r for r in read_json(OUT, [])}   # keep earlier results, upgrade if stronger

    def add(rec):
        rec.setdefault("checkedAt", now)
        rec.setdefault("lifecycle", lifecycle_of(rec["erp"]))
        k = (rec["kvk"], rec["erp"])
        old = merged.get(k)
        if not old or RANK[rec["confidence"]] >= RANK[old["confidence"]]:
            merged[k] = rec

    if a.csv:
        rows, accounts, unmatched = read_csv_evidence(a.csv, read_json(a.companies, []))
        for r in rows:
            add(r)
        if accounts:
            acc_path = LANDING / "accounts.json"
            allacc = read_json(acc_path, {})
            allacc.update(accounts)
            write_json(acc_path, allacc)
        print(f"CSV: {len(rows):,} ERP facts and {len(accounts):,} account statuses read from {a.csv}")
        if unmatched:
            print(f"  {len(unmatched):,} company names could not be matched to a company in the list "
                  f"(add the KVK number to the row, or the company is not in the list): {unmatched[:15]}")

    if a.references:
        import requests
        with requests.Session() as sess:
            recs = scan_references(sess, a.references, read_json(a.companies, []), a.ref_max_pages)
        for r in recs:
            add(r)
        print(f"Vendor references: {len(recs):,} customer matches for {len({r['kvk'] for r in recs}):,} of your companies.")

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
