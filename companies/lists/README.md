# Company lists (journals, rankings, associations, registers)

Drop any list of Dutch companies in this folder and the **CBS companies** workflow imports it (files starting with `_` are ignored):

* **CSV or Excel** exported from a journal ranking or an association's member page, or
* a plain **`.txt`** with one company per line (copy / paste from a ranking; a trailing number such as `Company BV   1.234 mln` is read as revenue).

The file name becomes the **source** shown in the dashboard ("listed in: Cobouw Top 100 2025 (#12)"), or add a `source` column.
Columns (any order, Dutch or English): `company|bedrijf|naam` (required), `revenue|omzet` (+ unit in the header, e.g. `omzet (mln)`),
`staff|medewerkers`, `city|plaats`, `website`, `industry|sector|branche` (an SBI letter A-U or text like "bouw", "energie", "logistiek"),
`rank|positie`, `year|jaar`, `source|bron`, `source_url`.

Where to find lists (free or with your subscriptions) -- always check the terms of use before copying:
* **Journals / rankings** -- business and trade media rank companies per industry (largest companies, fastest growers, top contractors, top logistics
  providers, top IT companies ...). Export or copy the ranking table you are licensed to use.
* **Associations** -- public member lists of industry associations (manufacturing, transport and logistics, construction, IT, energy, investment companies / private equity).
* **Registers** -- public registers of supervised financial institutions (banks, insurers, pension funds, payment institutions, investment firms) for finance.

Then run Actions -> **CBS companies**: names are looked up on Wikidata (exact Dutch match only), websites are scanned for KvK number / staff / address, and
every company keeps the list it came from. Companies with no staff or revenue anywhere cannot be sized and are reported in the log.
