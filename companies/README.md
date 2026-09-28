# Company layer: which companies sit behind the CBS KPIs

CBS says *how many* businesses per industry lack ERP/AI. It never names them.
This folder adds the actual companies, linked to the same CBS industry nodes,
so KPI 2 and KPI 3 can list "the businesses behind this number".

| Source | Gives | Cost | Key |
|---|---|---|---|
| GLEIF (LEI register) | legal name, **KVK number**, address, legal form, parent/group | free | none |
| KVK Basisprofiel | **SBI code** (-> CBS industry), **staff count**, trade names, address, website | EUR 6.40/month + EUR 0.02 per company | `KVK_API_KEY` |
| Wikidata | **exact revenue** + employees for larger companies | free | none |

Join key everywhere: the **KVK number**. Link to CBS: SBI code -> `:Branch`, staff -> size band, city -> `:Gemeente`.

## Run order (Windows PowerShell, from the CBS_Data folder)

```
pip install -r companies\requirements.txt
cd companies

# --- Step 1 (free): see the KVK record format with KVK's own test companies
python fetch_kvk.py --test
python build_companies.py --test          # -> landing_zone\companies\companies.test.json

# --- Step 2 (free): real named companies, no KVK needed
python fetch_wikidata.py                  # revenue, employees, industry (NACE) for larger companies (~1 min)
python fetch_gleif.py                     # LEI, legal name, address for exactly those companies
python build_companies.py --free          # merge -> landing_zone\companies\companies.json
python fetch_gleif.py parents             # group structure for those companies
python build_companies.py --free          # re-run to attach the parents

# optional: every Dutch LEI record (~200k) from GLEIF's free full file -- only needed
# for fetch_kvk.py --keywords (choosing companies by name before paying KVK)
python fetch_gleif.py bulk

$env:NEO4J_URI="neo4j+s://..."; $env:NEO4J_USERNAME="..."; $env:NEO4J_PASSWORD="..."
python load_companies.py --dry-run        # shows Aura Free usage before/after -- nothing written
python load_companies.py

# --- Step 3 (paid, later): real SBI + staff for a sample via the KVK API
$env:KVK_API_KEY="..."
python fetch_kvk.py --keywords hotel,restaurant,horeca,catering --max-calls 250    # ~EUR 5
python build_companies.py                 # KVK-based build (replaces the --free list)
python load_companies.py
```

Then open the dashboard, run KPI 2 or 3 and click an industry: the drill-down ends with
"Companies in this industry (10+ staff)".

## Buying KVK data (100+ employees only)
The KVK API only shows a company's staff count AFTER you pay for its profile, so for
"100+ employees only" order a **KVK selection file** (kvk.nl -> Handelsregister selecties offline):

- **Selection criteria:** werkzame personen totaal **100 or more** - hoofdvestiging only - active registrations
  (optionally: only the SBI sections you target). Roughly 8,600 Dutch businesses have 100+ staff.
- **Extra fields to request** (not in the standard delivery): SBI-code hoofdactiviteit + omschrijving,
  werkzame personen totaal, statutaire naam / handelsnaam, rechtsvorm, internetadres.
- The standard delivery already says whether the address may be used for marketing (Non-Mailing Indicator).

Then:
```
python import_kvk_selection.py path\to\kvk_selectie.csv     # .xlsx works too after: pip install openpyxl
python fetch_wikidata.py                                      # published revenue for the big names (free)
python fetch_gleif.py                                         # LEI + group structure (free)
python build_companies.py                                     # keeps 100+ staff; drops published revenue < EUR 50m
python fetch_gleif.py parents
python build_companies.py
python load_companies.py --dry-run
python load_companies.py
```
Companies marked "no marketing" by KVK are loaded but flagged in the dashboard -- don't use them for campaigns.

## Sales focus: revenue above EUR 50m
Only companies with revenue above EUR 50m are targeted.
- `build_companies.py` drops companies whose published revenue is below EUR 50m (`--min-revenue 0` to keep all).
- The dashboard lists only companies above EUR 50m: exact revenue where published, otherwise an estimate
  (staff x the industry's revenue per person working, CBS 81156NED), labelled "estimate".
- EUR 50m is also the Dutch legal line for 'large' companies, which must publish full annual accounts incl. revenue.
- For the paid KVK route, order/select companies with 100+ staff: below that, EUR 50m revenue is very rare
  in most industries, so you don't pay for profiles you'll discard.

## Good to know
- **Company list completeness.** GLEIF only has companies with an LEI (mostly larger ones and BV/NV/holdings).
  For a complete list of e.g. all Horeca businesses with 10+ staff, buy a KVK selection file
  (filter: SBI + number of employees) and pass it with `python fetch_kvk.py --kvk-file selection.csv`.
- **Revenue.** Exact only where published (Wikidata = large companies). Small companies don't have to publish revenue.
- **ERP / AI per company is not in any of these sources.** The CBS % is the likelihood for the industry.
- **Aura Free.** `load_companies.py` refuses to go past 90% of the 200k node / 400k relationship limit.
- `load_companies.py --test` flags test companies `testData=true`; remove them with `--remove-test`.
