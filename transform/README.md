# Transform & Validate — Stage 2

Turns raw CBS rows (landed in Azure Blob Storage by `fetch_cbs_tables.py`) into validated
RDF, ready for the Incremental Load Controller (stage 3). Generalized to
handle all 13 tables in the pipeline, not just one — each table's own
dimension structure (region, industry branch, country, period...) is
picked up automatically, since it was built by reading the real field
names out of your actual landed files.

| Box | File |
|---|---|
| RDF Mapping | `rdf_mapper.py` |
| Glossary Alignment | `glossary.py` |
| Geometry Join | not yet built — see "Not yet built" below |
| Validation Gate | `shacl_shapes.ttl` + `validate.py` |

`run_transform.py` is the orchestrator that ties them together.

## Place in the repo

Sits next to `fetch_cbs_tables.py` at the repo root — same repo, not split
out, since it's one pipeline with one deployment lifecycle:

```
cbs-knowledge-graph-pipeline/
  fetch_cbs_tables.py        # stage 1 (unchanged)
  pipeline_state.json
  landing_zone/              # stage 1 output, gitignored (lives in Azure Blob Storage)
  transform/                 # <- this folder (stage 2)
    glossary.py
    rdf_mapper.py
    shacl_shapes.ttl
    validate.py
    run_transform.py
    requirements.txt
    sample_landing/           # test fixtures: 3 real landed files + 1 synthetic (marked as such)
  .github/workflows/
    fetch.yml                 # stage 1 (unchanged)
```

## Run it

```bash
pip install -r transform/requirements.txt
cd transform

# Test locally first -- no Azure credentials needed, no cbs.nl network needed.
# sample_landing/ has 3 REAL landed files (85958NED, 81588NED, 83827NED) plus
# one synthetic 86165NED fixture (clearly marked -- real values not pulled yet
# for Amsterdam specifically since this dev environment can't reach cbs.nl):
python run_transform.py --local

# Against your real Azure Blob container -- reuses the exact same env vars
# fetch_cbs_tables.py / your GitHub Actions workflow already use:
export AZURE_STORAGE_CONNECTION_STRING=...   # from your storage account's "Access keys" page
export AZURE_STORAGE_CONTAINER=...
python run_transform.py

# --scope poc: same as above, but narrows the largest tables down to
# top-level industry sections and major trading partners (see
# scope_filters.py) so the result fits inside AuraDB Free's 200k node /
# 400k relationship cap. Writes to validated/*.poc.nt (a
# separate file and a separate manifest from the unscoped run, so neither
# run interferes with the other -- run this any time without touching
# your full-fidelity output):
python run_transform.py --scope poc
```

## POC scoping (`--scope poc`)

For a Neo4j AuraDB **Free**-tier demo, the full dataset doesn't fit --
`81578NED` alone has 163,520 rows, driven by CBS breaking industries down
to very fine sub-codes (`81589NED` has 1,487 distinct branch values, from
`01 Landbouw` down to `01131 Teelt van groenten in volle grond`). Rather
than drop whole tables, `--scope poc` narrows rows on the 5 tables that
actually have a volume problem (checked against real landed data, not
guessed):

| Table | Full rows | `--scope poc` rows | Kept |
|---|---|---|---|
| `81578NED` | 163,520 | 2,352 | top-level industry sections only |
| `83631NED` | 8,514 | 1,386 | top-level industry sections only |
| `83635NED` | 8,385 | 1,365 | top-level industry sections only |
| `84765NED` | 4,141 | 369 | major trading partners + total |
| `81589NED` | 1,487 | 21 | top-level industry sections only |

"Top-level industry section" means the 21 broadest SBI categories CBS
itself tags (`A Landbouw, bosbouw en visserij`, `G Handel`, `J Informatie
en communicatie`, ...) -- the same categories a business stakeholder
already thinks in, not an arbitrary cutoff. Every other table (including
`83827NED`, which shares the branch field but is only ever reported at a
finer level CBS never rolls up to a section, and would be silently wiped
out by a blanket top-level-only rule) passes through `--scope poc`
completely untouched.

The unscoped run is still the default and always produces the full,
faithful dataset -- `--scope poc` is purely an opt-in narrowing for a
demo that needs to fit a free database tier, not a replacement for it.

Reads raw JSON from the `landing_zone/` prefix in your container (exactly
where `fetch_cbs_tables.py` already writes) and produces:

- `validated/<name>.ttl` — RDF that passed the SHACL gate
- `rejected/<name>.report.txt` — anything that failed, with the exact reason
- `validated/_processed_manifest.json` — tracks what's already been done, so reruns are incremental

**No changes made to `fetch_cbs_tables.py` or `fetch.yml`** — stage 1 keeps
working exactly as it does today. To wire stage 2 into the same GitHub
Actions schedule, add a second job to `fetch.yml` (or a second workflow
file) that runs `python transform/run_transform.py` after the fetch job,
using the same repo secrets.

## How it handles 13 different table schemas generically

Every row is split into dimension fields (things that classify the row —
region, industry branch, country, period) and measure fields (actual
numbers). `glossary.py`'s `DIMENSION_FIELDS` dict says which raw CBS
column names are dimensions and what kind; everything else numeric is
treated as a measure and gets its own `qb:Observation`.

Two important, deliberate distinctions the mapper makes:

- **`WijkenEnBuurten` vs `RegioS`**: `WijkenEnBuurten` (86165NED only)
  carries real CBS municipal codes (`GM`/`WK`/`BU`), and the
  Buurt → Wijk → Gemeente hierarchy is derived automatically from the
  code structure. `RegioS` (on the business tables) is a *different*,
  unrelated classification — a national/foreign/unclassified breakdown,
  not a municipality. They are kept as separate node types on purpose;
  merging them would misrepresent the data.
- **Curated vs. auto-fallback glossary entries**: a handful of headline
  variables per table have real, curated labels and definitions in
  `glossary.py` (confirmed against your actual landed field names). Every
  other measure field still gets mapped — with a human-readable label
  auto-derived from its CBS field name — but is tagged `verified: false`
  and reported by `run_transform.py` after every run, e.g.:
  `NOTE: 30 concept(s) used auto-fallback labels...`. Nothing is silently
  dropped; you always know exactly what's left to curate.

## Verified against real data, not just synthetic fixtures

Tested in-session against three of your actual landed files
(`85958NED`, `81588NED`, `83827NED` — international trade, business size
classes, wholesale turnover) plus one synthetic Amsterdam fixture for
86165NED (real schema, fake values, since this environment can't reach
cbs.nl to pull actual Amsterdam rows yet). Confirmed:

- Real wholesale-turnover observations came out correctly linked to their
  industry branch and quarter, with no `exo:region` (that table genuinely
  has none) — and the SHACL gate still passed them, because the region
  requirement is now "at most one", not "exactly one".
- A deliberately corrupted observation (missing value) is still caught by
  the Validation Gate with a precise violation message.
- The 86165NED synthetic sample still produces the correct
  Buurt → Wijk → Gemeente hierarchy via `org:unitOf`/`org:hasUnit`.

## Not yet built: Geometry Join

Attaching PDOK boundary polygons via GeoSPARQL needs a second live network
source (PDOK's WFS service) this environment can't reach either. It only
applies to the 86165NED region hierarchy anyway (the business tables have
no geometry to attach) — a self-contained `geometry_join.py` that adds
`geo:hasGeometry` triples onto the region URIs `rdf_mapper.py` already
mints, whenever you're ready to build it.

## Extending the glossary

Run `glossary.unmapped_fields(table_id, row)` against a real row any time
to see exactly which measure fields for that table are still running on
auto-fallback labels. Curate the ones that matter most to the business
first — you don't need all ~40 size-class buckets in `81588NED` labeled
by hand before this is useful; the fallback keeps them queryable in the
meantime.
