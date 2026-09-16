"""
RDF Mapping + Glossary Alignment, generalized (Transform & Validate stage,
boxes 1 and 2 of 4).

Works across all 13 tables in your fetch_cbs_tables.py, because it treats
every row generically:

  - Fields listed in glossary.DIMENSION_FIELDS classify the row (region,
    industry branch, country, period, ...). Each becomes either a linked
    dimension-value node (a small skos:Concept-like node reused across
    rows that share it) or, for Perioden, a plain literal on the
    Observation.
  - Every other numeric field is a MEASURE: one qb:Observation per
    (row, measure field). Its business meaning comes from glossary.py
    when curated there, or an auto-generated fallback label otherwise --
    either way it still gets a proper skos:Concept, just flagged
    verified=False so you know what still needs curating.

Region hierarchy special case: WijkenEnBuurten (86165NED only) carries
real CBS GM/WK/BU codes, and Buurt -> Wijk -> Gemeente parentage is
derived purely from the code structure -- see region_type_and_parent().
RegioS on the business tables is a DIFFERENT, unrelated classification
(national/foreign/unclassified) and is deliberately kept separate --
see the note in glossary.py.
"""

import re
from datetime import datetime, timezone

from rdflib import Graph, Namespace, URIRef, Literal, RDF, RDFS
from rdflib.namespace import SKOS, PROV, XSD

from glossary import lookup, dimension_info, DIMENSION_FIELDS

QB = Namespace("http://purl.org/linked-data/cube#")
ORG = Namespace("http://www.w3.org/ns/org#")
EX = Namespace("https://data.cbs-knowledge-graph.example/id/")
EXO = Namespace("https://data.cbs-knowledge-graph.example/ontology/")


# ---------- region hierarchy (WijkenEnBuurten only) ----------

def region_uri(code: str) -> URIRef:
    return EX[f"region/{code.strip()}"]


def region_type_and_parent(code: str):
    """
    Classifies a CBS region code and derives its parent code, purely from
    the code's own structure: BU<gemeente><wijk><buurt> -> WK<gemeente><wijk> -> GM<gemeente>.
    Anything that doesn't match (e.g. the literal "Nederland" aggregate
    row) falls back to a generic Region type with no parent.
    """
    code = code.strip()
    if code.startswith("GM") and len(code) >= 6:
        return EXO.Gemeente, None
    if code.startswith("WK") and len(code) >= 8:
        return EXO.Wijk, f"GM{code[2:6]}"
    if code.startswith("BU") and len(code) >= 10:
        return EXO.Buurt, f"WK{code[2:6]}{code[6:8]}"
    return EXO.Region, None


def add_region(g: Graph, code: str, name: str | None = None) -> URIRef:
    uri = region_uri(code)
    rdf_type, parent_code = region_type_and_parent(code)
    g.add((uri, RDF.type, rdf_type))
    g.add((uri, EXO.regionCode, Literal(code)))
    if name:
        g.add((uri, SKOS.prefLabel, Literal(name.strip(), lang="nl")))
    if parent_code:
        parent_uri = region_uri(parent_code)
        parent_type, _ = region_type_and_parent(parent_code)
        g.add((parent_uri, RDF.type, parent_type))
        g.add((parent_uri, EXO.regionCode, Literal(parent_code)))
        g.add((uri, ORG.unitOf, parent_uri))
        g.add((parent_uri, ORG.hasUnit, uri))
    return uri


# ---------- generic dimension-value nodes (branch, country, etc.) ----------

def slugify(text: str) -> str:
    text = text.strip().lower()
    text = re.sub(r"[^a-z0-9]+", "-", text)
    return text.strip("-")[:80]


def dimension_value_uri(short_name: str, raw_value: str) -> URIRef:
    return EX[f"{short_name}/{slugify(raw_value)}"]


def add_dimension_value(g: Graph, info: dict, raw_value: str) -> URIRef:
    """Adds (idempotently) a node for one dimension VALUE, e.g. one specific industry branch."""
    uri = dimension_value_uri(info["short_name"], raw_value)
    g.add((uri, RDF.type, EXO[info["class"]]))
    g.add((uri, SKOS.prefLabel, Literal(raw_value.strip(), lang="nl")))
    return uri


# ---------- glossary concepts for measures ----------

def humanize_field_name(field_name: str) -> str:
    """Auto-fallback label for a measure not yet curated in glossary.py."""
    base = re.sub(r"_\d+$", "", field_name)  # strip trailing _<N> suffix
    spaced = re.sub(r"(?<!^)(?=[A-Z])", " ", base)  # insert spaces before capitals
    return spaced.strip()


def concept_uri(table_id: str, slug: str) -> URIRef:
    return EX[f"concept/{table_id}/{slug}"]


def add_concept(g: Graph, table_id: str, field_name: str) -> tuple[URIRef, dict]:
    """
    Adds (idempotently) a skos:Concept for one measure field. Uses the
    curated glossary entry when one exists; otherwise auto-generates a
    label and marks it unverified, so the field still flows through the
    pipeline instead of being silently dropped.
    """
    entry = lookup(table_id, field_name)
    if entry is None:
        entry = {
            "slug": slugify(f"{table_id}-{field_name}"),
            "label_en": humanize_field_name(field_name),
            "label_nl": humanize_field_name(field_name),
            "definition_en": "Auto-generated label -- not yet curated in glossary.py. Verify against CBS DataProperties metadata.",
            "unit": "unknown",
            "topic": "uncurated",
            "verified": False,
        }
    uri = concept_uri(table_id, entry["slug"])
    g.add((uri, RDF.type, SKOS.Concept))
    g.add((uri, SKOS.prefLabel, Literal(entry["label_en"], lang="en")))
    g.add((uri, SKOS.prefLabel, Literal(entry["label_nl"], lang="nl")))
    g.add((uri, SKOS.definition, Literal(entry["definition_en"], lang="en")))
    g.add((uri, EXO.unit, Literal(entry["unit"])))
    g.add((uri, EXO.topic, Literal(entry["topic"])))
    g.add((uri, EXO.verified, Literal(entry["verified"])))
    return uri, entry


# ---------- main entry point ----------

def dataset_uri(table_id: str) -> URIRef:
    return EX[f"dataset/{table_id}"]


def map_rows_to_graph(rows: list, table_id: str, description: str, run_id: str, pulled_at: str) -> tuple[Graph, list]:
    """
    Converts raw CBS rows (from ANY of the 13 tables) into an RDF graph.
    Returns (graph, unverified_concepts_used) -- concept slugs that came
    from the auto-fallback rather than a curated glossary entry, so you
    know exactly what to prioritise curating next.
    """
    g = Graph()
    g.bind("qb", QB)
    g.bind("org", ORG)
    g.bind("skos", SKOS)
    g.bind("prov", PROV)
    g.bind("ex", EX)
    g.bind("exo", EXO)

    ds_uri = dataset_uri(table_id)
    g.add((ds_uri, RDF.type, QB.DataSet))
    g.add((ds_uri, RDFS.label, Literal(description or f"CBS StatLine table {table_id}")))

    generated_at = datetime.now(timezone.utc).isoformat()
    unverified_seen = set()

    for row in rows:
        # --- resolve this row's dimension links first ---
        dim_links = {}  # predicate -> URIRef, collected before building observations
        period_literal = None
        region_node = None

        for field, value in row.items():
            info = dimension_info(field)
            if info is None:
                continue  # it's a measure, handled below
            if value is None or (isinstance(value, str) and not value.strip()):
                continue

            if field == "WijkenEnBuurten":
                gemeente_name = row.get("Gemeentenaam_1")
                region_node = add_region(g, value, gemeente_name)
            elif info.get("literal"):
                period_literal = value.strip() if isinstance(value, str) else value
            elif not info.get("skip"):
                dim_links[info["predicate"]] = add_dimension_value(g, info, value)

        # --- one Observation per measure field ---
        for field, value in row.items():
            if dimension_info(field) is not None:
                continue
            if value is None or not isinstance(value, (int, float)):
                continue

            concept, entry = add_concept(g, table_id, field)
            if not entry["verified"]:
                unverified_seen.add(entry["slug"])

            obs_key = "_".join(filter(None, [
                table_id, field, run_id,
                slugify(str(row.get("ID", ""))),
            ]))
            obs_uri = EX[f"obs/{obs_key}"]
            g.add((obs_uri, RDF.type, QB.Observation))
            g.add((obs_uri, QB.dataSet, ds_uri))
            g.add((obs_uri, EXO.variable, concept))
            g.add((obs_uri, RDF.value, Literal(float(value), datatype=XSD.double)))
            g.add((obs_uri, PROV.wasDerivedFrom, Literal(f"cbs:{table_id}")))
            g.add((obs_uri, PROV.generatedAtTime, Literal(generated_at, datatype=XSD.dateTime)))
            g.add((obs_uri, EXO.sourcePulledAt, Literal(pulled_at)))

            if region_node is not None:
                g.add((obs_uri, EXO.region, region_node))
            if period_literal is not None:
                g.add((obs_uri, EXO.period, Literal(str(period_literal))))
            for predicate, node in dim_links.items():
                g.add((obs_uri, EXO[predicate], node))

    return g, sorted(unverified_seen)
