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


---

# How to get KVK access (API key and sample data)

**Two different products -- pick by goal**

| | KVK **API** (developers.kvk.nl) | KVK **selection file** (bestandsselectie) |
|---|---|---|
| What it is | look up companies one by one | one delivered file with every company matching your filter |
| Finds companies by size? | **No** -- you must already know the company; Zoeken (search) cannot filter on staff or industry | **Yes** -- e.g. "100+ working persons, active, main establishment" |
| Gives | name, KVK no., official industry (SBI), staff, address, website, legal form | the same, for the whole filtered list |
| Cost (verify current prices) | about EUR 6.40/month per key + EUR 0.02 per profile (our notes) | quoted by KVK |
| Best for | enriching companies we already have (fills missing industry / staff / address) | **building the full target list** |

**Getting a free sample (no key, no cost):** `python fetch_kvk.py --test` uses KVK's *test environment* (fictitious companies, public test key built in)
so you can see the exact record format before paying.

**Getting a real API key**
1. Go to **developers.kvk.nl** (KVK Developer Portal) and create an account (a company login may be required -- check the portal's eligibility notes).
2. Subscribe to the **Basisprofiel** API (and **Zoeken** if you want name search) and accept the terms / contract; KVK then issues the **API key**.
3. Store it as the GitHub repository secret **`KVK_API_KEY`** (Settings -> Secrets and variables -> Actions) -- never in a file.
4. Run Actions -> **CBS companies** with **use_kvk_api = true** and a small **kvk_max_calls** first (e.g. 50 = about EUR 1), dry run on. The workflow looks up
   the companies already in the list, adds official industry, staff and address, then rebuilds the list with real staff counts (companies under 100 staff drop out).

**Selection file:** order via kvk.nl -> *Handelsregister* -> *selecties / bestandsselectie* using the table at the top of this page.

## Your target list, field by field (be realistic about sources)

| What sales wants | Source | Status |
|---|---|---|
| Company name, KVK number, legal form | KVK (file or API) | covered |
| **Address** (street, postcode, city), website | KVK (file or API) | covered |
| Industry (SBI -> CBS industry) | KVK | covered |
| **Size** (people working) | KVK | covered |
| **Revenue** | **not in KVK.** Exact only where published (Wikidata; annual accounts via a paid provider such as Company.info / Graydon); otherwise an estimate: staff x industry revenue per worker (CBS) | partial |
| **Which ERP** | not in any register: your team's knowledge, vendor customer pages, job vacancies, a technographics provider | partial |
| **AI / automation signals** | job vacancies (data / AI / automation roles), website mentions | partial |
| Pitch angle | CBS industry gaps (ERP, AI) + the signals above -> score and next-step text in KPI 10 | covered |


---

# What the KVK API agreement and terms of use mean for us (read before the first production call)

Source: KVK's email of 1 Oct 2026, the *KVK API Agreement v2.2*, the *Description of Service Levels* and the *Gebruiksvoorwaarden* (terms of use). Not legal advice -- have the signatory / legal read them.

**Process (from KVK's email)**
1. An authorised signatory signs the agreement (wet signature or DocuSign; two signatures if the company has joint representation) and returns it to **account@kvk.nl**.
2. In the same email state **what the API is used for, how many queries are expected, and a 06 mobile number**.
3. Within 3 working days: an email with a login for the *Business Register* portal, where the **API costs are visible**.
4. Within 5 working days: an email with the **API key**.

**Cost safety (KVK warns explicitly: programming mistakes -> very high bills that are NOT credited)**
- Test environment is free; **production calls -- also accidental or "test" ones -- are billed** at KVK's published tariffs (kvk.nl/tarieven).
- Our safeguards: a hard cap (`kvk_max_calls`, default 20), a restored cache so a company is never paid for twice, manual runs only (never schedule it).
- Do this: first production run with **20** calls, check the cost in the portal, then raise slowly. Never put the key anywhere except the repository secret.
- The agreement lets you name a person who can view spending -- do that.

**What the API cannot do (KVK says so itself):** it cannot select companies by SBI code or size; it only looks up by company name or KVK number.
For target-group lists use the **selection file** (bestandsselectie).

**Terms of use that matter for a sales list**
- **Own use is allowed** (supporting your own internal work processes, e.g. our sales team prospecting).
- **No passing on** ("Doorgifte"): KVK data may not be handed or sold to third parties, and a company may not serve several clients from its own collection of KVK data. -> Do not give customers the KPI 10 list or CSV, and do not expose it in a customer-facing product, without KVK's agreement.
- **If KVK-based information is offered to third parties** (re-use), it must show the retrieval date and the sentence *"Voor actuele informatie met juridische derdenwerking dient u altijd het Handelsregister te raadplegen."* and must not use the KVK logo.
- **Non-mailing indicator:** where active, no direct marketing by post / door-to-door (KVK may cancel the contract). We load the flag and show "no marketing"; the safe rule is to exclude those companies from campaigns.
- **Personal data:** sole proprietorships (eenmanszaken), officers, contact details of natural persons need a separate request to re-use and must also satisfy GDPR / Telecommunicatiewet. -> Our target list is 100+ staff companies; exclude sole proprietors and do not store officer names.
- **No scraping** of the register; the API key is **personal -- never share it**; rotate it periodically.
- KVK may **audit** compliance, and may cancel with notice.
- Contract has no fixed term; cancel in writing (effective at the end of the following calendar month).
