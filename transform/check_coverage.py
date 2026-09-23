"""
Coverage check: has every table Transform & Validate actually needs
(IMPORTANT_TABLES in run_transform.py -- the curated B2B set + the 4 KPI
gap-analysis tables) actually landed in the Azure Blob landing zone yet?

fetch_cbs_tables.py now pulls the ENTIRE CBS catalog (~1,250+ tables) and
that takes days to fully land, so this answers the practical question:
"is transform going to have anything to do for the tables I actually
care about, or is it still waiting on fetch?" It reads the real landing
zone in Blob storage -- not pipeline_state.json, not guesses -- so it's
ground truth regardless of where fetch_cbs_tables.py's run currently is.

Usage:
    cd transform
    export AZURE_STORAGE_CONNECTION_STRING=...
    export AZURE_STORAGE_CONTAINER=...
    python check_coverage.py
"""

import os
import sys

from run_transform import IMPORTANT_TABLES, RAW_PREFIX, _table_id_from_key, get_blob_container


def main():
    container = get_blob_container()

    landed_tables = {}  # table_id -> list of raw filenames landed for it
    for b in container.list_blobs(name_starts_with=RAW_PREFIX):
        if not b.name.endswith(".json"):
            continue
        table_id = _table_id_from_key(b.name)
        landed_tables.setdefault(table_id, []).append(b.name)

    present = sorted(t for t in IMPORTANT_TABLES if t in landed_tables)
    missing = sorted(t for t in IMPORTANT_TABLES if t not in landed_tables)

    print(f"IMPORTANT_TABLES: {len(IMPORTANT_TABLES)} table(s) required for transform/KPIs.\n")

    print(f"LANDED  ({len(present)}/{len(IMPORTANT_TABLES)}):")
    for t in present:
        # Multiple timestamped files can exist per table (each incremental
        # pull adds a new one) -- show the count and the most recent file.
        files = sorted(landed_tables[t])
        print(f"  [x] {t}  ({len(files)} file(s), latest: {files[-1].rsplit('/', 1)[-1]})")

    print(f"\nMISSING ({len(missing)}/{len(IMPORTANT_TABLES)}) -- fetch_cbs_tables.py hasn't landed these yet:")
    if missing:
        for t in missing:
            print(f"  [ ] {t}")
    else:
        print("  (none -- every required table has landed. Safe to run run_transform.py.)")

    if missing:
        print(
            f"\n{len(missing)} table(s) still missing. run_transform.py will still run fine on "
            f"whatever HAS landed -- it just won't have anything to do yet for the missing ones. "
            f"Re-run this script any time to check progress as fetch_cbs_tables.py works through "
            f"the full catalog."
        )
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
