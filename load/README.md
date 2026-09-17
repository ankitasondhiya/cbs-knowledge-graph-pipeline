# Incremental Load Controller — Stage 3

Loads the validated RDF from stage 2 (`validated/*.nt` in R2) into Neo4j
Aura as a property graph. `run_load.py` is the only file here — it's a
direct translator from our own fixed RDF shape (the one `rdf_mapper.py`
produces) into Cypher, not a general RDF importer.

## Why not just import the RDF directly?

The plugin that reads RDF/N-Triples natively into Neo4j (neosemantics /
n10s) isn't installable on Aura — Aura only allows a curated plugin list,
and n10s isn't on it (it's an open feature request on Neo4j's own board,
not a shipped capability). So instead of a plugin, this script parses the
N-Triples ourselves and writes the equivalent property graph by hand.

## How a triple becomes graph structure

| RDF thing | Becomes in Neo4j |
|---|---|
| `rdf:type` triple | one or more node **labels** (e.g. `:Region:Gemeente`, `:Concept`, `:Observation`) |
| an object-valued predicate (`org:unitOf`, `qb:dataSet`, `exo:variable`, `exo:region`, `exo:branch`, ...) | a **relationship** (`PART_OF`, `IN_DATASET`, `HAS_VARIABLE`, `ABOUT_REGION`, `HAS_BRANCH`, ...) |
| a literal-valued predicate (`skos:prefLabel`, `rdf:value`, `exo:unit`, ...) | a **node property** |
| a language-tagged literal (`prefLabel@en` vs `prefLabel@nl`) | two separate properties (`prefLabelEn`, `prefLabelNl`) so neither overwrites the other |

Every node also gets a generic `:Entity` label with a uniqueness
constraint on `uri`. That's what makes this correct regardless of triple
order — N-Triples lines have no guaranteed order, so a node is often
first referenced as the *target* of a relationship before its own
`rdf:type` triple shows up later in the file. `MERGE (n:Entity {uri: ...})`
handles that safely either way.

The predicate tables at the top of `run_load.py` (`TYPE_LABELS`,
`PROP_PREDICATES`, `REL_PREDICATES`) are the single source of truth for
this mapping — deliberately hardcoded against `rdf_mapper.py`'s exact
vocabulary, not a generic converter. If `rdf_mapper.py` ever starts
emitting a new predicate, `--dry-run` will simply not classify it (see
Testing below) rather than fail silently in production.

## Batching, same lesson as stage 2

`run_transform.py` learned the hard way that building one big structure
in memory with zero progress output looks identical to "stuck" in a CI
log on the largest tables. This script processes each `.nt` file in
fixed-size **line** batches (5,000 triples at a time — N-Triples is one
self-contained triple per line, so this needs no knowledge of row
boundaries), prints a progress line every batch, and writes each batch in
its own Neo4j transaction.

## Run it

```bash
pip install -r requirements.txt

# 1. Dry run first -- parses + classifies every triple, prints operation
#    counts, and touches NEITHER Neo4j nor the R2 manifest. Safe to run
#    against real R2 data any time, including before Neo4j credentials exist.
python run_load.py --dry-run

# 2. Same, but against the 4 sample files here instead of R2 -- no
#    credentials needed at all:
python run_load.py --local --dry-run

# 3. The real thing, against R2 + your Aura instance:
export R2_ACCOUNT_ID=...
export R2_ACCESS_KEY_ID=...
export R2_SECRET_ACCESS_KEY=...
export R2_BUCKET_NAME=...
export NEO4J_URI=neo4j+s://xxxxxxxx.databases.neo4j.io   # from your Aura instance's "Connect" details
export NEO4J_USERNAME=neo4j
export NEO4J_PASSWORD=...                                 # set when the instance was created
python run_load.py
```

Reads from the `validated/` prefix in your bucket (only `.nt` files —
leftover `.ttl` files from before the batching fix are ignored). Tracks
what's already been loaded in `load/_processed_manifest.json` in the same
bucket, so reruns only pick up files landed since the last load — this is
the "incremental" part of Incremental Load Controller.

## Verified

Dry-run tested against all 4 real sample files in `sample_validated/`
(copied from `transform/validated_output/`, including the real 401-row
`81588NED` file — 116,516 triples). Every triple parsed successfully and
every predicate matched a known handler: 0 unparsed lines, 0 unknown
predicates, across all 4 files. Not yet tested against a live Aura
instance — that needs your real `NEO4J_URI`/`NEO4J_USERNAME`/`NEO4J_PASSWORD`,
which only you have.

## Exploring the graph afterward

A few Cypher queries to try in the Aura Query tab once data is loaded:

```cypher
// All measures for Amsterdam itself (the Gemeente-level node)
MATCH (r:Gemeente {regionCode: "GM0363"})<-[:ABOUT_REGION]-(o:Observation)-[:HAS_VARIABLE]->(c:Concept)
RETURN c.labelEn, o.value, o.period

// Buurt -> Wijk -> Gemeente hierarchy for one neighbourhood
MATCH path = (b:Buurt {regionCode: "BU03630000"})-[:PART_OF*]->(g:Gemeente)
RETURN path

// Which glossary concepts are still auto-fallback (verified = false)?
MATCH (c:Concept {verified: false}) RETURN c.labelEn, c.topic LIMIT 25
```

## Fitting a curated subset into Aura Free (400k relationship cap)

With 27 CBS tables landing, even `--scope poc` on every table will not fit
in Aura Free's hard 400,000-relationship ceiling — this was hit for real
(`Neo.ClientError.Transaction.TransactionHookFailed ... exceeded the
logical size limit of 400000 relationships`). Because each 5,000-triple
batch commits as its own transaction, a failed run like that still leaves
everything that succeeded *before* the failing batch permanently written —
the database needs a clean wipe before a curated re-load means anything.

**1. Wipe the database** (Aura console -> Query tab, or Neo4j Browser):

```cypher
MATCH (n) DETACH DELETE n
```

For a large graph this itself may need to run in batches
(`CALL apoc.periodic.iterate(...)`) if APOC is available on your Aura tier,
or just repeat the plain `DETACH DELETE` call a few times — Aura Free
has no APOC, so on Free, run the plain command repeatedly (or drop and
recreate the free instance from the Aura console, which is faster).

**2. Get real per-table relationship counts before choosing a subset.**
Never guess this — table row/measure counts vary wildly (see
`transform/scope_filters.py`'s notes on `85609NED`'s 145 measure columns).
Run a dry run against every scoped file and read the `rel_ops` in each
file's printed stats:

```bash
python run_load.py --dry-run --scope poc
# or via GitHub Actions: workflow_dispatch with scope=poc, dry_run=true,
# tables left blank -- then read the per-file stats in the job log.
```

**3. Pick tables whose combined `rel_ops` stays comfortably under 400k**
(leave headroom — Aura Free also caps nodes at 200k, and constraints/labels
add overhead), then load only those:

```bash
python run_load.py --scope poc --tables 81578NED,84765NED,82242NED,...
# or via GitHub Actions: workflow_dispatch with scope=poc,
# tables=<comma-separated list>, dry_run=false
```

The other tables stay fully landed and validated in R2 (`landing_zone/`,
`validated/`) — they're just not loaded into this particular Neo4j
instance. Nothing about fetch or transform changes; this is purely a
load-stage decision, and it's revisable any time (a paid Aura tier removes
the cap entirely, or a different `--tables` list can be loaded next).

## Set up as a GitHub Actions job

Add `NEO4J_URI`, `NEO4J_USERNAME`, `NEO4J_PASSWORD` as repo secrets
(alongside your existing `R2_*` ones), then a `load.yml` workflow that
runs `python load/run_load.py` after `transform.yml` completes — the same
`workflow_run` pattern `transform.yml` already uses to chain after
`fetch.yml`.
