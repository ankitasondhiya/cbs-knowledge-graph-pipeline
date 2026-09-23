"""
Stage 1 of the CBS -> Knowledge Graph pipeline: Extraction Worker.

Pulls EVERY table currently published in the CBS StatLine open-data
catalog and lands each one untouched in a "raw landing zone", exactly
the Ingestion stage from the breadboard. This is the full, unfiltered
catalog (cbsodata.get_table_list()) -- no curated shortlist, no B2B/
business-only filter, no topic filter of any kind. That's a deliberate
choice for the RAW LAYER ONLY: everything CBS publishes lands here so
nothing is thrown away before anyone's decided they don't need it.
Filtering by relevance still happens downstream, in transform/run_transform.py
(--scope poc) and load/run_load.py (--tables), which is where it belongs --
this stage's only job is faithful, complete ingestion.

Scope is all of the Netherlands (no region filter). Tables that carry a
time dimension ("Perioden") are fetched for their latest period only --
history accumulates naturally across runs via the timestamped files
instead of re-pulling decades of quarters every time.

The landing zone lives in Azure Blob Storage, not git -- these files are
too large and too frequent to version-control sanely. Set these env vars
(see .github/workflows/fetch.yml) to enable upload; without them the
script just writes locally, which is enough for testing.

    AZURE_STORAGE_CONNECTION_STRING
    AZURE_STORAGE_CONTAINER

Also implements the incremental-load check from the Orchestration rail:
before pulling a table, it checks that table's `Modified` timestamp
against the last timestamp we successfully pulled for it, and skips that
table if nothing changed on CBS's side. Each table is tracked
independently, so one table changing doesn't force a re-pull of the rest.

Pull order is PRIORITIZED, not catalog order: IMPORTANT_TABLES (the same
31 business/B2B + KPI-gap-analysis tables transform/run_transform.py
actually processes) are pulled first, so the tables that matter for the
KPI work land within the first few minutes of a run instead of waiting
for the full ~1,250-table catalog to be worked through alphabetically/
by-catalog-order. Everything else follows after, in whatever order the
catalog returned it. IMPORTANT_TABLES is duplicated here (not imported)
from transform/run_transform.py's copy on purpose -- fetch's
requirements.txt deliberately stays light (no rdflib/pyshacl), and
transform's own imports assume it's run from inside transform/. Keep the
two lists in sync by hand if you change one.

A note on runtime: the CBS catalog has several thousand tables. A single
GitHub Actions job is hard-capped at 6 hours, which is very unlikely to
be enough to land every table on the FIRST run. That's fine by design --
progress (pipeline_state.json) is saved after every single table, not
just at the end, so a run that gets cut off (timeout, cancelled, failed)
picks up exactly where it left off on the next scheduled run instead of
starting over. Expect the first full backfill to take several days'
worth of daily runs before "No tables changed since last run" actually
means the whole catalog is landed. A single table failing to fetch
(malformed metadata, a transient CBS-side error, etc.) is logged and
skipped rather than aborting the whole run -- it's retried automatically
next time since it's never marked as pulled.

Requirements:
    pip install -r requirements.txt

CBS open data license: CC-BY. Free to use, including commercially --
just credit CBS (Centraal Bureau voor de Statistiek) as the source
wherever this data is published downstream.
"""

import json
import os
from datetime import datetime, timezone

import cbsodata

LANDING_ZONE = "./landing_zone"
STATE_FILE = "./pipeline_state.json"

AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER")

# Kept in sync by hand with transform/run_transform.py's IMPORTANT_TABLES --
# see this file's module docstring for why it's duplicated rather than
# imported. These are pulled FIRST, before the rest of the catalog.
IMPORTANT_TABLES = {
    "86165NED", "81589NED", "81588NED", "83148NED", "83149NED", "83147NED",
    "83827NED", "85828NED", "85958NED", "84765NED", "81578NED", "83631NED",
    "83635NED", "86280NED", "86281NED", "86282NED", "82242NED", "82522NED",
    "82244NED", "85610NED", "85609NED", "85611NED", "85612NED", "85614NED",
    "86413NED", "85821NED", "81234ned",
    # -- added to close KPI gap-analysis findings --
    "86119NED", "80567NED", "84466NED", "48051NED",
}


def load_full_table_catalog() -> dict:
    """
    Returns {table_id: title} for EVERY table currently published in the
    CBS StatLine open-data catalog -- not a curated shortlist. Uses
    cbsodata.get_table_list(), which hits ODataCatalog/Tables once (no
    per-table cost yet). Falls back to the table's own Identifier as the
    description if a title is missing for some reason, so nothing in the
    catalog is ever silently skipped for lack of a pretty name.
    """
    catalog = cbsodata.get_table_list()
    tables = {}
    for entry in catalog:
        table_id = entry.get("Identifier")
        if not table_id:
            continue
        title = entry.get("Title") or entry.get("ShortTitle") or table_id
        tables[table_id] = title
    return tables


def load_state() -> dict:
    """Remembers the CBS 'Modified' date we last saw, per table."""
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)


def get_table_modified_date(table_id: str) -> str:
    """
    Reads the table's own metadata to get its last-modified timestamp.
    This is the check the Orchestration & Scheduling rail runs on every
    scheduled trigger -- it is what makes the load incremental instead of
    a blind full reload.
    """
    info = cbsodata.get_meta(table_id, "TableInfos")
    return info[0]["Modified"]


def get_latest_period_filter(table_id: str) -> str | None:
    """
    Returns an OData filter clause restricting to the latest period, or
    None if the table has no time dimension (e.g. a point-in-time
    snapshot table like neighbourhood demographics).
    """
    props = cbsodata.get_meta(table_id, "DataProperties")
    has_time_dim = any(p.get("Type") == "TimeDimension" for p in props)
    if not has_time_dim:
        return None

    periods = cbsodata.get_meta(table_id, "Perioden")
    latest_period = periods[-1]["Key"]
    return f"Perioden eq '{latest_period}'"


def fetch_table(table_id: str) -> list:
    period_filter = get_latest_period_filter(table_id)
    return cbsodata.get_data(table_id, filters=period_filter)


def upload_to_blob(local_path: str, key: str) -> bool:
    """Uploads a landed file to Azure Blob Storage. No-ops if Azure isn't configured."""
    if not all([AZURE_STORAGE_CONNECTION_STRING, AZURE_STORAGE_CONTAINER]):
        print("  Azure Blob Storage not configured (env vars missing) -- leaving file local only.")
        return False

    from azure.storage.blob import BlobServiceClient

    service_client = BlobServiceClient.from_connection_string(AZURE_STORAGE_CONNECTION_STRING)
    container_client = service_client.get_container_client(AZURE_STORAGE_CONTAINER)
    with open(local_path, "rb") as data:
        container_client.upload_blob(name=key, data=data, overwrite=True)
    print(f"  Uploaded to Azure Blob: {AZURE_STORAGE_CONTAINER}/{key}")
    return True


def pull_one_table(table_id: str, description: str, state: dict) -> bool:
    """Returns True if a new file was landed, False if skipped."""
    print(f"Checking CBS table {table_id} ({description}) for changes...")
    current_modified = get_table_modified_date(table_id)
    last_seen_modified = state.get(table_id)

    if current_modified == last_seen_modified:
        print(f"  No change since last pull ({last_seen_modified}). Skipping.")
        return False

    print(f"  Changed: last seen {last_seen_modified!r}, now {current_modified!r}. Pulling...")
    rows = fetch_table(table_id)
    print(f"  Pulled {len(rows)} rows.")

    pulled_at = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"{table_id}_{pulled_at}.json"
    out_path = os.path.join(LANDING_ZONE, filename)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(
            {
                "source_table": table_id,
                "description": description,
                "source_modified": current_modified,
                "pulled_at": pulled_at,
                "row_count": len(rows),
                "rows": rows,
            },
            f,
            ensure_ascii=False,
        )
    print(f"  Landed raw data at {out_path}")

    upload_to_blob(out_path, f"landing_zone/{filename}")

    state[table_id] = current_modified
    return True


def _resolve_missing_important_tables(tables: dict) -> None:
    """
    Some IMPORTANT_TABLES ids don't show up in cbsodata.get_table_list()'s
    catalog feed even though the table itself is real and live -- CBS's
    catalog listing and a specific table's own OData endpoint aren't
    always perfectly in sync (seen in practice with 48051NED/80567NED,
    both independently confirmed live via CBS's own site). Rather than
    silently skip a table just because the CATALOG doesn't mention it,
    probe each missing one directly via its own TableInfos metadata --
    if that succeeds, the table is real and gets added to `tables` (with
    its real title) so it flows through the normal pull path below. Only
    a table that fails this direct probe too is actually treated as gone.
    Mutates `tables` in place.
    """
    missing = sorted(t for t in IMPORTANT_TABLES if t not in tables)
    if not missing:
        return
    print(f"NOTE: {len(missing)} IMPORTANT_TABLES id(s) not in the catalog listing -- "
          f"probing each directly before giving up on it: {missing}")
    still_missing = []
    for table_id in missing:
        try:
            info = cbsodata.get_meta(table_id, "TableInfos")
            title = info[0].get("Title") or info[0].get("ShortTitle") or table_id
            tables[table_id] = title
            print(f"  {table_id}: found via direct probe (title: {title!r}) -- added back in.")
        except Exception as e:
            still_missing.append(table_id)
            print(f"  {table_id}: direct probe failed too ({e}) -- genuinely unavailable right now, skipping this run.")
    if still_missing:
        print(f"NOTE: {len(still_missing)} IMPORTANT_TABLES id(s) truly unavailable this run "
              f"(retired/renamed on CBS's side?): {still_missing}")


def _priority_ordered_ids(tables: dict) -> list:
    """IMPORTANT_TABLES first (that are actually in the live catalog, after
    the direct-probe fallback above has had a chance to add back any that
    the catalog listing alone missed), then every other table in the
    catalog's own order."""
    priority = [t for t in IMPORTANT_TABLES if t in tables]
    rest = [t for t in tables if t not in IMPORTANT_TABLES]
    return priority + rest


def main():
    os.makedirs(LANDING_ZONE, exist_ok=True)
    state = load_state()

    print("Fetching the full CBS StatLine table catalog (every published table, no topic filter)...")
    tables = load_full_table_catalog()
    print(f"Catalog has {len(tables)} table(s). Checking each for changes...")

    _resolve_missing_important_tables(tables)

    ordered_ids = _priority_ordered_ids(tables)
    num_priority = sum(1 for t in ordered_ids if t in IMPORTANT_TABLES)
    print(f"Pull order: {num_priority} IMPORTANT_TABLES first, then {len(ordered_ids) - num_priority} "
          f"remaining catalog table(s).")

    any_changed = False
    failed = []
    for i, table_id in enumerate(ordered_ids, start=1):
        description = tables[table_id]
        print(f"[{i}/{len(ordered_ids)}]", end=" ")
        try:
            if pull_one_table(table_id, description, state):
                any_changed = True
        except Exception as e:
            # One malformed/unreachable table should never abort a run
            # that's landing thousands of others. It's simply never
            # marked as pulled, so the next run retries it automatically.
            print(f"  ERROR pulling {table_id}: {e} -- skipping, will retry next run.")
            failed.append(table_id)
        # Persist state after each table so a later failure (or a job
        # timeout partway through thousands of tables) doesn't forget the
        # tables that already succeeded this run.
        save_state(state)

    if failed:
        print(f"\n{len(failed)} table(s) failed this run and will be retried automatically next run: {failed}")
    if not any_changed:
        print("No tables changed since last run -- incremental load working as intended.")


if __name__ == "__main__":
    main()
