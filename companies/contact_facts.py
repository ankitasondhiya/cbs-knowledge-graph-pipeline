"""
Company contact details from the company's OWN website  ->  landing_zone/companies/contacts.json + company_contacts.csv

What it collects (company level, published by the company for exactly this purpose):
  * role mailboxes  info@ / contact@ / sales@ / pers@ / ir@ / hr@ ...  on the company's own domain
  * phone numbers   from tel: links and "Tel" lines
  * leadership      NAME + ROLE (CTO, CIO, CFO, IT director/manager ...) + the page it was found on -- from schema.org Person data or
                    "Name, CFO" style text on leadership / team / about pages. No e-mail for them.
What it deliberately does NOT collect: personal e-mail addresses (firstname.lastname@...), anything from LinkedIn, guessed addresses,
or pages behind logins. Personal-looking addresses are only COUNTED ("personalAddressesSkipped") so you know they exist.

Polite and legal: only the company's public pages, robots.txt respected, ~6 pages per site, a pause between requests.
Use of the result (e.g. event invitations) still needs your compliance check: GDPR / Telecommunicatiewet, opt-out, source on record.

    python contact_facts.py --source companies --max 500        # biggest companies first
    python contact_facts.py --csv target_accounts.csv            # the dashboard's CSV export (columns company, kvk, website)
"""
import argparse
import csv
import json
import re
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import urljoin, urlparse

from common import LANDING, read_json, write_json
from erp_enrich import UA, _robots_ok
from website_facts import _jsonld, _plain, _txt, _walk

OUT = LANDING / "contacts.json"
CSV_OUT = LANDING / "company_contacts.csv"

PAGE_HINT = re.compile(r"contact|over-?ons|about|team|bestuur|management|leadership|directie|board|colofon|impressum|"
                       r"pers\b|press|media|investor|organi[sz]atie|who-?we-?are|wie-?zijn", re.I)
ROLE_BOX = {"info", "contact", "sales", "verkoop", "support", "service", "klantenservice", "pers", "press", "media", "communicatie",
            "communications", "comms", "hr", "jobs", "vacatures", "recruitment", "careers", "werken", "ir", "investors",
            "investor.relations", "secretariaat", "receptie", "reception", "office", "mail", "hello", "hallo", "administratie",
            "finance", "financien", "inkoop", "procurement", "marketing", "events", "evenementen", "kantoor", "algemeen", "post", "nl"}
EMAIL = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"(?:\+31|0031|\b0)[\s\-.]?(?:\(0\))?[1-9]\d?(?:[\s\-.]?\d){6,8}\b")
ROLE = (r"(?:CTO|CIO|CFO|CDO|COO|Chief (?:Technology|Information|Financial|Digital|Data|Operating) Officer|"
        r"(?:IT|ICT|Finance|Financial|Digital|Data)[- ](?:director|manager|directeur|lead|hoofd)|"
        r"(?:director|directeur|manager|hoofd|head of|Head of)[- ](?:IT|ICT|Finance|Financial|Digital|Data|Information Technology)|"
        r"financieel directeur|directeur (?:financi[eë]n|ICT|IT)|manager (?:IT|ICT))")
NAME = r"([A-Z][\w.'’\-]+(?:\s(?:van|de|der|den|ten|ter|von|el|al|in 't|van der|van den|van de)){0,2}(?:\s[A-Z][\w.'’\-]+){1,2})"
LEAD1 = re.compile(NAME + r"\s*[,–—|:\-]\s*(" + ROLE + r")\b")
LEAD2 = re.compile(r"\b(" + ROLE + r")\s*[:–—|\-]\s*" + NAME)
LEAD_ROLE_JSON = re.compile(ROLE, re.I)


def _domain(url):
    h = urlparse(url if "//" in url else "//" + url).netloc.lower().split(":")[0]
    return h[4:] if h.startswith("www.") else h


def _same_site(email_domain, site_domain):
    a, b = email_domain.lower(), site_domain.lower()
    return a == b or a.endswith("." + b) or b.endswith("." + a)


def extract_contacts(html, site_domain):
    """Pure function: one page -> {emails:[role mailboxes], skipped:int, phones:[...], leaders:[{name, role}]}"""
    text = _plain(html)
    raw = set(re.findall(r"mailto:([^\"'?\s>]+)", html, re.I)) | set(EMAIL.findall(html)) | set(EMAIL.findall(text))
    emails, skipped = set(), 0
    for e in raw:
        e = e.strip().strip(".,;:").lower()
        if not EMAIL.fullmatch(e):
            continue
        local, dom = e.split("@", 1)
        if not _same_site(dom, site_domain) or re.search(r"\.(png|jpg|jpeg|gif|svg|webp)$", e):
            continue
        if local in ROLE_BOX:
            emails.add(e)
        else:
            skipped += 1          # looks personal (name@...): counted, never stored
    phones = []
    for p in re.findall(r"tel:([+\d\s().\-]+)", html, re.I) + PHONE.findall(text):
        p = re.sub(r"\s+", " ", p).strip(" .-")
        digits = re.sub(r"\D", "", p)
        if 9 <= len(digits) <= 13 and p not in phones:
            phones.append(p)
    leaders = []
    for node in _walk(_jsonld(html)):
        t = node.get("@type")
        if "Person" in (" ".join(t) if isinstance(t, list) else str(t or "")):
            nm, jt = _txt(node.get("name")), _txt(node.get("jobTitle"))
            if nm and jt and LEAD_ROLE_JSON.search(jt):
                leaders.append({"name": nm, "role": jt})
    for m in LEAD1.finditer(text):
        leaders.append({"name": m.group(1).strip(), "role": m.group(2).strip()})
    for m in LEAD2.finditer(text):
        leaders.append({"name": m.group(2).strip(), "role": m.group(1).strip()})
    return {"emails": sorted(emails), "skipped": skipped, "phones": phones[:3], "leaders": leaders}


def scan_site(session, key, website, max_pages=6):
    url = website if re.match(r"https?://", website or "", re.I) else "https://" + (website or "")
    dom = _domain(url)
    cache, seen, todo = {}, set(), [url]
    res = {"website": url, "emails": set(), "skipped": 0, "phones": [], "leaders": {}, "pages": []}
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
        got = extract_contacts(html, dom)
        res["pages"].append(u)
        res["emails"] |= set(got["emails"])
        res["skipped"] += got["skipped"]
        res["phones"] += [p for p in got["phones"] if p not in res["phones"]]
        for l in got["leaders"]:
            res["leaders"].setdefault((l["name"].lower(), l["role"].lower()), {**l, "url": u})
        if len(seen) == 1:
            for href in re.findall(r'href=["\']([^"\'#]+)', html, re.I):
                full = urljoin(r.url, href).split("?")[0]
                if _same_site(_domain(full), dom) and PAGE_HINT.search(urlparse(full).path) and full not in seen and full not in todo:
                    todo.append(full)
        time.sleep(0.4)
    out = {"website": url, "emails": sorted(res["emails"])[:6], "personalAddressesSkipped": res["skipped"],
           "phones": res["phones"][:3], "leaders": list(res["leaders"].values())[:6], "pages": res["pages"],
           "checkedAt": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    return key, out


def read_targets(a):
    if a.csv:
        if not Path(a.csv).exists():
            raise SystemExit(f"CSV not found: {a.csv} -- commit the dashboard's CSV export (columns company, kvk, website) at that path.")
        with open(a.csv, encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        return [{"kvk": r.get("kvk") or r.get("company"), "name": r.get("company") or r.get("name"), "website": r.get("website")}
                for r in rows if (r.get("website") or "").strip()]
    cs = [c for c in read_json(LANDING / "companies.json", []) if c.get("website")]
    cs.sort(key=lambda c: -((c.get("revenue") or 0) + (c.get("staff") or 0) * 1e5))
    return [{"kvk": c["kvk"], "name": c.get("name"), "website": c["website"]} for c in cs[: a.max]]


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", choices=["companies"], default="companies")
    ap.add_argument("--csv", help="the dashboard's CSV export (company, kvk, website)")
    ap.add_argument("--max", type=int, default=500)
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    targets = read_targets(a)
    done = read_json(OUT, {})
    print(f"Contacts: {len(targets):,} companies with a website, scanning {len(targets):,} ({a.workers} at a time, robots.txt respected)...")
    names = {str(t["kvk"]): t.get("name") for t in targets}
    with requests.Session() as sess, ThreadPoolExecutor(a.workers) as ex:
        futs = [ex.submit(scan_site, sess, str(t["kvk"]), t["website"]) for t in targets]
        for i, f in enumerate(as_completed(futs), 1):
            k, v = f.result()
            done[k] = {**v, "name": names.get(k)}
            if i % 50 == 0:
                print(f"  {i:,}/{len(targets):,}", flush=True)
                write_json(OUT, done)
    write_json(OUT, done)
    keys = [str(t["kvk"]) for t in targets]
    rows = [done[k] for k in keys if k in done]
    with CSV_OUT.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["company", "kvk", "website", "role_mailboxes", "phones", "leaders_name_role_source", "personal_addresses_skipped", "pages_checked"])
        for k in keys:
            v = done.get(k)
            if v:
                w.writerow([v.get("name"), k, v["website"], "; ".join(v["emails"]), "; ".join(v["phones"]),
                            " | ".join(f"{l['name']} - {l['role']} ({l['url']})" for l in v["leaders"]),
                            v["personalAddressesSkipped"], len(v["pages"])])
    print(f"Done: role mailboxes for {sum(1 for v in rows if v['emails']):,} of {len(rows):,} companies, phones for "
          f"{sum(1 for v in rows if v['phones']):,}, named CTO/CFO/IT roles for {sum(1 for v in rows if v['leaders']):,} "
          f"({sum(len(v['leaders']) for v in rows):,} people) -> {CSV_OUT}")


if __name__ == "__main__":
    main()
