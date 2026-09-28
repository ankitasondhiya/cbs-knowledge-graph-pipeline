"""
KVK Basisprofiel -> landing_zone/companies/kvk_profiles.jsonl

Adds what GLEIF doesn't have: SBI activity codes (-> CBS industry), number
of people working there (-> CBS size band), trade names, visit address,
websites.

KVK is NOT free for production use: EUR 6.40/month per API key + EUR 0.02 per
Basisprofiel call (Zoeken is free, but it cannot filter on SBI). So this
script has a hard budget cap and caches every profile it ever fetched --
you never pay twice for the same company.

Which KVK numbers get looked up (in this order of preference):
    --kvk-file list.csv    a list you already have (KVK selection file, overheid.io export...)
    otherwise              the KVK numbers found in GLEIF (fetch_gleif.py), active entities only
    --keywords a,b,c       only GLEIF companies whose name contains one of these words
                           (cheap way to pilot one industry, e.g. hotel,restaurant,catering)

    python fetch_kvk.py --test                          # KVK test environment, fictitious data, no key needed
    set KVK_API_KEY=...  &&  python fetch_kvk.py --max-calls 500 --keywords hotel,restaurant,horeca,catering
"""
import argparse
import csv
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

from common import LANDING, norm_kvk, read_jsonl, append_jsonl

PROD = "https://api.kvk.nl/api/v1"
TEST = "https://api.kvk.nl/test/api/v1"
TEST_KEY = "l7xx1f2691f2520d487b902f4e0b57a0b197"          # public test key, published by KVK
TEST_KVK_NUMBERS = ["68750110", "90001354", "68727720", "90004760", "69599084", "69599068",
                    "69599076", "55344526", "90003942", "55505201"]  # KVK's documented test companies
COST_PER_CALL = 0.02
OUT = LANDING / "kvk_profiles.jsonl"
OUT_TEST = LANDING / "kvk_profiles.test.jsonl"


def _first(*vals):
    for v in vals:
        if v not in (None, "", [], {}):
            return v
    return None


def slim(p, kvk):
    emb = p.get("_embedded") or {}
    hoofd = emb.get("hoofdvestiging") or p.get("hoofdvestiging") or {}
    eig = emb.get("eigenaar") or p.get("eigenaar") or {}
    adressen = hoofd.get("adressen") or p.get("adressen") or []
    visit = next((a for a in adressen if (a.get("type") or "").lower().startswith("bezoek")), adressen[0] if adressen else {})
    sbi = [{"code": s.get("sbiCode"), "desc": s.get("sbiOmschrijving"),
            "main": str(s.get("indHoofdactiviteit", "")).lower() in ("ja", "true", "1")}
           for s in (p.get("sbiActiviteiten") or hoofd.get("sbiActiviteiten") or [])]
    main = next((s for s in sbi if s["main"]), sbi[0] if sbi else None)
    staff = _first(p.get("totaalWerkzamePersonen"), hoofd.get("totaalWerkzamePersonen"))
    return {
        "kvk": norm_kvk(p.get("kvkNummer") or kvk),
        "name": p.get("naam") or _first(*[(h or {}).get("naam") for h in (p.get("handelsnamen") or [])]),
        "tradeNames": [h.get("naam") for h in (p.get("handelsnamen") or []) if h.get("naam")],
        "sbi": sbi,
        "mainSbi": main["code"] if main else None,
        "mainSbiDesc": main["desc"] if main else None,
        "staff": int(staff) if staff not in (None, "") else None,
        "legalForm": _first(eig.get("rechtsvorm"), eig.get("uitgebreideRechtsvorm")),
        "address": {"street": visit.get("straatnaam"), "number": visit.get("huisnummer"),
                    "postcode": visit.get("postcode"), "city": visit.get("plaats")} if visit else None,
        "websites": hoofd.get("websites") or p.get("websites") or [],
        "registered": p.get("formeleRegistratiedatum"),
        "fetchedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }


def kvk_numbers_from_file(path):
    nums = []
    with open(path, encoding="utf-8-sig") as f:
        sample = f.read(4096)
        f.seek(0)
        if re.search(r"[;,\t]", sample.splitlines()[0] if sample else ""):
            dialect = csv.Sniffer().sniff(sample, delimiters=";,\t")
            reader = csv.DictReader(f, dialect=dialect)
            col = next((c for c in reader.fieldnames if re.sub(r"[^a-z]", "", c.lower()) in
                        ("kvk", "kvknummer", "kvknr", "dossiernummer", "kvknumber")), None)
            if not col:
                sys.exit(f"No KVK-number column found in {path}. Columns: {reader.fieldnames}")
            nums = [row[col] for row in reader]
        else:
            nums = [line for line in f]
    return [n for n in (norm_kvk(x) for x in nums) if n]


def kvk_numbers_from_gleif(keywords):
    rows = read_jsonl(LANDING / "gleif_nl.jsonl")
    if not rows:
        sys.exit("No GLEIF data yet -- run: python fetch_gleif.py")
    kw = [k.strip().lower() for k in keywords.split(",")] if keywords else []
    out = []
    for r in rows:
        if not r.get("kvk") or r.get("entityStatus") != "ACTIVE" or r.get("category") in ("FUND",):
            continue
        if kw:
            names = " ".join([r.get("name") or ""] + (r.get("otherNames") or [])).lower()
            if not any(k in names for k in kw):
                continue
        out.append(r["kvk"])
    return list(dict.fromkeys(out))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="KVK test environment (fictitious companies, no key needed)")
    ap.add_argument("--kvk-file", help="CSV/TXT with KVK numbers to look up")
    ap.add_argument("--keywords", help="only GLEIF companies whose name contains one of these (comma-separated)")
    ap.add_argument("--max-calls", type=int, default=200, help="budget cap: max paid calls this run (default 200 = EUR 4)")
    a = ap.parse_args()

    base, key, out = (TEST, TEST_KEY, OUT_TEST) if a.test else (PROD, os.environ.get("KVK_API_KEY"), OUT)
    if not key:
        sys.exit("Set KVK_API_KEY (from developers.kvk.nl), or run with --test to try the KVK test environment.")

    if a.test:
        wanted = TEST_KVK_NUMBERS
    elif a.kvk_file:
        wanted = kvk_numbers_from_file(a.kvk_file)
    else:
        wanted = kvk_numbers_from_gleif(a.keywords)

    have = {r["kvk"] for r in read_jsonl(out) if r.get("kvk")}
    todo = [k for k in wanted if k not in have]
    run = todo[: a.max_calls]
    print(f"KVK: {len(wanted):,} companies wanted, {len(have):,} already cached, {len(todo):,} to fetch.")
    if not a.test:
        print(f"This run: {len(run):,} calls ~= EUR {len(run) * COST_PER_CALL:,.2f} "
              f"(all {len(todo):,} would be ~EUR {len(todo) * COST_PER_CALL:,.2f}). Raise --max-calls to fetch more.")

    ok = missing = 0
    for i, kvk in enumerate(run, 1):
        r = requests.get(f"{base}/basisprofielen/{kvk}", headers={"apikey": key}, timeout=30)
        if r.status_code == 404:
            missing += 1
            append_jsonl(out, {"kvk": kvk, "notFound": True})
        elif r.status_code == 401:
            sys.exit("KVK rejected the API key (401).")
        elif r.status_code == 429:
            time.sleep(5)
            continue
        elif not r.ok:
            print(f"  {kvk}: HTTP {r.status_code} {r.text[:120]}")
        else:
            append_jsonl(out, slim(r.json(), kvk))
            ok += 1
        if i % 50 == 0:
            print(f"  {i:,}/{len(run):,}", flush=True)
        time.sleep(0.02)   # KVK allows 100/s; stay well below
    print(f"Done: {ok:,} profiles saved, {missing:,} not found -> {out}")


if __name__ == "__main__":
    main()
