"""
companies.json -> Neo4j (Aura), linked to the EXISTING CBS graph.

    (:Company:Entity {uri, kvk, name, staff, revenue, lei, ...})
        -[:IN_BRANCH]->  (:Branch)      the CBS industry node KPI 1-5 already use (matched on its Dutch label)
        -[:LOCATED_IN]-> (:Gemeente)    the CBS municipality node (matched on city name; skipped if no match)
        -[:SUBSIDIARY_OF]-> (:Company)  direct parent from GLEIF (parent created as a light node if not loaded)
        -[:USES_ERP {source, confidence, lifecycle, evidence}]-> (:ERPSystem {name, vendor, lifecycle})
                                        only for companies in erp.json (erp_enrich.py / signals_enrich.py); replaced on every run
    (:Company).signals / signalCount / erpChangeSignal   job-vacancy buying signals from signals.json
    (:Company).accountStatus / accountOwner / accountNote   our own account knowledge from accounts.json

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
    python load_companies.py --erp-only       # only (re)load erp.json -> USES_ERP links, leave companies as they are
"""
import argparse
import os
import sys
from datetime import datetime, timezone

from common import LANDING, read_json

BASE = "https://data.cbs-knowledge-graph.example/id/"
FREE_NODES, FREE_RELS, SAFETY = 200_000, 400_000, 0.90
BATCH = 500

PROPS = ["kvk", "kvkSource", "listSources", "name", "legalName", "tradeNames", "mainSbi", "mainSbiDesc", "sbiCodes", "section", "staff",
         "sizeBand", "ictSizeBand", "legalForm", "city", "postcode", "street", "website", "lei", "leiStatus",
         "parentName", "ultimateParentName", "revenue", "revenueYear", "revenueSource", "wikidata", "sources",
         "industrySource", "noMarketing"]

LOAD = """
UNWIND $rows AS row
MERGE (c:Entity {uri: row.uri})
SET c:Company, c += row.props
WITH c, row
// re-runs must REPLACE the industry / municipality link, not add a second one (a company had two industries)
OPTIONAL MATCH (c)-[oldB:IN_BRANCH]->()
DELETE oldB
WITH DISTINCT c, row
OPTIONAL MATCH (c)-[oldG:LOCATED_IN]->()
DELETE oldG
WITH DISTINCT c, row
OPTIONAL MATCH (b:Branch {prefLabelNl: row.branchLabel})
FOREACH (_ IN CASE WHEN b IS NULL THEN [] ELSE [1] END | MERGE (c)-[:IN_BRANCH]->(b))
WITH c, row
OPTIONAL MATCH (g:Gemeente) WHERE row.city IS NOT NULL AND toLower(g.prefLabelNl) = toLower(row.city)
WITH c, row, collect(g)[0] AS gm
FOREACH (_ IN CASE WHEN gm IS NULL THEN [] ELSE [1] END | MERGE (c)-[:LOCATED_IN]->(gm))
WITH c, row WHERE row.parentLei IS NOT NULL
OPTIONAL MATCH (k:Company {lei: row.parentLei})
WITH c, row, collect(k)[0] AS known
// parent not loaded as a company yet -> create a light parent node
FOREACH (_ IN CASE WHEN known IS NULL THEN [1] ELSE [] END |
  MERGE (p:Entity {uri: $base + 'lei/' + row.parentLei})
  ON CREATE SET p:Company, p.lei = row.parentLei, p.name = row.parentName, p.parentOnly = true
  MERGE (c)-[:SUBSIDIARY_OF]->(p))
// parent already loaded -> link to it
FOREACH (_ IN CASE WHEN known IS NOT NULL AND known <> c THEN [1] ELSE [] END |
  MERGE (c)-[:SUBSIDIARY_OF]->(known))
RETURN count(*) AS n
"""


LOAD_ERP = """
UNWIND $rows AS row
MATCH (c:Company {kvk: row.kvk})
MERGE (e:Entity {uri: $base + 'erp/' + row.slug})
ON CREATE SET e:ERPSystem, e.name = row.erp, e.vendor = row.vendor
SET e:ERPSystem, e.lifecycle = row.lifecycle
MERGE (c)-[r:USES_ERP]->(e)
SET r.source = row.source, r.confidence = row.confidence, r.evidence = row.evidence,
    r.evidenceUrl = row.evidenceUrl, r.checkedAt = row.checkedAt, r.lifecycle = row.lifecycle
RETURN count(r) AS n
"""


LOAD_SIGNALS = """
UNWIND $rows AS row
MATCH (c:Company {kvk: row.kvk})
SET c.signals = row.signals, c.signalCount = size(row.signals), c.erpChangeSignal = row.change
RETURN count(c) AS n
"""


def load_signals(session, dry_run):
    """signals.json (job-vacancy buying signals) -> properties on :Company. Each signal is one string
    'type|erp|title|url|date' (Neo4j properties cannot hold maps); the dashboard splits it again."""
    sig = read_json(LANDING / "signals.json", {})
    if not sig:
        print("No signals.json -- run signals_enrich.py to add vacancy-based buying signals (skipping).")
        return
    clean = lambda x: str(x or "").replace("|", "/").replace("\n", " ")[:160]
    rows = [{"kvk": k, "change": any(s["type"] == "erp_change" for s in v),
             "signals": [f"{s['type']}|{clean(s.get('erp'))}|{clean(s.get('title'))}|{clean(s.get('url'))}|{clean(s.get('date'))}" for s in v]}
            for k, v in sig.items()]
    print(f"Signals: {len(rows):,} companies, {sum(1 for r in rows if r['change']):,} with an ERP project / migration signal")
    if dry_run:
        return
    session.run("MATCH (c:Company) WHERE c.signals IS NOT NULL REMOVE c.signals, c.signalCount, c.erpChangeSignal").consume()
    n = sum(session.run(LOAD_SIGNALS, rows=rows[i:i + BATCH]).single()["n"] for i in range(0, len(rows), BATCH))
    print(f"  signals written on {n:,} companies.")


LOAD_ACCOUNTS = """
UNWIND $rows AS row
MATCH (c:Company {kvk: row.kvk})
SET c.accountStatus = row.status, c.accountOwner = row.owner, c.accountNote = row.note
RETURN count(c) AS n
"""


def load_accounts(session, dry_run):
    """accounts.json (our own account knowledge: customer / prospect / lost / partner / competitor / do-not-contact,
    owner) -> properties on :Company, shown in KPI 10 so sales never cold-calls a customer."""
    acc = read_json(LANDING / "accounts.json", {})
    if not acc:
        print("No accounts.json -- add account_status / owner columns to erp_evidence.csv (skipping).")
        return
    rows = [{"kvk": k, "status": v.get("status"), "owner": v.get("owner"), "note": v.get("note")} for k, v in acc.items()]
    print(f"Accounts: {len(rows):,} companies with a status / owner")
    if dry_run:
        return
    session.run("MATCH (c:Company) WHERE c.accountStatus IS NOT NULL OR c.accountOwner IS NOT NULL "
                "REMOVE c.accountStatus, c.accountOwner, c.accountNote").consume()
    n = sum(session.run(LOAD_ACCOUNTS, rows=rows[i:i + BATCH]).single()["n"] for i in range(0, len(rows), BATCH))
    print(f"  account status written on {n:,} companies.")


def load_erp(session, dry_run):
    from erp_enrich import slug
    load_signals(session, dry_run)
    load_accounts(session, dry_run)
    recs = read_json(LANDING / "erp.json", [])
    if not recs:
        print("No erp.json -- run erp_enrich.py to add ERP evidence (skipping ERP links).")
        return
    rows = [{**r, "slug": slug(r["erp"]), "lifecycle": r.get("lifecycle") or "unknown"}
            for r in recs if r.get("kvk") and r.get("erp")]
    print(f"ERP: {len(rows):,} records for {len({r['kvk'] for r in rows}):,} companies, "
          f"{len({r['slug'] for r in rows}):,} ERP systems")
    if dry_run:
        return
    session.run("CREATE INDEX erp_name IF NOT EXISTS FOR (e:ERPSystem) ON (e.name)").consume()
    session.run("MATCH ()-[r:USES_ERP]->() DELETE r").consume()   # erp.json is the source of truth
    linked = 0
    for i in range(0, len(rows), BATCH):
        linked += session.run(LOAD_ERP, rows=rows[i:i + BATCH], base=BASE).single()["n"]
    print(f"  {linked:,} USES_ERP links written (companies not in the graph are skipped).")


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
        rows.append({"uri": BASE + "company/" + (c.get("uriKey") or c["kvk"]), "props": props, "branchLabel": c.get("branchLabel"),
                     "city": c.get("city"), "parentLei": c.get("parentLei"), "parentName": c.get("parentName")})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true", help="ignore the 90%% Aura Free safety margin")
    ap.add_argument("--remove-test", action="store_true")
    ap.add_argument("--erp-only", action="store_true", help="only load erp.json (USES_ERP links)")
    a = ap.parse_args()

    if a.remove_test:
        with driver() as d, d.session() as s:
            n = s.run("MATCH (c:Company {testData: true}) DETACH DELETE c RETURN count(*) AS n").single()["n"]
            print(f"Removed {n} test companies.")
        return

    if a.erp_only:
        with driver() as d, d.session() as s:
            load_erp(s, a.dry_run)
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
        load_erp(s, False)


if __name__ == "__main__":
    main()
