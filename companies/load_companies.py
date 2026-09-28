"""
companies.json -> Neo4j (Aura), linked to the EXISTING CBS graph.

    (:Company:Entity {uri, kvk, name, staff, revenue, lei, ...})
        -[:IN_BRANCH]->  (:Branch)      the CBS industry node KPI 1-5 already use (matched on its Dutch label)
        -[:LOCATED_IN]-> (:Gemeente)    the CBS municipality node (matched on city name; skipped if no match)
        -[:SUBSIDIARY_OF]-> (:Company)  direct parent from GLEIF (parent created as a light node if not loaded)

Size band, SBI codes, revenue etc. are PROPERTIES, not extra nodes -- that
keeps it at ~2-3 relationships per company, so ~72k companies (all NL
businesses with 10+ staff) fit in Aura Free next to the CBS data.

Before writing it checks current node/relationship counts and refuses to
go past 90% of Aura Free's limits (200k nodes / 400k relationships) unless --force.

    set NEO4J_URI=... NEO4J_USERNAME=... NEO4J_PASSWORD=...
    python load_companies.py --dry-run        # show what would be written
    python load_companies.py                  # load companies.json
    python load_companies.py --test           # load the KVK TEST companies (flagged testData=true)
    python load_companies.py --remove-test    # delete those test companies again
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from common import LANDING, read_json

BASE = "https://data.cbs-knowledge-graph.example/id/"
FREE_NODES, FREE_RELS, SAFETY = 200_000, 400_000, 0.90
BATCH = 500

PROPS = ["kvk", "name", "legalName", "tradeNames", "mainSbi", "mainSbiDesc", "sbiCodes", "section", "staff",
         "sizeBand", "ictSizeBand", "legalForm", "city", "postcode", "street", "website", "lei", "leiStatus",
         "parentName", "ultimateParentName", "revenue", "revenueYear", "revenueSource", "wikidata", "sources",
         "industrySource", "noMarketing"]

LOAD = """
UNWIND $rows AS row
MERGE (c:Entity {uri: row.uri})
SET c:Company, c += row.props
WITH c, row
OPTIONAL MATCH (b:Branch {prefLabelNl: row.branchLabel})
FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [1] END | MERGE (c)-[:IN_BRANCH]->(b))
WITH c, row
OPTIONAL MATCH (g:Gemeente) WHERE row.city IS NOT NULL AND toLower(g.prefLabelNl) = toLower(row.city)
WITH c, row, collect(g)[0] AS gm
FOREACH (_ IN CASE WHEN gm IS NULL THEN [] ELSE [1] END | MERGE (c)-[:LOCATED_IN]->(gm))
WITH c, row WHERE row.parentLei IS NOT NULL
OPTIONAL MATCH (k:Company {lei: row.parentLei})
WITH c, row, collect(k)[0] AS known
CALL {
  WITH c, row, known
  WITH c, row, known WHERE known IS NULL
  MERGE (p:Entity {uri: $base + 'lei/' + row.parentLei})
  ON CREATE SET p:Company, p.lei = row.parentLei, p.name = row.parentName, p.parentOnly = true
  MERGE (c)-[:SUBSIDIARY_OF]->(p)
  RETURN count(*) AS createdParent
}
CALL {
  WITH c, known
  WITH c, known WHERE known IS NOT NULL AND known <> c
  MERGE (c)-[:SUBSIDIARY_OF]->(known)
  RETURN count(*) AS linkedParent
}
RETURN count(*) AS n
"""


def driver():
    from neo4j import GraphDatabase
    uri, user, pw = (os.environ.get(k) for k in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD"))
    if not all([uri, user, pw]):
        sys.exit("Set NEO4J_URI, NEO4J_USERNAME, NEO4J_PASSWORD (same as load/run_load.py).")
    return GraphDatabase.driver(uri, auth=(user, pw))


def rows_for(companies, test):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for c in companies:
        props = {k: c[k] for k in PROPS if c.get(k) not in (None, [], "")}
        props["loadedAt"] = now
        if test:
            props["testData"] = True
        rows.append({"uri": BASE + "company/" + c["kvk"], "props": props, "branchLabel": c.get("branchLabel"),
                     "city": c.get("city"), "parentLei": c.get("parentLei"), "parentName": c.get("parentName")})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the 90%% Aura Free safety margin")
    ap.add_argument("--remove-test", action="store_true")
    a = ap.parse_args()

    if a.remove_test:
        with driver() as d, d.session() as s:
            n = s.run("MATCH (c:Company {testData: true}) DETACH DELETE c RETURN count(*) AS n").single()["n"]
            print(f"Removed {n} test companies.")
        return

    companies = read_json(LANDING / ("companies.test.json" if a.test else "companies.json"), [])
    if not companies:
        sys.exit("No companies to load -- run build_companies.py first.")
    rows = rows_for(companies, a.test)
    est_nodes = len(rows) + len({r["parentLei"] for r in rows if r["parentLei"]})
    est_rels = sum(2 + (1 if r["parentLei"] else 0) for r in rows)
    print(f"{len(rows):,} companies -> up to ~{est_nodes:,} nodes and ~{est_rels:,} relationships")
    print(f"  industries: {sum(1 for r in rows if r['branchLabel']):,} have an SBI section to link to")

    with driver() as d, d.session() as s:
        nodes = s.run("MATCH (n) RETURN count(n) AS n").single()["n"]
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS n").single()["n"]
        print(f"Aura now: {nodes:,} nodes ({nodes / FREE_NODES:.0%}), {rels:,} relationships ({rels / FREE_RELS:.0%})")
        after_n, after_r = nodes + est_nodes, rels + est_rels
        print(f"After load (worst case): {after_n:,} nodes ({after_n / FREE_NODES:.0%}), "
              f"{after_r:,} relationships ({after_r / FREE_RELS:.0%})")
        if not a.force and (after_n > FREE_NODES * SAFETY or after_r > FREE_RELS * SAFETY):
            sys.exit("Stopping: this would pass 90% of Aura Free's limits. Load fewer companies "
                     "(e.g. build_companies.py --min-staff 20) or re-run with --force.")
        if a.dry_run:
            print("Dry run -- nothing written.")
            return
        s.run("CREATE INDEX company_kvk IF NOT EXISTS FOR (c:Company) ON (c.kvk)")
        s.run("CREATE INDEX company_lei IF NOT EXISTS FOR (c:Company) ON (c.lei)")
        for i in range(0, len(rows), BATCH):
            s.run(LOAD, rows=rows[i:i + BATCH], base=BASE).consume()
            print(f"  loaded {min(i + BATCH, len(rows)):,}/{len(rows):,}", flush=True)
        linked = s.run("MATCH (c:Company)-[:IN_BRANCH]->() RETURN count(DISTINCT c) AS n").single()["n"]
        print(f"Done. {linked:,} companies linked to a CBS industry.")


if __name__ == "__main__":
    main()
