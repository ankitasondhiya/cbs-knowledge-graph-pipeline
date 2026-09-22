"""
Stage 1 of the CBS -> Knowledge Graph pipeline: Extraction Worker.

Pulls a curated set of CBS StatLine tables -- neighbourhood demographics
plus business/enterprise (B2B-relevant) tables -- and lands each one
untouched in a "raw landing zone", exactly the Ingestion stage from the
breadboard. Scope is all of the Netherlands (no region filter). Tables
that carry a time dimension ("Perioden") are fetched for their latest
period only -- history accumulates naturally across runs via the
timestamped files instead of re-pulling decades of quarters every time.

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

# Curated CBS tables. Add/remove entries here to change what gets pulled --
# nothing else in this script needs to know about specific tables.
TABLES = {
    "86165NED": "Kerncijfers wijken en buurten (neighbourhood demographics)",
    "81589NED": "Bedrijven; bedrijfstak (business counts by industry)",
    "81588NED": "Bedrijven; bedrijfsgrootte en rechtsvorm (size & legal form)",
    "83148NED": "Bedrijven; oprichtingen (business starts)",
    "83149NED": "Bedrijven; opheffingen (business closures)",
    "83147NED": "Bedrijven; fusies en overnames (mergers & acquisitions)",
    "83827NED": "Groothandelsbedrijven; omzet (wholesale turnover -- core B2B trade)",
    "85828NED": "Handel en diensten; omzet en productie (trade & services turnover, incl. zakelijke dienstverlening via its SBI branch dimension)",
    "85958NED": "Invoer en uitvoer volgens eigendomsoverdracht (international trade in goods, headline)",
    "84765NED": "Internationale handel; invoer en uitvoer van diensten naar land (international trade in services, by country -- direct B2B)",
    "81578NED": "Vestigingen van bedrijven; bedrijfstak, regio (business establishments by sector & region)",
    "83631NED": "Vestigingen van bedrijven; oprichtingen, bedrijfstak, regio (regional business openings)",
    "83635NED": "Vestigingen van bedrijven; opheffingen, bedrijfstak, regio (regional business closures)",
    # SBI 2025 -- CBS's new industry classification, replacing SBI 2008.
    # Added ALONGSIDE the SBI 2008 tables above (not replacing them): CBS
    # itself is still mid-transition, and the transform stage already
    # recognizes both schemes' branch field and merges matching top-level
    # industry labels onto the same graph nodes (see glossary.py).
    "86280NED": "Bedrijven; bedrijfstak (SBI 2025) (business counts by industry -- SBI 2025 successor to 81589NED)",
    "86281NED": "Bedrijven; bedrijfsgrootte en rechtsvorm (SBI 2025) (size & legal form -- SBI 2025 successor to 81588NED)",
    "86282NED": "Bedrijven; opheffingen, bedrijfsgrootte, rechtsvorm, bedrijfstak (SBI 2025) (business closures -- SBI 2025 successor to 83149NED)",
    # Bankruptcies -- a more standard, legally-defined "business distress"
    # signal than opheffingen (closures), which includes voluntary exits too.
    "82242NED": "Faillissementen; kerncijfers (bankruptcies -- headline figures)",
    "82522NED": "Faillissementen; bedrijven en instellingen, regio (bankruptcies by region)",
    "82244NED": "Faillissementen; bedrijven en instellingen, SBI 2008 (bankruptcies by industry sector)",
    # Business cycle survey (Conjunctuurenquête) -- forward-looking sentiment,
    # not just historical counts.
    "85610NED": "Conjunctuurenquête Nederland; regio (business cycle survey by region)",
    "85609NED": "Conjunctuurenquête Nederland; bedrijfstakken (SBI 2008) (business cycle survey by industry)",
    "85611NED": "Conjunctuurenquête Nederland; bedrijfsgrootte, bedrijfstakken (SBI 2008) (business cycle survey by size & industry)",
    # Entrepreneur confidence -- a leading indicator, distinct from the
    # historical/structural tables above.
    "85612NED": "Ondernemersvertrouwen; bedrijfstakken (SBI 2008) (entrepreneur confidence by industry)",
    "85614NED": "Ondernemersvertrouwen; regio (entrepreneur confidence by region)",
    # Financial performance by region & size -- not represented anywhere
    # else in this table set.
    "86413NED": "Bedrijfsleven; financiële gegevens, regio, bedrijfsgrootte (business financial data by region & size)",
    # Foreign-owned businesses in NL -- an internationalization angle
    # distinct from the trade tables above.
    "85821NED": "Buitenlandse zeggenschap bedrijven in Nederland; kerncijfers, bedrijfstak (foreign-controlled businesses in NL, by industry)",
    # Producer/industry sentiment -- narrower than Conjunctuurenquête,
    # industry-only.
    "81234ned": "Producentenvertrouwen; stemmingsindicator van de industrie, bedrijfstak (producer confidence, by industry)",
}

LANDING_ZONE = "./landing_zone"
STATE_FILE = "./pipeline_state.json"

AZURE_STORAGE_CONNECTION_STRING = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
AZURE_STORAGE_CONTAINER = os.environ.get("AZURE_STORAGE_CONTAINER")


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


def main():
    os.makedirs(LANDING_ZONE, exist_ok=True)
    state = load_state()

    any_changed = False
    for table_id, description in TABLES.items():
        if pull_one_table(table_id, description, state):
            any_changed = True
        # Persist state after each table so a later failure doesn't
        # forget the tables that already succeeded this run.
        save_state(state)

    if not any_changed:
        print("No tables changed since last run -- incremental load working as intended.")


if __name__ == "__main__":
    main()
