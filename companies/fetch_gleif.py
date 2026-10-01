"""
GLEIF (LEI register) -> landing_zone/companies/gleif_nl.jsonl       FREE, no API key.

Keeps per company: lei, legal name, KVK number (GLEIF 'registeredAs' where
registeredAt == RA000463), legal form (ELF code), entity status, LEI status,
legal + headquarters address, category (GENERAL / FUND / BRANCH / SOLE_PROPRIETOR).

GLEIF's API stops paging after 10,000 results, and the Netherlands has ~200k
LEI records, so there are two ways in:

    python fetch_gleif.py              # = lookup: only the KVK numbers you actually have
                                       #   (Wikidata companies, KVK profiles, --kvk-file) -- quick
    python fetch_gleif.py bulk         # ALL Dutch records from GLEIF's free daily full file
                                       #   ("Golden Copy", worldwide, several hundred MB download)
    python fetch_gleif.py parents      # parent companies for the companies in companies.json

GLEIF allows 60 API requests/minute; every mode can be stopped and restarted.
"""
import argparse
import json
import os
import sys
import time

import requests

from common import LANDING, GLEIF_KVK_AUTHORITY, lenient_json, norm_kvk, read_json, write_json, append_jsonl, read_jsonl

API = "https://api.gleif.org/api/v1"
OUT = LANDING / "gleif_nl.jsonl"
PARENTS = LANDING / "gleif_parents.json"
PAGE_SIZE = 200
BATCH = 25                   # KVK numbers per batched lookup call
MIN_INTERVAL = 1.05          # seconds between calls -> stays under 60/min
HEADERS = {"Accept": "application/vnd.api+json", "User-Agent": "cbs-knowledge-graph-pipeline (company layer)"}

_last_call = 0.0


def get(url, params=None):
    """GET with rate limiting and simple retry on 429/5xx."""
    global _last_call
    for attempt in range(6):
        wait = MIN_INTERVAL - (time.time() - _last_call)
        if wait > 0:
            time.sleep(wait)
        _last_call = time.time()
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=60)
        except requests.exceptions.ConnectionError as e:     # incl. SSL EOF / dropped connection
            print(f"  (connection dropped, retrying in {10 * (attempt + 1)}s: {type(e).__name__})", flush=True)
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code == 404:
            return None
        if r.status_code in (429, 500, 502, 503, 504):
            time.sleep(min(60, 5 * (attempt + 1)))
            continue
        r.raise_for_status()
        return lenient_json(r)
    raise RuntimeError(f"GLEIF kept failing for {url}")


def _addr(a):
    if not a:
        return None
    return {
        "lines": a.get("addressLines") or [],
        "postcode": a.get("postalCode"),
        "city": a.get("city"),
        "country": a.get("country"),
    }


def slim(rec):
    """Keep only what the company layer uses."""
    at = rec.get("attributes", {})
    ent = at.get("entity", {}) or {}
    reg = at.get("registration", {}) or {}
    reg_at = (ent.get("registeredAt") or {}).get("id")
    return {
        "lei": at.get("lei") or rec.get("id"),
        "name": (ent.get("legalName") or {}).get("name"),
        "otherNames": [n.get("name") for n in (ent.get("otherNames") or []) if n.get("name")],
        "kvk": norm_kvk(ent.get("registeredAs")) if reg_at == GLEIF_KVK_AUTHORITY else None,
        "registeredAt": reg_at,
        "registeredAs": ent.get("registeredAs"),
        "legalForm": (ent.get("legalForm") or {}).get("id"),
        "category": ent.get("category"),
        "entityStatus": ent.get("status"),
        "leiStatus": reg.get("status"),
        "lastUpdate": reg.get("lastUpdateDate"),
        "legalAddress": _addr(ent.get("legalAddress")),
        "hqAddress": _addr(ent.get("headquartersAddress")),
    }


def _existing():
    rows = read_jsonl(OUT)
    return rows, {r["lei"] for r in rows}, {r["kvk"] for r in rows if r.get("kvk")}


def fetch_lookup(kvk_file=None):
    """Look up only the KVK numbers we need: one small API call each."""
    import csv, re
    wanted = [r["kvk"] for r in read_json(LANDING / "wikidata_revenue.json", [])
              if r.get("kvk") and not str(r["kvk"]).startswith("wd-")]       # 'wd-Q..' = no KvK number known yet
    wanted += [v["kvk"] for v in read_json(LANDING / "website_facts.json", {}).values() if v and v.get("kvk")]   # found on company websites
    wanted += [r["kvk"] for r in read_jsonl(LANDING / "kvk_profiles.jsonl") if r.get("kvk") and not r.get("notFound")]
    if kvk_file:
        with open(kvk_file, encoding="utf-8-sig") as f:
            wanted += [m for m in (norm_kvk(x) for x in re.findall(r"\d[\d ]{6,}\d", f.read())) if m]
    wanted = list(dict.fromkeys(k for k in wanted if k))
    if not wanted:
        sys.exit("Nothing to look up yet -- run fetch_wikidata.py (or fetch_kvk.py) first, or pass --kvk-file.")
    _, leis, have = _existing()
    misses_path = LANDING / "gleif_lookup_misses.json"
    misses = set(read_json(misses_path, []))
    todo = [k for k in wanted if k not in have and k not in misses]
    print(f"GLEIF lookup: {len(wanted):,} KVK numbers, {len(wanted) - len(todo):,} already known, "
          f"{len(todo):,} to look up (~{len(todo) * MIN_INTERVAL / 60:.0f} min)")
    found = failed = consecutive = 0
    hit_kvks = []          # KVK numbers GLEIF has answered for individually -> used to probe batch mode
    batch_ok = None        # None = not tested yet, True/False after the probe
    failures = set()

    def query(kvks):
        """One GLEIF call for one or more KVK numbers -> {kvk: [records]} (raises RuntimeError if GLEIF keeps failing)."""
        data = get(f"{API}/lei-records", params={"filter[entity.registeredAs]": ",".join(kvks),
                                                  "filter[entity.legalAddress.country]": "NL",
                                                  "page[size]": PAGE_SIZE if len(kvks) > 1 else 10})
        out = {k: [] for k in kvks}
        for r in (data or {}).get("data", []):
            h = slim(r)
            if h.get("kvk") in out:
                out[h["kvk"]].append(h)
        return out

    def record(kvk, hits):
        nonlocal found
        hits = [h for h in hits if h["lei"] not in leis]
        for h in hits:
            append_jsonl(OUT, h); leis.add(h["lei"]); found += 1
        if hits:
            hit_kvks.append(kvk)

    i = 0
    while i < len(todo):
        # batch mode: ~25 KVK numbers per call instead of 1 (GLEIF accepts comma-separated filter values)
        use_batch = batch_ok is True
        chunk = todo[i:i + (BATCH if use_batch else 1)]
        try:
            res = query(chunk)
            consecutive = 0
        except (RuntimeError, requests.RequestException) as e:
            if len(chunk) > 1:      # a failed batch: retry its members one by one so only the bad one is skipped
                print(f"  ! batch of {len(chunk)} failed ({e}); retrying one at a time", flush=True)
                res, bad_ones = {}, []
                for k in chunk:
                    try:
                        res.update(query([k]))
                    except (RuntimeError, requests.RequestException):
                        bad_ones.append(k)
                if len(bad_ones) < len(chunk):
                    failed += len(bad_ones); failures.update(bad_ones); consecutive = 0
                    for kvk in chunk:
                        if kvk in bad_ones:
                            continue
                        hits = res.get(kvk, [])
                        record(kvk, hits)
                        if not hits:
                            misses.add(kvk)
                    i += len(chunk)
                    continue
            failed += len(chunk); consecutive += 1
            failures.update(chunk)
            print(f"  ! GLEIF failed for {len(chunk)} KVK number(s), skipping ({e})", flush=True)
            i += len(chunk)
            if consecutive >= 15:
                write_json(misses_path, sorted(misses))
                print("  GLEIF has failed 15 calls in a row -- stopping the lookup here and keeping what was found. "
                      "Re-run later to continue.", flush=True)
                break
            continue
        for kvk in chunk:
            hits = res.get(kvk, [])
            record(kvk, hits)
            if not hits:
                misses.add(kvk)
        i += len(chunk)
        # probe: once 2 single lookups have hit, check that a batched call returns both before trusting batch mode
        if batch_ok is None and len(hit_kvks) >= 2:
            try:
                probe = query(hit_kvks[:2])
                batch_ok = all(probe.get(k) for k in hit_kvks[:2])
            except (RuntimeError, requests.RequestException):
                batch_ok = False
            print(f"  batch lookup {'ENABLED' if batch_ok else 'not available -- continuing one at a time'}", flush=True)
        if i % 25 < len(chunk) or i >= len(todo):
            write_json(misses_path, sorted(misses))
            print(f"  {min(i, len(todo)):,}/{len(todo):,}  ({found:,} LEIs found)", flush=True)
    if failed:
        print(f"  {failed:,} lookups failed and were skipped (they are retried on the next run).")
    write_json(misses_path, sorted(misses))
    print(f"Done: {found:,} new LEI records -> {OUT}  (companies without an LEI are normal: most SMEs have none)")


GOLDEN_COPY = "https://goldencopy.gleif.org/api/v2/golden-copies/publishes/lei2/latest.csv"


def fetch_bulk(url=GOLDEN_COPY):
    """GLEIF Golden Copy (full register, CSV, usually zipped) -> keep only Dutch records."""
    import csv, io, zipfile
    raw = LANDING / "gleif_goldencopy.download"
    if not raw.exists():
        print(f"Downloading GLEIF Golden Copy (worldwide, several hundred MB) ...\n  {url}")
        with requests.get(url, headers={"User-Agent": HEADERS["User-Agent"]}, stream=True, timeout=120, allow_redirects=True) as r:
            r.raise_for_status()
            tmp = raw.with_suffix(".part")
            done = 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk); done += len(chunk)
                    if done % (50 << 20) < (1 << 20):
                        print(f"  {done >> 20:,} MB", flush=True)
            tmp.replace(raw)
    else:
        print(f"Using the already downloaded {raw.name} (delete it to download a fresh copy)")

    with open(raw, "rb") as fh:
        is_zip = fh.read(2) == b"PK"
    if is_zip:
        zf = zipfile.ZipFile(raw)
        name = next(n for n in zf.namelist() if n.lower().endswith(".csv"))
        text = io.TextIOWrapper(zf.open(name), encoding="utf-8", newline="")
    else:
        text = open(raw, encoding="utf-8", newline="")
    reader = csv.DictReader(text)
    cols = reader.fieldnames or []

    def col(*names):
        for n in names:
            if n in cols:
                return n
        for n in names:                     # tolerate small naming differences between file versions
            hit = next((c for c in cols if c.lower().endswith(n.lower())), None)
            if hit:
                return hit
        return None
    C = {k: col(*v) for k, v in {
        "lei": ["LEI"], "name": ["Entity.LegalName"], "raid": ["Entity.RegistrationAuthority.RegistrationAuthorityID"],
        "raeid": ["Entity.RegistrationAuthority.RegistrationAuthorityEntityID"], "form": ["Entity.LegalForm.EntityLegalFormCode"],
        "cat": ["Entity.EntityCategory"], "status": ["Entity.EntityStatus"], "regstatus": ["Registration.RegistrationStatus"],
        "upd": ["Registration.LastUpdateDate"], "line1": ["Entity.LegalAddress.FirstAddressLine"],
        "pc": ["Entity.LegalAddress.PostalCode"], "city": ["Entity.LegalAddress.City"], "country": ["Entity.LegalAddress.Country"],
        "hqcity": ["Entity.HeadquartersAddress.City"], "hqpc": ["Entity.HeadquartersAddress.PostalCode"],
        "hqline1": ["Entity.HeadquartersAddress.FirstAddressLine"], "hqcountry": ["Entity.HeadquartersAddress.Country"],
        "other1": ["Entity.OtherEntityNames.OtherEntityName.1"],
    }.items()}
    missing = [k for k in ("lei", "name", "raid", "raeid", "country") if not C[k]]
    if missing:
        sys.exit(f"Unexpected Golden Copy columns (missing {missing}). First columns: {cols[:40]}")

    tmp_out = OUT.with_suffix(".bulk.tmp")
    n = nl = 0
    with open(tmp_out, "w", encoding="utf-8") as out:
        for row in reader:
            n += 1
            if n % 500_000 == 0:
                print(f"  scanned {n:,} records, {nl:,} Dutch", flush=True)
            if row.get(C["country"]) != "NL":
                continue
            g = lambda k: (row.get(C[k]) or None) if C[k] else None
            rec = {"lei": g("lei"), "name": g("name"), "otherNames": [x for x in [g("other1")] if x],
                   "kvk": norm_kvk(g("raeid")) if g("raid") == GLEIF_KVK_AUTHORITY else None,
                   "registeredAt": g("raid"), "registeredAs": g("raeid"), "legalForm": g("form"),
                   "category": g("cat"), "entityStatus": g("status"), "leiStatus": g("regstatus"), "lastUpdate": g("upd"),
                   "legalAddress": {"lines": [x for x in [g("line1")] if x], "postcode": g("pc"), "city": g("city"), "country": "NL"},
                   "hqAddress": {"lines": [x for x in [g("hqline1")] if x], "postcode": g("hqpc"), "city": g("hqcity"),
                                 "country": g("hqcountry")}}
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            nl += 1
    os.replace(tmp_out, OUT)
    print(f"Done: scanned {n:,} records worldwide, kept {nl:,} Dutch -> {OUT}")


def _parent(lei, kind):
    try:
        d = get(f"{API}/lei-records/{lei}/{kind}")
    except (RuntimeError, requests.RequestException) as e:
        print(f"  ! parent lookup skipped for {lei} ({e})", flush=True)
        return None
    if not d or not d.get("data"):
        return None
    s = slim(d["data"])
    return {"lei": s["lei"], "name": s["name"], "kvk": s["kvk"],
            "country": (s["legalAddress"] or {}).get("country")}


def fetch_parents():
    companies = read_json(LANDING / "companies.json", [])
    leis = sorted({c["lei"] for c in companies if c.get("lei")})
    done = read_json(PARENTS, {})
    todo = [l for l in leis if l not in done]
    print(f"GLEIF parents: {len(leis):,} companies with an LEI, {len(todo):,} still to look up "
          f"(~{len(todo) * 2 * MIN_INTERVAL / 60:.0f} min at GLEIF's rate limit)")
    for i, lei in enumerate(todo, 1):
        done[lei] = {"direct": _parent(lei, "direct-parent"), "ultimate": _parent(lei, "ultimate-parent")}
        if i % 25 == 0 or i == len(todo):
            write_json(PARENTS, done)
            print(f"  {i:,}/{len(todo):,}", flush=True)
    write_json(PARENTS, done)
    print(f"Done -> {PARENTS}. Re-run build_companies.py to attach them.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", nargs="?", default="lookup", choices=["lookup", "bulk", "parents"])
    ap.add_argument("--kvk-file", help="lookup mode: also look up the KVK numbers in this file")
    ap.add_argument("--url", default=GOLDEN_COPY, help="bulk mode: Golden Copy URL")
    a = ap.parse_args()
    try:
        if a.mode == "parents":
            fetch_parents()
        elif a.mode == "bulk":
            fetch_bulk(a.url)
        else:
            fetch_lookup(a.kvk_file)
    except requests.RequestException as e:
        print(f"GLEIF request failed: {e} -- continuing with what was fetched (LEI/parents are optional enrichment).")
