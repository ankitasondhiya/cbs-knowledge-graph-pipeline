"""
Incremental Load Controller (stage 3): loads validated RDF (N-Triples,
produced by transform/run_transform.py) into Neo4j Aura as a property
graph.

Why this file exists at all: everything upstream (rdf_mapper.py,
shacl_shapes.ttl) was built for a triple store queried with SPARQL.
Neo4j is a property graph queried with Cypher, and the plugin that would
read RDF natively (neosemantics/n10s) isn't installable on Aura -- Aura
only allows a curated plugin list, and n10s isn't on it. So this script
is a direct, hand-written translator from our OWN fixed, known RDF shape
(the one rdf_mapper.py produces -- not arbitrary RDF) into Cypher. It is
NOT a general-purpose RDF-to-property-graph converter; the predicate
tables below are deliberately hardcoded against rdf_mapper.py's exact
vocabulary.

How a triple becomes graph structure:
  - rdf:type triples decide which Neo4j label(s) a node gets (Region +
    Gemeente/Wijk/Buurt, Concept, Dataset, Observation, DimensionValue +
    its specific kind e.g. Branch/Country).
  - Object-valued predicates (org:unitOf, qb:dataSet, exo:variable,
    exo:region, exo:branch, ...) become relationships, with a fixed
    Cypher relationship type per predicate (relationship types can't be
    parameterized in Cypher, so each one gets its own small UNWIND).
  - Literal-valued predicates (skos:prefLabel, rdf:value, exo:unit, ...)
    become node properties. A language-tagged literal (prefLabel@en vs
    prefLabel@nl) gets a language suffix on the property key so both
    survive as separate properties.
  - Every node also gets a generic :Entity label with a uniqueness
    constraint on `uri` -- this is what lets MERGE work correctly even
    when a node is first referenced (as the object of a relationship)
    before its own rdf:type triple is seen later in the stream, which
    happens routinely since N-Triples lines have no guaranteed order.

Processes each .nt file in fixed-size LINE batches (not row batches --
N-Triples has exactly one triple per line, fully self-contained, so
batching by line count needs no knowledge of row boundaries) to keep
memory bounded and progress visible on the largest tables, mirroring the
same lesson learned in transform/run_transform.py.

Usage:
    python run_load.py                 # load new files from Azure Blob validated/ into Neo4j
    python run_load.py --dry-run       # parse + classify triples, print stats, touch NO database
    python run_load.py --local         # same as above but reads ./sample_validated/*.nt instead of Azure
    pip install -r requirements.txt

Env vars (same Azure vars fetch_cbs_tables.py / run_transform.py already
use, plus three Neo4j ones -- from your Aura instance's connection details):
    AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_CONTAINER
    NEO4J_URI          e.g. neo4j+s://xxxxxxxx.databases.neo4j.io
    NEO4J_USERNAME      usually "neo4j"
    NEO4J_PASSWORD      set when the Aura instance was created
"""

import argparse
import json
import os
import re
import sys
import time

VALIDATED_PREFIX = "validated/"
LOAD_MANIFEST_KEY = "load/_processed_manifest.json"

BATCH_LINES = 5000  # triples per batch -- bounded memory, visible progress on huge files

# Aura Free's HARD cap is exactly 400,000 relationships -- this is set a
# little under it (not at it) so the budget check below stops BEFORE the
# wall, not after failing against it. Load repeatedly hit the real cap
# mid-file and crashed the whole run, losing an otherwise-successful load
# of everything smaller (2026-09-24) -- this makes the load self-limiting
# instead: it now always finishes successfully with whatever fits, skips
# (not crashes on) anything that wouldn't, and reports exactly what got
# skipped so it's obvious what's missing and why. Raise this once you're
# on a paid Aura tier -- it's not a real graph-size limit, just this
# tier's ceiling with headroom.
MAX_RELATIONSHIPS = 395000

# ---------------------------------------------------------------------------
# Our fixed RDF vocabulary -> Cypher shape. See rdf_mapper.py for the source
# of truth these tables mirror.

QB = "http://purl.org/linked-data/cube#"
ORG = "http://www.w3.org/ns/org#"
EX = "https://data.cbs-knowledge-graph.example/id/"
EXO = "https://data.cbs-knowledge-graph.example/ontology/"
SKOS = "http://www.w3.org/2004/02/skos/core#"
PROV = "http://www.w3.org/ns/prov#"
RDF_NS = "http://www.w3.org/1999/02/22-rdf-syntax-ns#"
RDFS = "http://www.w3.org/2000/01/rdf-schema#"

TYPE_LABELS = {
    EXO + "Gemeente": ("Region", "Gemeente"),
    EXO + "Wijk": ("Region", "Wijk"),
    EXO + "Buurt": ("Region", "Buurt"),
    EXO + "Region": ("Region",),
    EXO + "Provincie": ("Region", "Provincie"),
    EXO + "CoropGebied": ("Region", "CoropGebied"),
    EXO + "Landsdeel": ("Region", "Landsdeel"),
    QB + "DataSet": ("Dataset",),
    QB + "Observation": ("Observation",),
    SKOS + "Concept": ("Concept",),
    EXO + "Branch": ("DimensionValue", "Branch"),
    EXO + "RegioCategory": ("DimensionValue", "RegioCategory"),
    EXO + "ServiceCategory": ("DimensionValue", "ServiceCategory"),
    EXO + "Country": ("DimensionValue", "Country"),
    EXO + "TradeDirection": ("DimensionValue", "TradeDirection"),
    EXO + "OtherDimension": ("DimensionValue", "OtherDimension"),
}

PROP_PREDICATES = {
    SKOS + "prefLabel": "prefLabel",
    EXO + "regionCode": "regionCode",
    RDFS + "label": "label",
    SKOS + "definition": "definition",
    EXO + "unit": "unit",
    EXO + "topic": "topic",
    EXO + "verified": "verified",
    RDF_NS + "value": "value",
    PROV + "wasDerivedFrom": "sourceTable",
    PROV + "generatedAtTime": "generatedAt",
    EXO + "sourcePulledAt": "sourcePulledAt",
    EXO + "period": "period",
    # New: any DIMENSION_FIELDS entry in glossary.py marked literal:True
    # lands here as a plain Observation property (see rdf_mapper.py's
    # generic literal_props handling). Marges/Seizoencorrectie are the
    # first two beyond Perioden -- raw CBS values, not yet boolean-
    # verified (see glossary.py's comment on both). Add more here any
    # time glossary.py grows another literal-flagged field.
    EXO + "marginType": "marginType",
    EXO + "seasonalAdjustment": "seasonalAdjustment",
    EXO + "dimensionKey": "dimensionKey",
}

REL_PREDICATES = {
    ORG + "unitOf": "PART_OF",
    QB + "dataSet": "IN_DATASET",
    EXO + "variable": "HAS_VARIABLE",
    EXO + "region": "ABOUT_REGION",
    EXO + "branch": "HAS_BRANCH",
    EXO + "regioCategory": "HAS_REGIO_CATEGORY",
    EXO + "otherDimension": "HAS_DIMENSION",
    EXO + "serviceCategory": "HAS_SERVICE_CATEGORY",
    EXO + "country": "HAS_COUNTRY",
    EXO + "tradeDirection": "HAS_TRADE_DIRECTION",
}

RDF_TYPE = RDF_NS + "type"
SKIP_PREDICATES = {ORG + "hasUnit"}  # redundant inverse of org:unitOf -- PART_OF already covers it


# ---------------------------------------------------------------------------
# Minimal streaming N-Triples line parser (deliberately hand-rolled instead
# of loading the whole file into an rdflib Graph -- for the largest tables
# that would mean holding millions of triple objects in memory at once,
# the exact mistake run_transform.py's rewrite moved away from).

TRIPLE_RE = re.compile(r"^(<[^>]*>|_:\S+)\s+(<[^>]*>)\s+(.*)\s\.\s*$")
LITERAL_RE = re.compile(r'^"((?:[^"\\]|\\.)*)"(?:\^\^<([^>]*)>|@([a-zA-Z-]+))?$')


def _unescape(s: str) -> str:
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), s)
    s = re.sub(r"\\U([0-9a-fA-F]{8})", lambda m: chr(int(m.group(1), 16)), s)
    s = s.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").replace("\\r", "\r")
    s = s.replace("\\\\", "\\")
    return s


def parse_line(line: str):
    """Returns (subject, predicate, obj_value, obj_is_uri, lang) or None if the line is blank/unparseable."""
    line = line.strip()
    if not line:
        return None
    m = TRIPLE_RE.match(line)
    if not m:
        return None
    s_raw, p_raw, o_raw = m.groups()
    s = s_raw[1:-1] if s_raw.startswith("<") else s_raw
    p = p_raw[1:-1]
    o_raw = o_raw.strip()
    if o_raw.startswith("<"):
        return s, p, o_raw[1:-1], True, None
    lm = LITERAL_RE.match(o_raw)
    if not lm:
        return None
    val = _unescape(lm.group(1))
    lang = lm.group(3)
    return s, p, val, False, lang


def _coerce(prop_key: str, value: str):
    if prop_key == "value":
        return float(value)
    if prop_key == "verified":
        return value.lower() == "true"
    return value


# ---------------------------------------------------------------------------
# Cypher (batched via UNWIND -- one write transaction per batch, a handful
# of statements per transaction rather than one per triple)

CONSTRAINT_CYPHER = "CREATE CONSTRAINT entity_uri IF NOT EXISTS FOR (n:Entity) REQUIRE n.uri IS UNIQUE"

MERGE_ENTITY = "UNWIND $uris AS u MERGE (n:Entity {uri: u})"

SET_LABELS_TEMPLATE = "UNWIND $uris AS u MATCH (n:Entity {{uri: u}}) SET n:{labels}"

SET_PROPS = (
    "UNWIND $rows AS row "
    "MATCH (n:Entity {uri: row.uri}) "
    "SET n[row.key] = row.value"
)

REL_TEMPLATE = (
    "UNWIND $rows AS row "
    "MATCH (a:Entity {{uri: row.from}}), (b:Entity {{uri: row.to}}) "
    "MERGE (a)-[:{rel_type}]->(b)"
)


class BatchAccumulator:
    def __init__(self):
        self.entity_uris = set()
        self.label_ops = {}  # labels-tuple -> set of uris
        self.prop_rows = []  # {uri, key, value}
        self.rel_rows = {}   # rel_type -> list of {from, to}

    def add_entity(self, uri):
        self.entity_uris.add(uri)

    def add_type(self, uri, class_iri):
        labels = TYPE_LABELS.get(class_iri)
        if labels is None:
            return
        self.add_entity(uri)
        self.label_ops.setdefault(labels, set()).add(uri)

    def add_prop(self, uri, base_key, value, lang):
        self.add_entity(uri)
        key = base_key
        if lang:
            key = base_key + lang.capitalize()
        self.prop_rows.append({"uri": uri, "key": key, "value": _coerce(base_key, value)})

    def add_rel(self, from_uri, rel_type, to_uri):
        self.add_entity(from_uri)
        self.add_entity(to_uri)
        self.rel_rows.setdefault(rel_type, []).append({"from": from_uri, "to": to_uri})

    def stats(self):
        return {
            "entities": len(self.entity_uris),
            "label_ops": sum(len(v) for v in self.label_ops.values()),
            "prop_ops": len(self.prop_rows),
            "rel_ops": sum(len(v) for v in self.rel_rows.values()),
        }

    def flush(self, tx):
        if self.entity_uris:
            tx.run(MERGE_ENTITY, uris=list(self.entity_uris))
        for labels, uris in self.label_ops.items():
            cypher = SET_LABELS_TEMPLATE.format(labels=":".join(labels))
            tx.run(cypher, uris=list(uris))
        if self.prop_rows:
            tx.run(SET_PROPS, rows=self.prop_rows)
        for rel_type, rows in self.rel_rows.items():
            cypher = REL_TEMPLATE.format(rel_type=rel_type)
            tx.run(cypher, rows=rows)


def process_lines(lines, acc: BatchAccumulator):
    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            continue
        s, p, o, o_is_uri, lang = parsed
        if p in SKIP_PREDICATES:
            continue
        if p == RDF_TYPE:
            acc.add_type(s, o)
        elif p in REL_PREDICATES and o_is_uri:
            acc.add_rel(s, REL_PREDICATES[p], o)
        elif p in PROP_PREDICATES and not o_is_uri:
            acc.add_prop(s, PROP_PREDICATES[p], o, lang)
        # anything else (unknown predicate) is silently skipped -- there
        # shouldn't be any, since rdf_mapper.py only ever emits the
        # predicates listed above, but a future upstream change adding a
        # new predicate should fail loudly in dry-run rather than here.


def load_stream(line_iter, driver, dry_run: bool, source_label: str, budget: dict | None = None):
    total_stats = {"entities": 0, "label_ops": 0, "prop_ops": 0, "rel_ops": 0}
    batch = []
    started = time.monotonic()
    processed = 0

    def flush_batch(batch_lines):
        acc = BatchAccumulator()
        process_lines(batch_lines, acc)
        s = acc.stats()

        if dry_run:
            for k in total_stats:
                total_stats[k] += s[k]
            return s, True

        # Budget check BEFORE writing anything -- s['rel_ops'] is the
        # number of MERGE rows this batch would attempt, which is an
        # upper bound on new relationships (MERGE dedupes, so the real
        # number added can only be <= this). Using the upper bound means
        # we always stop with room to spare rather than risk overshooting
        # into the same crash this replaces.
        if budget is not None and s["rel_ops"] > budget["remaining"]:
            print(f"    SKIPPING rest of {source_label} -- this batch alone "
                  f"({s['rel_ops']} relationships) would exceed the remaining "
                  f"budget ({budget['remaining']}/{MAX_RELATIONSHIPS}). "
                  f"Not marking this file as loaded -- it can be finished "
                  f"later (paid tier, or after freeing up room).")
            return s, False

        try:
            with driver.session() as session:
                session.execute_write(acc.flush)
        except Exception as e:
            # Safety net for the budget estimate above being wrong in some
            # edge case (it shouldn't be, given MERGE only ever adds <=
            # rel_ops -- but a crash here is exactly the failure mode this
            # whole mechanism exists to prevent, so never let one escape).
            msg = str(e)
            if "relationship" in msg.lower() and ("limit" in msg.lower() or "exceed" in msg.lower()):
                print(f"    SKIPPING rest of {source_label} -- hit Aura's real "
                      f"relationship cap despite the budget check above "
                      f"(estimate was off). Not marking this file as loaded.")
                if budget is not None:
                    budget["remaining"] = 0
                return s, False
            raise

        for k in total_stats:
            total_stats[k] += s[k]
        if budget is not None:
            budget["remaining"] -= s["rel_ops"]
        return s, True

    file_complete = True
    for line in line_iter:
        batch.append(line)
        processed += 1
        if len(batch) >= BATCH_LINES:
            _, ok = flush_batch(batch)
            batch = []
            if not ok:
                file_complete = False
                break
            elapsed = time.monotonic() - started
            print(f"    ...{processed} triples processed ({elapsed:.0f}s elapsed)")
    if batch and file_complete:
        _, ok = flush_batch(batch)
        if not ok:
            file_complete = False
        elapsed = time.monotonic() - started
        status = "done" if file_complete else "INCOMPLETE -- skipped, see SKIPPING line above"
        print(f"    ...{processed} triples processed ({elapsed:.0f}s elapsed) [{source_label} {status}]")

    return total_stats, file_complete


# ---------------------------------------------------------------------------

def get_neo4j_driver():
    from neo4j import GraphDatabase

    uri = os.environ.get("NEO4J_URI")
    user = os.environ.get("NEO4J_USERNAME")
    password = os.environ.get("NEO4J_PASSWORD")
    if not all([uri, user, password]):
        sys.exit(
            "Missing Neo4j config. Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD "
            "as environment variables -- from your Aura instance's connection details."
        )
    return GraphDatabase.driver(uri, auth=(user, password))


def get_blob_container():
    from azure.storage.blob import BlobServiceClient

    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    container_name = os.environ.get("AZURE_STORAGE_CONTAINER")
    if not all([conn_str, container_name]):
        sys.exit(
            "Missing Azure Blob config. Set AZURE_STORAGE_CONNECTION_STRING and "
            "AZURE_STORAGE_CONTAINER as environment variables -- the same ones "
            "fetch_cbs_tables.py uses."
        )
    service = BlobServiceClient.from_connection_string(conn_str)
    return service.get_container_client(container_name)


def load_manifest(container, manifest_key: str = LOAD_MANIFEST_KEY) -> set:
    from azure.core.exceptions import ResourceNotFoundError
    try:
        data = container.download_blob(manifest_key).readall()
        return set(json.loads(data)["processed_keys"])
    except ResourceNotFoundError:
        return set()


def save_manifest(container, processed_keys: set, manifest_key: str = LOAD_MANIFEST_KEY):
    from azure.storage.blob import ContentSettings
    body = json.dumps({"processed_keys": sorted(processed_keys)}, indent=2).encode()
    container.upload_blob(
        manifest_key, body, overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def run_azure(dry_run: bool, tables: set | None = None, scope: str | None = None, force: bool = False):
    container = get_blob_container()
    manifest_key = "load/_processed_manifest_poc.json" if scope == "poc" else LOAD_MANIFEST_KEY
    processed = load_manifest(container, manifest_key)

    # transform's --scope poc writes "<table>.poc.nt" instead of
    # "<table>.nt" -- match the SAME suffix here, in both directions,
    # so a poc load only ever picks up poc-scoped files (and vice versa).
    # ("foo.poc.nt".endswith(".nt") is also True, so this can't be a
    # plain endswith(".nt") check or a poc run would load full files too.)
    suffix = ".poc.nt" if scope == "poc" else ".nt"
    blobs = [
        b for b in container.list_blobs(name_starts_with=VALIDATED_PREFIX)
        if b.name.endswith(suffix)
        and (scope == "poc" or not b.name.endswith(".poc.nt"))
        # --force skips the "already processed" check entirely -- needed
        # after wiping the Neo4j database by hand (the manifest still
        # thinks those files were loaded, but the graph they were loaded
        # into no longer exists), and useful for --dry-run any time you
        # want real stats on files that are technically already loaded.
        and (force or b.name not in processed)
    ]
    if tables:
        # Filter by table ID -- the filename is always "<table_id>_<timestamp>.nt",
        # so this is a plain prefix check on the basename. Useful for a Free-tier
        # POC: AuraDB Free caps out at 200k nodes / 400k relationships, and the
        # biggest CBS tables blow past that alone (one node per row x measure
        # field). Load only the small tables until you're on a paid tier.
        blobs = [b for b in blobs if os.path.basename(b.name).split("_")[0] in tables]
    # Smallest file first -- maximizes how many DISTINCT tables (and so how
    # many KPIs) get real data before the relationship budget below runs
    # out, instead of one huge table (e.g. 84466NED) eating the whole
    # budget first and leaving every other KPI empty.
    blobs.sort(key=lambda b: b.size or 0)
    keys = [b.name for b in blobs]
    if not keys:
        print("No new validated files to load (after any --tables filter). Neo4j is up to date.")
        return

    driver = None if dry_run else get_neo4j_driver()
    budget = None
    if not dry_run:
        with driver.session() as session:
            session.run(CONSTRAINT_CYPHER)
            current = session.run("MATCH ()-->() RETURN count(*) AS c").single()["c"]
        budget = {"remaining": max(0, MAX_RELATIONSHIPS - current)}
        print(f"Current relationships in Neo4j: {current:,}. Budget for this run: "
              f"{budget['remaining']:,} (cap {MAX_RELATIONSHIPS:,}).")

    print(f"Found {len(keys)} new validated file(s) to load{' (DRY RUN -- no writes)' if dry_run else ''}, smallest first.")
    incomplete = []
    for key in keys:
        print(f"Processing {key} ...")
        text = container.download_blob(key).readall().decode("utf-8")
        stats, complete = load_stream(text.splitlines(), driver, dry_run, key, budget)
        print(f"  {stats}{'' if complete else '  (INCOMPLETE -- see SKIPPING line above)'}")
        if not dry_run:
            if complete:
                processed.add(key)
                save_manifest(container, processed, manifest_key)
            else:
                incomplete.append(key)

    if driver:
        driver.close()
    if dry_run:
        print("Dry run complete -- nothing was written to Neo4j or the manifest.")
    elif incomplete:
        print(f"Done, with {len(incomplete)}/{len(keys)} file(s) left incomplete (relationship "
              f"budget ran out): {incomplete}")
        print("Everything that fit is loaded and safe to use right now. The tables above "
              "weren't -- upgrade the Aura tier (or free up room) and re-run with --force "
              "to pick them up; nothing needs to be redone for the tables that already succeeded.")
    else:
        print("Done. Every table fit within budget.")


def run_local(dry_run: bool, tables: set | None = None, scope: str | None = None):
    here = os.path.dirname(os.path.abspath(__file__))
    in_dir = os.path.join(here, "sample_validated")
    suffix = ".poc.nt" if scope == "poc" else ".nt"
    files = sorted(
        f for f in os.listdir(in_dir)
        if f.endswith(suffix) and (scope == "poc" or not f.endswith(".poc.nt"))
    )
    if tables:
        files = [f for f in files if f.split("_")[0] in tables]
    print(f"Found {len(files)} local sample file(s){' (DRY RUN -- no writes)' if dry_run else ''}.")

    driver = None if dry_run else get_neo4j_driver()
    if not dry_run:
        with driver.session() as session:
            session.run(CONSTRAINT_CYPHER)

    for fname in files:
        print(f"Processing {fname} ...")
        with open(os.path.join(in_dir, fname), encoding="utf-8") as f:
            # No budget passed here -- local sample fixtures are tiny by
            # construction, nowhere near Aura Free's cap.
            stats, _complete = load_stream(f, driver, dry_run, fname)
        print(f"  {stats}")

    if driver:
        driver.close()
    print("Done." if not dry_run else "Dry run complete -- nothing was written to Neo4j.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="read ./sample_validated/*.nt instead of Azure Blob")
    parser.add_argument("--dry-run", action="store_true", help="parse + classify triples, print stats, write nothing")
    parser.add_argument(
        "--tables", default=None,
        help="comma-separated CBS table IDs to load, e.g. 85958NED,83827NED,81588NED -- "
             "skips all others. Combine with --scope poc, or use alone on full-size "
             "tables that are already small enough for Free.",
    )
    parser.add_argument(
        "--scope", choices=["poc"], default=None,
        help="poc: load only validated/*.poc.nt (produced by "
             "transform/run_transform.py --scope poc) instead of the full-fidelity "
             "*.nt files. Uses its own manifest, so a poc load and a full load never "
             "interfere with each other.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the processed-files manifest -- reprocesses files even if they're "
             "already marked loaded. Use this after wiping the Neo4j database by hand "
             "(the manifest doesn't know the graph is now empty), or with --dry-run to "
             "see real per-table stats regardless of load history. Ignored with --local "
             "(no manifest there).",
    )
    args = parser.parse_args()
    tables = set(t.strip() for t in args.tables.split(",")) if args.tables else None

    if args.local:
        run_local(args.dry_run, tables, args.scope)
    else:
        run_azure(args.dry_run, tables, args.scope, args.force)
