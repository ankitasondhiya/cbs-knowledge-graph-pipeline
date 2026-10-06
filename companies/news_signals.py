"""
Dutch IT / business media  ->  buying signals  (merged into landing_zone/companies/signals.json)         FREE, headlines only

Why: the trade media (Computable, AG Connect, Dutch IT Channel, Emerce, Executive Finance, CFO ...) report which Dutch companies run big
migration / transformation / AI programmes. A dated headline with a link is a citeable "why now" for the sales team.

What it does (and does not do):
  * reads ONLY what each publisher offers as an RSS/Atom feed (title, teaser, link, date) -- it never opens article pages, never logs in,
    never touches paywalled text; robots.txt is respected for the feed URL; a feed that cannot be found is skipped with a message
  * finds the companies of companies.json in the headline / teaser (conservative whole-word name matching, name_match.py)
  * keeps an item only if it speaks about migration / transformation / ERP (type news_migration) or data / AI (type news_ai)
  * stores title + link + date + the publication -- no article text

Feeds are listed in news_feeds.csv (name,url). `url` may be the site's home page (the feed link is auto-discovered) or the RSS URL itself.
Check each publisher's terms for automated reading of its feed before you run this on a schedule.

    python news_signals.py [--max-age-days 365]
"""
import argparse
import csv
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urljoin

from common import LANDING, norm_kvk, read_json, write_json
from name_match import build_index, find_companies

FEEDS = Path(__file__).with_name("news_feeds.csv")
OUT = LANDING / "signals.json"
MAX_PER_COMPANY = 8
UA = "cbs-knowledge-graph-pipeline/1.1 (+https://github.com/ankitasondhiya/cbs-knowledge-graph-pipeline; news headlines for B2B research)"

MIGRATION = re.compile(r"migrat|transformatie|transformation|moderniser|overstap|stapt over|vervangt|vervanging|nieuw(?:e)? erp|erp|"
                       r"s/?4 ?hana|cloud[- ]?(?:first|strategie|transitie|migratie)|go[- ]?live|uitrol|roll[- ]?out|implementatie|"
                       r"implementation|legacy|platform(?:keuze|wissel)|datamigratie|data migration|ict-programma|it-programma", re.I)
AI_DATA = re.compile(r"\bai\b|\bkunstmatige intelligentie|generatieve ai|genai|machine learning|data[- ]?platform|datastrategie|"
                     r"data[- ]?architectuur|ai[- ]?fabriek|ai factory|datagedreven|automatiser|\brpa\b", re.I)


def read_feeds():
    if not FEEDS.exists():
        return []
    with FEEDS.open(encoding="utf-8-sig", newline="") as f:
        return [(r["name"].strip(), r["url"].strip()) for r in csv.DictReader(f) if (r.get("name") or "").strip() and (r.get("url") or "").strip()]


def robots_ok(session, url):
    from urllib.robotparser import RobotFileParser
    from urllib.parse import urlparse
    p = urlparse(url)
    rp = RobotFileParser()
    try:
        r = session.get(f"{p.scheme}://{p.netloc}/robots.txt", timeout=10, headers={"User-Agent": UA})
        if r.status_code != 200:
            return True
        rp.parse(r.text.splitlines())
        return rp.can_fetch(UA, url)
    except Exception:
        return True


def find_feed_url(session, url):
    """Home page or feed URL -> the RSS/Atom URL (auto-discovery through <link rel="alternate">), or None."""
    r = session.get(url, timeout=20, headers={"User-Agent": UA})
    r.raise_for_status()
    head = r.text[:3000].lstrip()
    if head.startswith("<?xml") or re.match(r"<(rss|feed)\b", head, re.I):
        return url
    m = re.search(r'<link[^>]+type=["\']application/(?:rss|atom)\+xml["\'][^>]*>', r.text, re.I)
    if m:
        h = re.search(r'href=["\']([^"\']+)', m.group(0), re.I)
        if h:
            return urljoin(r.url, h.group(1))
    return None


def _text(el, *names):
    for n in names:
        for c in el.iter():
            if c.tag.split("}")[-1] == n and (c.text or c.attrib.get("href")):
                return (c.text or c.attrib.get("href") or "").strip()
    return ""


def parse_feed(xml_text):
    """RSS 2.0 / Atom text -> [{title, summary, url, date 'YYYY-MM-DD' or ''}]"""
    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    items = [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]
    out = []
    for it in items:
        title = re.sub(r"\s+", " ", _text(it, "title"))
        summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", _text(it, "description", "summary")))[:400]
        link = _text(it, "link")
        raw = _text(it, "pubDate", "published", "updated", "date")
        date = ""
        if raw:
            try:
                date = parsedate_to_datetime(raw).date().isoformat()
            except (TypeError, ValueError):
                date = raw[:10] if re.match(r"\d{4}-\d{2}-\d{2}", raw) else ""
        if title:
            out.append({"title": title, "summary": summary, "url": link, "date": date})
    return out


def classify_item(item):
    blob = f"{item['title']} {item['summary']}"
    if MIGRATION.search(blob):
        return "news_migration", "migration / transformation programme in the news"
    if AI_DATA.search(blob):
        return "news_ai", "data / AI programme in the news"
    return None, None


def process_items(items, publication, companies, max_age_days=365, today=None):
    """-> {kvk: [signal]}. Pure function (no network)."""
    index = build_index(companies)
    today = today or datetime.now(timezone.utc).date()
    cutoff = (today - timedelta(days=max_age_days)).isoformat()
    sig = {}
    for it in items:
        if it["date"] and it["date"] < cutoff:
            continue
        typ, label = classify_item(it)
        if not typ:
            continue
        for kvk in find_companies(f"{it['title']} {it['summary']}", index):
            sig.setdefault(kvk, []).append({"type": typ, "label": label, "erp": None, "title": f"{it['title']} ({publication})",
                                            "url": it["url"], "date": it["date"], "evidence": "headline / teaser in " + publication})
    return sig


def merge(old, new):
    for k, v in new.items():
        have = {(s.get("url"), s.get("title")) for s in old.get(k, [])}
        merged = old.get(k, []) + [s for s in v if (s.get("url"), s.get("title")) not in have]
        merged.sort(key=lambda s: s.get("date") or "", reverse=True)                 # newest first ...
        merged.sort(key=lambda s: s["type"] not in ("erp_change", "news_migration"))   # ... but migration signals before the rest
        old[k] = merged[:MAX_PER_COMPANY]
    return old


def main():
    import requests
    ap = argparse.ArgumentParser()
    ap.add_argument("--max-age-days", type=int, default=365)
    ap.add_argument("--companies", default=str(LANDING / "companies.json"))
    a = ap.parse_args()
    companies = read_json(a.companies, [])
    feeds = read_feeds()
    if not companies or not feeds:
        sys.exit("Need companies.json (build_companies.py) and companies/news_feeds.csv.")
    new, n_items = {}, 0
    with requests.Session() as s:
        for name, url in feeds:
            try:
                if not robots_ok(s, url):
                    print(f"  {name}: robots.txt disallows -- skipped")
                    continue
                feed = find_feed_url(s, url)
                if not feed:
                    print(f"  {name}: no RSS/Atom feed found on {url} -- put the feed URL in news_feeds.csv")
                    continue
                if feed != url and not robots_ok(s, feed):
                    print(f"  {name}: robots.txt disallows the feed -- skipped")
                    continue
                r = s.get(feed, timeout=30, headers={"User-Agent": UA})
                r.raise_for_status()
                items = parse_feed(r.content)
            except Exception as e:
                print(f"  {name}: skipped ({str(e)[:160]})")
                continue
            n_items += len(items)
            got = process_items(items, name, companies, a.max_age_days)
            for k, v in got.items():
                new.setdefault(k, []).extend(v)
            print(f"  {name}: {len(items):,} items, {sum(len(v) for v in got.values()):,} about our companies")
    out = merge(read_json(OUT, {}), new)
    write_json(OUT, out)
    mig = sum(1 for v in new.values() if any(x["type"] == "news_migration" for x in v))
    print(f"News: {n_items:,} items read, signals for {len(new):,} companies ({mig:,} with a migration / transformation headline) -> {OUT}")


if __name__ == "__main__":
    main()
