"""
"Why now?" -- buying signals from job vacancies  ->  landing_zone/companies/signals.json (+ ERP evidence in erp.json)

A vacancy is the most honest public statement of what a company runs and what it is about to change:
  * "Functional consultant SAP S/4HANA migratie"  -> erp_change : an ERP project is under way / being staffed  (the buying moment)
  * "Finance controller, ervaring met AFAS"       -> erp_role   : they run AFAS (evidence for the ERP column)
Each signal keeps the vacancy title, date and link, so a seller can open it before calling.

  * "Data engineer / AI / automation role"        -> ai_data    : they invest in data & AI -- the other thing we sell

Two ways in (both optional, both keep only vacancies from companies that are in your companies.json):

  --jobs-csv FILE   Any vacancy export: company, title, [text], [url], [date], [kvk]
                    (Textkernel/Jobfeed, an Indeed/LinkedIn export you are licensed to use, a Google-Alerts sheet ...)
  --adzuna          Adzuna job-search API (free key from developer.adzuna.com; env ADZUNA_APP_ID, ADZUNA_APP_KEY).
                    Searches ERP-related terms in the Netherlands and matches employers to your companies by name.
                    NOTE: written from Adzuna's public API docs; not yet run against the live API.

Recruitment agencies and IT consultancies post vacancies FOR clients, so they are ignored (AGENCY list below).
We do not scrape LinkedIn or Indeed pages -- their terms forbid it. Use an export or an API.

    python signals_enrich.py --jobs-csv vacancies.csv
    python signals_enrich.py --adzuna --pages 3
"""
import argparse
import csv
import os
import re
import sys
from datetime import datetime, timezone

from common import LANDING, norm_kvk, read_json, write_json
from erp_catalog import CATALOG, lifecycle_of, mentions
from erp_enrich import merge_records
from name_match import build_index, fold, name_key

OUT = LANDING / "signals.json"
MAX_PER_COMPANY = 8

AGENCY = re.compile(r"randstad|tempo.?team|adecco|manpower|hays\b|brunel|yacht|michael page|page personnel|robert half|"
                    r"olympia|start people|covebo|maandag|uitzend|detachering|recruit|talent|capgemini|accenture|deloitte|"
                    r"kpmg|pwc\b|ey\b|cgi\b|sogeti|atos|ordina|cegeka|bearingpoint|avanade|qurius|ctac|sligro it", re.I)

CHANGE = re.compile(r"migrat|implement|selectie|selection|vervang|replace|transform|upgrade|go[- ]?live|roll[- ]?out|"
                    r"conversie|conversion|nieuw(?:e)? erp|erp[- ]programma|erp[- ]project|s/?4 ?hana (?:transit|migrat)", re.I)
# Hiring for data / AI / automation roles = the company is investing in exactly what our AI/data services support.
AI_DATA = re.compile(r"data[- ]?engineer|data[- ]?scientist|data[- ]?analyst|data platform|data governance|business intelligence|"
                     r"machine learning|\bai\b|kunstmatige intelligentie|\brpa\b|process automation|procesautomatisering|"
                     r"automation engineer|power bi|snowflake|databricks", re.I)
GENERIC_ERP = re.compile(r"\berp\b|enterprise resource planning|bedrijfssoftware|financieel systeem", re.I)

ADZUNA_QUERIES = ["ERP implementatie", "ERP migratie", "ERP selectie", "S/4HANA", "SAP ECC", "Dynamics 365 Business Central",
                  "Dynamics NAV", "AFAS", "Exact Online", "Exact Globe", "NetSuite", "Unit4", "Infor LN", "Baan",
                  "Oracle Fusion", "ERP key user", "functioneel beheerder ERP", "application manager ERP",
                  "data engineer", "data platform", "machine learning engineer", "process automation", "RPA developer",
                  "SAP consultant", "SAP beheerder", "SAP key user", "Business Central", "Microsoft Dynamics", "Navision",
                  "Exact Online consultant", "AFAS consultant", "financial systems analyst", "applicatiebeheerder ERP",
                  "ERP programmamanager", "ERP projectmanager", "business analist ERP"]


def classify(title, text):
    """Pure function: vacancy -> {type, erps:[names], evidence} or None if it says nothing about ERP."""
    blob = f"{title}\n{text or ''}"
    named = [(n, k, ev) for n, k, _f, ev in mentions(blob)]
    generic = bool(GENERIC_ERP.search(blob))
    if not named and not generic:
        if AI_DATA.search(title):      # no ERP mention, but a data / AI / automation role
            return {"type": "ai_data", "erps": [], "in_title": [], "evidence": "data / AI / automation role"}
        return None
    change = bool(CHANGE.search(blob)) and (generic or bool(named))
    return {"type": "erp_change" if change else "erp_role", "erps": [n for n, _k, _e in named],
            "in_title": [n for n, _k, _e in named if any(re.search(pat, title.lower()) for pat in CATALOG[n]["weak"])],
            "evidence": (named[0][2] if named else "mentions ERP")}


def match_company(name, kvk, index, by_kvk):
    if kvk and kvk in by_kvk:
        return kvk
    k = name_key(name or "")
    return index.get(k) if k else None


def process_vacancies(vacancies, companies):
    """[{company,title,text,url,date,kvk}] -> (signals {kvk:[...]}, erp_records [...])"""
    index = build_index(companies)
    by_kvk = {c["kvk"] for c in companies if c.get("kvk")}
    signals, erp_recs, seen = {}, [], set()
    for v in vacancies:
        if AGENCY.search(v.get("company") or ""):
            continue
        kvk = match_company(v.get("company"), norm_kvk(v.get("kvk")) if v.get("kvk") else None, index, by_kvk)
        c = classify(v.get("title") or "", v.get("text") or "")
        if not kvk or not c or (kvk, v.get("url") or v.get("title")) in seen:
            continue
        seen.add((kvk, v.get("url") or v.get("title")))
        label = {"erp_change": "ERP project / migration being staffed", "ai_data": "hiring for a data / AI / automation role"
                 }.get(c["type"], "ERP-related role")
        # "SAP ECC -> S/4HANA migration": the legacy system is what they run today, the other is the TARGET
        legacy = [n for n in c["erps"] if lifecycle_of(n) == "legacy"]
        migrating_from_legacy = c["type"] == "erp_change" and bool(legacy)
        shown = legacy[0] if migrating_from_legacy else (c["erps"][0] if c["erps"] else None)
        signals.setdefault(kvk, []).append({
            "type": c["type"], "label": label, "erp": shown, "title": v.get("title"),
            "url": v.get("url"), "date": v.get("date"), "evidence": c["evidence"]})
        for n in c["erps"]:
            target = migrating_from_legacy and n not in legacy
            erp_recs.append({"kvk": kvk, "erp": n, "vendor": CATALOG[n]["vendor"],
                             "confidence": "low" if target else ("medium" if n in c["in_title"] else "low"),
                             "source": "job vacancy (likely migration TARGET, not current)" if target else "job vacancy",
                             "evidence": f"{v.get('title')} -- {c['evidence']}", "evidenceUrl": v.get("url")})
    for k in signals:   # change signals first, newest first, capped
        signals[k].sort(key=lambda s: (s["type"] != "erp_change", -(int(re.sub(r"\D", "", s.get("date") or "0")[:8] or 0))))
        signals[k] = signals[k][:MAX_PER_COMPANY]
    return signals, erp_recs


def read_jobs_csv(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        sample = f.read(4096); f.seek(0)
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t") if sample else csv.excel
        return [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(f, dialect=dialect)]


def fetch_adzuna(pages):
    import requests
    app_id, app_key = os.environ.get("ADZUNA_APP_ID"), os.environ.get("ADZUNA_APP_KEY")
    if not (app_id and app_key):
        sys.exit("Set ADZUNA_APP_ID and ADZUNA_APP_KEY (free at developer.adzuna.com), or use --jobs-csv instead.")
    out = []
    for q in ADZUNA_QUERIES:
        for p in range(1, pages + 1):
            try:
                r = requests.get(f"https://api.adzuna.com/v1/api/jobs/nl/search/{p}", timeout=30,
                                 params={"app_id": app_id, "app_key": app_key, "what": q, "results_per_page": 50, "max_days_old": 180,
                                         "content-type": "application/json", "sort_by": "date"})
                r.raise_for_status()
                results = r.json().get("results", [])
            except Exception as e:
                print(f"  ! Adzuna '{q}' page {p} skipped ({e})", flush=True)
                break
            for j in results:
                out.append({"company": (j.get("company") or {}).get("display_name"), "title": j.get("title"),
                            "text": j.get("description"), "url": j.get("redirect_url"), "date": (j.get("created") or "")[:10]})
            if len(results) < 50:
                break
        print(f"  Adzuna '{q}': {len(out):,} vacancies so far", flush=True)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jobs-csv", help="vacancy export: company, title, [text], [url], [date], [kvk]")
    ap.add_argument("--adzuna", action="store_true", help="search the Adzuna API (needs ADZUNA_APP_ID / ADZUNA_APP_KEY)")
    ap.add_argument("--pages", type=int, default=5, help="Adzuna pages (50 vacancies each) per search term")
    ap.add_argument("--companies", default=str(LANDING / "companies.json"))
    a = ap.parse_args()
    if not a.jobs_csv and not a.adzuna:
        sys.exit("Nothing to do: pass --jobs-csv FILE and/or --adzuna.")
    companies = read_json(a.companies, [])
    if not companies:
        sys.exit("No companies.json yet -- run build_companies.py first.")

    vacancies = []
    if a.jobs_csv:
        vacancies += read_jobs_csv(a.jobs_csv)
        print(f"CSV: {len(vacancies):,} vacancies read")
    if a.adzuna:
        vacancies += fetch_adzuna(a.pages)

    signals, erp_recs = process_vacancies(vacancies, companies)
    old = read_json(OUT, {})
    for k, v in signals.items():    # keep earlier signals, newest evidence first, no duplicates
        have = {(s.get("url"), s.get("title")) for s in v}
        v += [s for s in old.get(k, []) if (s.get("url"), s.get("title")) not in have]
        old[k] = v[:MAX_PER_COMPANY]
    write_json(OUT, old)
    if erp_recs:
        merge_records(erp_recs)
    n_change = sum(1 for v in signals.values() if any(s["type"] == "erp_change" for s in v))
    print(f"{len(vacancies):,} vacancies checked -> ERP signals for {len(signals):,} of your companies "
          f"({n_change:,} with an ERP project / migration being staffed), {len(erp_recs):,} ERP evidence records -> {OUT}")


if __name__ == "__main__":
    main()
