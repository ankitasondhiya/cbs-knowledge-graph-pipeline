# Ordering the target-company list from KVK (copy / send this)

**Why.** CBS says *how many* businesses per industry lack ERP / AI -- it never names companies. The names come from the Dutch Chamber of
Commerce (KVK) register. A KVK **selection file** delivers the complete, filtered list in one go (no per-company cost for companies we would discard).

**Order (KVK -> Handelsregister -> selecties offline / "bestandsselectie")**

| Item | Value |
|---|---|
| Registrations | active, **hoofdvestiging** (main establishment) only, rechtspersonen + (optionally) eenmanszaken excluded |
| Size | **werkzame personen totaal >= 100** (about 8,600 businesses in the Netherlands) |
| Industries | all SBI sections (or only the ones we sell into) |
| Extra fields beyond the standard delivery | **SBI-code hoofdactiviteit** (+ omschrijving), **werkzame personen totaal**, statutaire naam / handelsnaam, rechtsvorm, **internetadres** (website) |
| Marketing flag | keep the **non-mailing indicator** (companies that opted out of marketing) |
| Format | CSV or Excel |

Ask KVK for the current price and licence terms (permitted use, whether it may be stored in a private database / repository).

**Load it**
1. Save the file as `companies/kvk_selection.csv` (or `.xlsx`) in the repository **only if the licence allows storing it in a private repo**; otherwise run locally:
   `python import_kvk_selection.py path/to/file.csv` then `python build_companies.py` and `python load_companies.py`.
2. Run Actions -> **CBS companies** (dry run first). The workflow detects the file and builds the company list from it
   (instead of the free Wikidata sample), and still adds exact revenue from Wikidata where it exists.
3. Open the dashboard -> KPI 10. Every 100+ staff company appears with its CBS industry, staff and (estimated or exact) revenue.

**What KVK does NOT give:** revenue (exact revenue stays a Wikidata / annual-accounts matter -- the dashboard estimates it from staff x industry
revenue per worker and labels it "estimate") and the ERP in use (see the ERP steps in README).
Companies flagged "no marketing" are loaded but must not be used for campaigns.
