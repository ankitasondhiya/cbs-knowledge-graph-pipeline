"""
KVK selection file (Handelsregister 'selecties offline') -> landing_zone/companies/kvk_profiles.jsonl

The paid KVK selection file is filtered by KVK BEFORE delivery (e.g. 100+
working persons), so there's no per-company API cost for companies you'd
throw away. This turns the delivered CSV/Excel into the same records
fetch_kvk.py produces, so build_companies.py / load_companies.py work unchanged.

Order the selection with (see README):
  criteria : werkzame personen totaal >= 100, hoofdvestiging, active, (optionally) SBI sections
  fields   : standard + extra rubrieken: SBI-code hoofdactiviteit (+ omschrijving), werkzame personen
             totaal, statutaire naam / handelsnaam, rechtsvorm, internetadres

Column names are matched loosely (case, spaces, dashes ignored), because the
exact headers KVK uses can differ per delivery; unmatched columns are listed.

    python import_kvk_selection.py path\\to\\kvk_selectie.csv      (or .xlsx)
    python import_kvk_selection.py selectie.csv --min-staff 100      # default 100
"""
import argparse
import csv
import re
import sys
from datetime import datetime, timezone

from common import LANDING, norm_kvk, read_jsonl, write_json

OUT = LANDING / "kvk_profiles.jsonl"

# normalised header -> our field; first match wins, so most specific first
FIELDS = {
    "kvk": ["kvknummer", "kvknr", "kvk", "dossiernummer"],
    "vestiging": ["vestigingsnummer"],
    "name": ["statutairenaam", "vennootschapsnaam", "naam", "eerstehandelsnaam", "handelsnaam", "verkortenaam"],
    "sbi": ["sbicodehoofdactiviteit", "hoofdactiviteitsbicode", "sbihoofdactiviteit", "hoofdactiviteit", "sbicode", "sbi"],
    "sbiDesc": ["omschrijvinghoofdactiviteit", "sbiomschrijvinghoofdactiviteit", "sbiomschrijving", "omschrijvingsbi"],
    "staff": ["werkzamepersonentotaal", "totaalwerkzamepersonen", "aantalwerkzamepersonen", "werkzamepersonen"],
    "street": ["bezoekadresstraatnaam", "bezoekadresstraat", "straatnaambezoekadres", "straatnaam", "straat"],
    "number": ["bezoekadreshuisnummer", "huisnummerbezoekadres", "huisnummer"],
    "postcode": ["bezoekadrespostcode", "postcodebezoekadres", "postcode"],
    "city": ["bezoekadresplaats", "plaatsbezoekadres", "bezoekadreswoonplaats", "plaats", "woonplaats"],
    "gemeente": ["bezoekadresgemeentenaam", "gemeentenaam", "gemeente"],
    "legalForm": ["rechtsvorm", "rechtsvormomschrijving"],
    "website": ["internetadres", "website", "url"],
    "noMarketing": ["nonmailingindicator", "nmi", "reclamepost", "reclame", "verkoopaandedeur"],
}


def norm(h):
    return re.sub(r"[^a-z0-9]", "", (h or "").lower())


def read_rows(path):
    if path.lower().endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError:
            sys.exit("Excel file: run  pip install openpyxl  (or save the file as CSV).")
        ws = openpyxl.load_workbook(path, read_only=True, data_only=True).active
        it = ws.iter_rows(values_only=True)
        headers = [str(h or "") for h in next(it)]
        return headers, [dict(zip(headers, r)) for r in it]
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        sample = f.read(8192)
        f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        r = csv.DictReader(f, dialect=dialect)
        return r.fieldnames, list(r)


def pick(headers):
    nh = {norm(h): h for h in headers}
    mapping, used = {}, set()
    for field, cands in FIELDS.items():
        for c in cands:
            hit = nh.get(c) or next((h for n, h in nh.items() if c in n and h not in used), None)
            if hit and hit not in used:
                mapping[field] = hit
                used.add(hit)
                break
    return mapping, [h for h in headers if h not in used]


def first_int(v):
    """'250' -> 250; '100 t/m 199' -> 100 (lower bound of a class); '' -> None"""
    m = re.search(r"\d+", str(v or "").replace(".", ""))
    return int(m.group()) if m else None


def yes(v):
    s = str(v or "").strip().lower()
    return s in ("ja", "j", "yes", "y", "true", "1", "x")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--min-staff", type=int, default=100)
    a = ap.parse_args()

    headers, rows = read_rows(a.file)
    m, unused = pick(headers)
    print("Column mapping:")
    for k, h in m.items():
        print(f"  {k:12} <- {h}")
    if unused:
        print(f"  (not used: {', '.join(unused[:20])}{' ...' if len(unused) > 20 else ''})")
    for must in ("kvk", "sbi", "staff"):
        if must not in m:
            sys.exit(f"\nNo column found for '{must}'. Headers in the file: {headers}\n"
                     "Rename that column or tell Claude the header name.")
    nmi_col = m.get("noMarketing")
    nmi_note = ""
    if nmi_col and ("reclame" in norm(nmi_col) or "verkoop" in norm(nmi_col)):
        # KVK's field says whether the address MAY be used for marketing -> invert
        nmi_note = " (column says 'may be used for marketing'; inverted to noMarketing)"

    existing = {r["kvk"]: r for r in read_jsonl(OUT) if r.get("kvk")}
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    kept = small = bad = 0
    seen = set()
    for r in rows:
        g = lambda k: (r.get(m[k]) if k in m else None)
        kvk = norm_kvk(g("kvk"))
        if not kvk or kvk in seen:
            bad += 0 if kvk else 1
            continue
        seen.add(kvk)
        staff = first_int(g("staff"))
        if staff is None or staff < a.min_staff:
            small += 1
            continue
        sbi = re.sub(r"\D", "", str(g("sbi") or "")) or None
        no_mkt = None
        if nmi_col:
            no_mkt = (not yes(g("noMarketing"))) if nmi_note else yes(g("noMarketing"))
        prev = existing.get(kvk, {})
        existing[kvk] = {
            "kvk": kvk,
            "name": (str(g("name")).strip() if g("name") else None) or prev.get("name"),
            "tradeNames": prev.get("tradeNames", []),
            "sbi": [{"code": sbi, "desc": g("sbiDesc"), "main": True}] if sbi else prev.get("sbi", []),
            "mainSbi": sbi or prev.get("mainSbi"),
            "mainSbiDesc": g("sbiDesc") or prev.get("mainSbiDesc"),
            "staff": staff,
            "legalForm": g("legalForm") or prev.get("legalForm"),
            "address": {"street": g("street"), "number": g("number"), "postcode": g("postcode"),
                        "city": g("city") or g("gemeente")},
            "websites": [g("website")] if g("website") else prev.get("websites", []),
            "noMarketing": no_mkt,
            "source": "kvk-selection",
            "fetchedAt": now,
        }
        kept += 1

    with open(OUT, "w", encoding="utf-8") as f:
        import json
        for rec in existing.values():
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\n{kept:,} companies with {a.min_staff}+ staff imported{nmi_note}; "
          f"{small:,} below {a.min_staff} staff skipped; {bad:,} rows without KVK number.")
    print(f"-> {OUT}\nNext: python build_companies.py  (then fetch_gleif.py, load_companies.py)")


if __name__ == "__main__":
    main()
