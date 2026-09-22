"""
Orchestrator for the Transform & Validate stage.

Pulls new raw files from the Azure Blob landing container (same
container/prefix fetch_cbs_tables.py already writes to --
landing_zone/<table>_<ts>.json), runs each through RDF Mapping ->
Glossary Alignment -> Validation Gate, and writes conforming graphs to a
"validated/" prefix in the same container, ready for the Incremental
Load Controller (stage 3). Anything that fails validation is logged and
skipped, with the exact SHACL reason -- see "How failures work" below.

Processes rows in BATCHES rather than building one giant in-memory graph
per table. Two of the 13 tables (81578NED: 163k+ rows, 86165NED: 18k+
rows x ~100 fields each) are large enough that doing it all at once is
genuinely slow and -- worse -- gives zero visible progress for minutes at
a time, which looks identical to "stuck" in a CI log. Batching fixes
both: bounded memory, and a progress line every batch so you can see it
actually working.

How failures work: each batch is validated independently. A validation
failure in one batch of, say, 2000 rows out of a 163,520-row table no
longer throws away the other 161,520 good rows -- only the bad batch is
skipped, logged with its exact reason, and everything else still gets
written. This is both more correct and faster than the original
all-or-nothing-per-file approach.

Reuses the exact same Azure env vars fetch_cbs_tables.py / your GitHub
Actions workflow already use -- no new secrets needed beyond the two
below (migrated from R2's four -- see README for the full variable list
and what each one is for):

    AZURE_STORAGE_CONNECTION_STRING
    AZURE_STORAGE_CONTAINER

Usage:
    python run_transform.py                 # process new files from Azure Blob
    python run_transform.py --local         # test against sample_landing/, no Azure/credentials needed
    pip install -r requirements.txt
"""

import argparse
import json
import os
import sys
import time

from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient, ContentSettings

from rdf_mapper import map_rows_to_graph
from scope_filters import apply_scope
from validate import run_validation

RAW_PREFIX = "landing_zone/"       # matches fetch_cbs_tables.py's upload key exactly
VALIDATED_PREFIX = "validated/"
REJECTED_PREFIX = "rejected/"
MANIFEST_KEY = "validated/_processed_manifest.json"

BATCH_SIZE = 2000  # rows per batch -- keeps memory bounded and progress visible on large tables


def get_blob_container():
    conn_str = os.environ.get("AZURE_STORAGE_CONNECTION_STRING")
    container_name = os.environ.get("AZURE_STORAGE_CONTAINER")
    if not all([conn_str, container_name]):
        sys.exit(
            "Missing Azure Blob config. Set AZURE_STORAGE_CONNECTION_STRING and "
            "AZURE_STORAGE_CONTAINER as environment variables -- the same ones "
            "fetch_cbs_tables.py uses. Never hardcode them here or commit them."
        )
    service = BlobServiceClient.from_connection_string(conn_str)
    return service.get_container_client(container_name)


def load_manifest(container, manifest_key: str = MANIFEST_KEY) -> set:
    try:
        data = container.download_blob(manifest_key).readall()
        return set(json.loads(data)["processed_keys"])
    except ResourceNotFoundError:
        return set()


def save_manifest(container, processed_keys: set, manifest_key: str = MANIFEST_KEY):
    body = json.dumps({"processed_keys": sorted(processed_keys)}, indent=2).encode()
    container.upload_blob(
        manifest_key, body, overwrite=True,
        content_settings=ContentSettings(content_type="application/json"),
    )


def process_payload(payload: dict, source_key: str, scope: str | None = None):
    """
    Runs one landed file through scope -> map -> validate, in batches,
    printing progress as it goes. Returns (nt_content_or_None, notes):
      - nt_content: the validated triples in N-Triples format (one line
        per triple -- unlike Turtle, batches can just be concatenated,
        which is what makes streaming this simple), or None if EVERY
        batch failed validation (or the scope filter dropped every row).
      - notes: a human-readable summary -- uncurated glossary fields
        seen, plus details of any batch that was rejected.

    scope=None (the default): every row is processed, exactly as before.
    scope="poc": rows are narrowed first via scope_filters.apply_scope --
    see that file for what's kept and why (top-level industry sections,
    a curated country list). This exists purely to fit a demo inside
    AuraDB Free's 200k node / 400k relationship cap; validated/ under the
    default (unscoped) run always still has everything.
    """
    table_id = payload["source_table"]
    rows = apply_scope(payload["rows"], table_id, scope)
    description = payload.get("description", "")
    pulled_at = payload["pulled_at"]
    run_id = source_key.replace("/", "_").replace(".json", "")

    if not rows:
        return None, "NOTE: --scope poc filtered out every row in this file -- nothing to map."

    total = len(rows)
    num_batches = max(1, (total + BATCH_SIZE - 1) // BATCH_SIZE)
    print(f"  {total} rows, processing in {num_batches} batch(es) of up to {BATCH_SIZE}...")

    nt_parts = []
    rejected_batches = []
    all_unverified = set()
    total_triples = 0
    started = time.monotonic()

    for i in range(0, total, BATCH_SIZE):
        batch = rows[i:i + BATCH_SIZE]
        graph, unverified = map_rows_to_graph(batch, table_id, description, run_id, pulled_at)
        all_unverified.update(unverified)

        conforms, report_text = run_validation(graph)
        if conforms:
            nt_parts.append(graph.serialize(format="nt"))
            total_triples += len(graph)
        else:
            batch_no = i // BATCH_SIZE + 1
            rejected_batches.append(f"Batch {batch_no} (rows {i}-{i + len(batch)}):\n{report_text[:800]}")

        done = min(i + BATCH_SIZE, total)
        elapsed = time.monotonic() - started
        print(f"    ...{done}/{total} rows ({elapsed:.0f}s elapsed)")

    notes_lines = []
    if all_unverified:
        notes_lines.append(f"NOTE: {len(all_unverified)} concept(s) used auto-fallback labels (not yet curated in glossary.py): {sorted(all_unverified)}")
    if rejected_batches:
        notes_lines.append(f"NOTE: {len(rejected_batches)}/{num_batches} batch(es) failed validation and were skipped (their rows are NOT in the output):")
        notes_lines.extend(rejected_batches)
    notes = "\n\n".join(notes_lines) if notes_lines else "All batches validated cleanly."

    if not nt_parts:
        return None, notes

    return "".join(nt_parts), notes


def _manifest_key(scope: str | None) -> str:
    # A separate manifest per scope -- otherwise a --scope poc run would
    # mark a raw file "processed" and a later FULL run would silently
    # skip it forever, thinking it already has output for it.
    return f"{VALIDATED_PREFIX}_processed_manifest_poc.json" if scope == "poc" else MANIFEST_KEY


def _out_suffix(scope: str | None) -> str:
    # Scoped and unscoped output live side by side in validated/ under
    # different names, rather than one overwriting the other.
    return ".poc.nt" if scope == "poc" else ".nt"


def run_azure(scope: str | None = None, force: bool = False):
    container = get_blob_container()
    manifest_key = _manifest_key(scope)
    processed = load_manifest(container, manifest_key)

    keys = [
        b.name for b in container.list_blobs(name_starts_with=RAW_PREFIX)
        if b.name.endswith(".json")
        # --force skips the "already processed" check. Needed because a
        # raw file gets marked processed here even when it produced NO
        # validated output (e.g. every batch failed SHACL) -- otherwise a
        # fix to rdf_mapper.py/shacl_shapes.ttl would have no new raw file
        # to re-run against, and "No new raw files to transform" would
        # print forever even though the fix was never actually applied to
        # that table's data.
        and (force or b.name not in processed)
    ]

    if not keys:
        print("No new raw files to transform. Landing zone is fully processed.")
        return

    label = f" (scope={scope})" if scope else ""
    print(f"Found {len(keys)} new raw file(s) to transform{label}.")
    for key in keys:
        print(f"Processing {key} ...")
        payload = json.loads(container.download_blob(key).readall())

        nt_content, notes = process_payload(payload, key, scope)
        base = os.path.basename(key).replace(".json", "")

        if nt_content is not None:
            out_key = f"{VALIDATED_PREFIX}{base}{_out_suffix(scope)}"
            container.upload_blob(
                out_key, nt_content.encode(), overwrite=True,
                content_settings=ContentSettings(content_type="application/n-triples"),
            )
            print(f"  VALIDATED -> {out_key}")
        else:
            print("  Nothing written to validated/ (all batches failed validation, or --scope poc dropped every row).")

        if "NOTE:" in notes:
            report_key = f"{REJECTED_PREFIX}{base}{_out_suffix(scope).replace('.nt', '')}.notes.txt"
            container.upload_blob(
                report_key, notes.encode(), overwrite=True,
                content_settings=ContentSettings(content_type="text/plain"),
            )
            print(f"  Notes (uncurated fields / rejected batches) -> {report_key}")

        processed.add(key)
        # Save the manifest after EVERY file, not just at the end -- so a
        # later failure (or a cancelled run) doesn't forget files that
        # already succeeded and force a wasteful full re-run.
        save_manifest(container, processed, manifest_key)

    print("Done. Manifest updated -- next run will skip everything already processed here.")


def run_local(scope: str | None = None):
    """Test mode: reads ./sample_landing/*.json, writes ./validated_output/*.nt. No Azure needed."""
    here = os.path.dirname(os.path.abspath(__file__))
    in_dir = os.path.join(here, "sample_landing")
    out_dir = os.path.join(here, "validated_output")
    os.makedirs(out_dir, exist_ok=True)

    files = [f for f in os.listdir(in_dir) if f.endswith(".json")]
    label = f" (scope={scope})" if scope else ""
    print(f"Found {len(files)} local sample file(s){label}.")
    for fname in files:
        with open(os.path.join(in_dir, fname)) as f:
            payload = json.load(f)

        print(f"Processing {fname} ...")
        nt_content, notes = process_payload(payload, fname, scope)

        if nt_content is not None:
            out_path = os.path.join(out_dir, fname.replace(".json", _out_suffix(scope)))
            with open(out_path, "w", encoding="utf-8") as out:
                out.write(nt_content)
            print(f"  VALIDATED -> {out_path}")
        else:
            print("  Nothing written (all batches failed validation, or --scope poc dropped every row).")

        print("  " + notes.split("\n\n")[0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="run against sample_landing/ instead of Azure Blob")
    parser.add_argument(
        "--scope", choices=["poc"], default=None,
        help="poc: narrow rows to top-level industry sections + major trading partners "
             "(see scope_filters.py) so the result fits AuraDB Free's node cap. "
             "Omit for the full, unscoped dataset (the default).",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Ignore the processed-files manifest -- reprocesses raw files even if "
             "already marked done. Use this after fixing rdf_mapper.py/shacl_shapes.ttl "
             "and re-running against a table that previously produced nothing (a raw "
             "file is marked processed here even when validation rejected every batch). "
             "Ignored with --local (no manifest there).",
    )
    args = parser.parse_args()

    if args.local:
        run_local(args.scope)
    else:
        run_azure(args.scope, args.force)
