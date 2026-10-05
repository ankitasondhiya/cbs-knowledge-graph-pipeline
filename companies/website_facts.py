"""
Company facts from the company's OWN website  ->  landing_zone/companies/website_facts.json

Why: no free register lists size, address and industry for every Dutch company, but the companies say it themselves:
  * KVK number  -- Dutch law requires it on a company's website / terms / footer, so it is almost always there
  * address     -- schema.org JSON-LD (PostalAddress) or "Adres:" text
  * staff       -- "over 650 medewerkers" on the About page, or JSON-LD numberOfEmployees
  * revenue     -- only where the company states it ("omzet van EUR 120 miljoen")
  * industry    -- from the page title / description, using the same keyword mapping as the Wikidata fallback
Everything is labelled as coming from the company website (text) -- it can be group-wide or marketing language, so it is only
used to FILL GAPS (a value from Wikidata / your own files always wins), and the evidence URL is kept.

Polite and legal: only the company's own public pages, robots.txt respected, a few pages per site, no register scraping.

    python website_facts.py --max 600
    python website_facts.py --source companies        # scan the already-built list instead of all Wikidata candidates
"""
import argparse
import json
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from common import LANDING, norm_kvk, read_json, section_from_industry_names, write_json
from erp_enrich import UA, _robots_ok

OUT = LANDING / "website_facts.json"

FACT_HINT = re.compile(r"over-?ons|about|contact|colofon|impressum|voorwaarden|algemene|terms|privacy|disclaimer|bedrijf|company|"
                       r"organi[sz]atie|organi[sz]ation|wie-?zijn|who-?we-?are|cijfers|figures|facts|feiten", re.I)

KVK_RE = re.compile(r"(?i)(?:\bk\.?v\.?k\.?\b|kamer\s+van\s+koophandel|chamber\s+of\s+commerce|handelsregister|\bcoc\b)"
                    r"\D{0,45}?(\d{2}\s?\d{2}\s?\d{2}\s?\d{2})(?!\d)")
STAFF_RE = re.compile(r"(?i)(\d{1,3}(?:[.,]\d{3})+|\d{2,6})\s*\+?\s*(?:medewerkers|collega'?s|werknemers|employees|mensen|professionals|specialisten|fte)\b")
REV_RE_A = re.compile(r"(?i)(?:omzet|revenue|turnover)[^€]{0,45}?(?:€|eur(?:o)?)\s*(\d[\d.,]*)\s*(miljoen|mln|million|miljard|billion|bn)\b")
REV_RE_B = re.compile(r"(?i)(?:€|eur(?:o)?)\s*(\d[\d.,]*)\s*(miljoen|mln|million|miljard|billion|bn)\b[^€]{0,25}?(?:omzet|revenue|turnover)")


def _num(txt):
    s = txt.replace(" ", "")
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".")
    elif "," in s:
        s = s.replace(",", ".") if len(s.split(",")[-1]) != 3 else s.replace(",", "")
    elif s.count(".") > 1 or (s.count(".") == 1 and len(s.split(".")[-1]) == 3 and len(s.split(".")[0]) <= 3):
        s = s.replace(".", "")
    try:
        return float(s)
    except ValueError:
        return None


def _plain(html):
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"(?s)<[^>]+>", " ", html))


def _jsonld(html):
    out = []
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html):
        try:
            out.append(json.loads(m.group(1)))
        except ValueError:
            continue
    return out


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def extract_facts(html, url):
    """Pure function: one page -> {kvk, staff, revenue, address, title, description} (only what is present)."""
    text = _plain(html)
    f = {}
    m = KVK_RE.search(text)
    if m:
        f["kvk"] = norm_kvk(re.sub(r"\s", "", m.group(1)))
    staff = [v for v in (_num(x) for x in STAFF_RE.findall(text)) if v and 10 <= v <= 1_000_000]
    if staff:
        f["staff"] = int(max(staff))
        sm = STAFF_RE.search(text)
        f["staffEvidence"] = "..." + text[max(0, sm.start() - 50):sm.end() + 30].strip() + "..."
    for rx in (REV_RE_A, REV_RE_B):
        rm = rx.search(text)
        if rm:
            n = _num(rm.group(1))
            if n:
                unit = rm.group(2).lower()
                f["revenue"] = n * (1e9 if unit in ("miljard", "billion", "bn") else 1e6)
                f["revenueEvidence"] = "..." + text[max(0, rm.start() - 20):rm.end() + 20].strip() + "..."
                break
    for node in _walk(_jsonld(html)):
        t = node.get("@type")
        t = " ".join(t) if isinstance(t, list) else str(t or "")
        if "PostalAddress" in t and "address" not in f and (node.get("streetAddress") or node.get("postalCode")):
            f["address"] = {"street": node.get("streetAddress"), "postcode": node.get("postalCode"), "city": node.get("addressLocality")}
        if re.search(r"Organization|Corporation|LocalBusiness", t):
            ne = node.get("numberOfEmployees")
            val = ne.get("value") if isinstance(ne, dict) else ne
            try:
                if val and "staff" not in f and 10 <= float(str(val).split("-")[-1].replace(",", "").replace(".", "")) <= 1_000_000:
                    f["staff"] = int(float(str(val).split("-")[-1].replace(",", "").replace(".", "")))
                    f["staffEvidence"] = "schema.org numberOfEmployees"
            except ValueError:
                pass
    tm = re.search(r"(?is)<title[^>]*>(.*?)</title>", html)
    dm = re.search(r'(?is)<meta[^>]+(?:name|property)=["\'](?:description|og:description)["\'][^>]+content=["\'](.*?)["\']', html)
    if tm:
        f["title"] = re.sub(r"\s+", " ", tm.group(1)).strip()[:200]
    if dm:
        f["description"] = re.sub(r"\s+", " ", dm.group(1)).strip()[:300]
    return f


def scan_site(session, key, website, max_pages=5):
    url = website if re.match(r"https?://", website or "", re.I) else "https://" + (website or "")
    cache, seen, todo, merged, pages = {}, set(), [url], {}, []
    while todo and len(seen) < max_pages:
        u = todo.pop(0)
        if u in seen or not _robots_ok(session, u, cache):
            continue
        seen.add(u)
        try:
            r = session.get(u, timeout=10, headers={"User-Agent": UA})
            if r.status_code != 200 or "html" not in r.headers.get("content-type", "html"):
                continue
            html = r.text[:700_000]
        except Exception:
            continue
        pages.append(u)
        for k, v in extract_facts(html, u).items():
            if k not in merged and v not in (None, "", {}):
                merged[k] = v
                if k == "kvk":
                    merged["kvkEvidenceUrl"] = u
        if len(seen) == 1:
            for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
                full = urljoin(r.url, href).split("?")[0]
                if urlparse(full).netloc == urlparse(r.url).netloc and FACT_HINT.search(urlparse(full).path) and full not in seen:
                    todo.append(full)
        time.sleep(0.3)
    if merged:
        names = [merged.get("title"), merged.get("description")]
        merged["sectionGuess"] = section_from_industry_names([n for n in names if n], extra_keywords=True)
        merged["pages"] = pages
        merged["checkedAt"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return key, merged


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["wikidata", "companies"], default="wikidata")
    ap.add_argument("--max", type=int, default=600, help="max websites to scan (default 600)")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    cands = read_json(LANDING / ("wikidata_revenue.json" if a.source == "wikidata" else "companies.json"), [])
    done = read_json(OUT, {})
    todo = [c for c in cands if c.get("website") and c["kvk"] not in done]
    # who benefits most: no KvK number yet, no staff count, or no industry -- and the biggest first
    def need(c):
        return (str(c["kvk"]).startswith(("wd-", "list-"))) + (c.get("employees") is None and c.get("staff") is None) + (not (c.get("naceCodes") or c.get("industries")))
    todo.sort(key=lambda c: (-need(c), -(c.get("revenue") or 0) - (c.get("employees") or c.get("staff") or 0)))
    todo = todo[: a.max]
    print(f"Website facts: {len(cands):,} candidates, {len(done):,} already scanned, scanning {len(todo):,} websites "
          f"({a.workers} at a time, robots.txt respected)...")
    got = 0
    with requests.Session() as sess, ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(scan_site, sess, c["kvk"], c["website"]) for c in todo]
        for i, f in enumerate(as_completed(futs), 1):
            key, facts = f.result()
            done[key] = facts
            got += bool(facts)
            if i % 50 == 0:
                write_json(OUT, done)
                print(f"  {i:,}/{len(todo):,} scanned, {got:,} with facts", flush=True)
    write_json(OUT, done)
    vals = [v for v in done.values() if v]
    print(f"Done: facts for {got:,} of {len(todo):,} sites -- KvK numbers found: {sum(1 for v in vals if v.get('kvk')):,}, "
          f"staff: {sum(1 for v in vals if v.get('staff')):,}, address: {sum(1 for v in vals if v.get('address')):,}, "
          f"revenue: {sum(1 for v in vals if v.get('revenue')):,} -> {OUT}")


if __name__ == "__main__":
    main()
