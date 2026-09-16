"""
Orchestrator for the Transform & Validate stage.

Pulls new raw files from the R2 landing bucket (same bucket/prefix
fetch_cbs_tables.py already writes to -- landing_zone/<table>_<ts>.json),
runs each through RDF Mapping -> Glossary Alignment -> Validation Gate,
and writes conforming graphs to a "validated/" prefix in the same bucket,
ready for the Incremental Load Controller (stage 3). Anything that fails
validation goes to "rejected/" instead, with the SHACL report attached.

Reuses the exact same R2 env vars fetch_cbs_tables.py / your GitHub
Actions workflow already use -- no new secrets needed:

    R2_ACCOUNT_ID
    R2_ACCESS_KEY_ID
    R2_SECRET_ACCESS_KEY
    R2_BUCKET_NAME

Usage:
    python run_transform.py                 # process new files from R2
    python run_transform.py --local         # test against sample_landing/, no R2/credentials needed
    pip install -r requirements.txt
"""

import argparse
import json
import os
import sys

import boto3

from rdf_mapper import map_rows_to_graph
from validate import run_validation

RAW_PREFIX = "landing_zone/"       # matches fetch_cbs_tables.py's upload key exactly
VALIDATED_PREFIX = "validated/"
REJECTED_PREFIX = "rejected/"
MANIFEST_KEY = "validated/_processed_manifest.json"


def get_r2_client():
    account_id = os.environ.get("R2_ACCOUNT_ID")
    access_key = os.environ.get("R2_ACCESS_KEY_ID")
    secret_key = os.environ.get("R2_SECRET_ACCESS_KEY")
    bucket = os.environ.get("R2_BUCKET_NAME")
    if not all([account_id, access_key, secret_key, bucket]):
        sys.exit(
            "Missing R2 config. Set R2_ACCOUNT_ID, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
            "R2_BUCKET_NAME as environment variables -- the same ones fetch_cbs_tables.py uses. "
            "Never hardcode them here or commit them."
        )
    client = boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        region_name="auto",
    )
    return client, bucket


def load_manifest(client, bucket) -> set:
    try:
        obj = client.get_object(Bucket=bucket, Key=MANIFEST_KEY)
        return set(json.loads(obj["Body"].read())["processed_keys"])
    except client.exceptions.NoSuchKey:
        return set()


def save_manifest(client, bucket, processed_keys: set):
    body = json.dumps({"processed_keys": sorted(processed_keys)}, indent=2).encode()
    client.put_object(Bucket=bucket, Key=MANIFEST_KEY, Body=body, ContentType="application/json")


def process_payload(payload: dict, source_key: str):
    """Runs one landed file through map -> validate. Returns (graph_or_None, report_or_notes)."""
    rows = payload["rows"]
    table_id = payload["source_table"]
    description = payload.get("description", "")
    pulled_at = payload["pulled_at"]
    run_id = source_key.replace("/", "_").replace(".json", "")

    graph, unverified = map_rows_to_graph(rows, table_id, description, run_id, pulled_at)

    conforms, report_text = run_validation(graph)
    notes = report_text
    if unverified:
        notes = f"NOTE: {len(unverified)} concept(s) used auto-fallback labels (not yet curated in glossary.py): {unverified}\n\n{report_text}"

    if not conforms:
        return None, notes

    return graph, notes


def run_r2():
    client, bucket = get_r2_client()
    processed = load_manifest(client, bucket)

    resp = client.list_objects_v2(Bucket=bucket, Prefix=RAW_PREFIX)
    keys = [o["Key"] for o in resp.get("Contents", []) if o["Key"] not in processed and o["Key"].endswith(".json")]

    if not keys:
        print("No new raw files to transform. Landing zone is fully processed.")
        return

    print(f"Found {len(keys)} new raw file(s) to transform.")
    for key in keys:
        print(f"Processing {key} ...")
        obj = client.get_object(Bucket=bucket, Key=key)
        payload = json.loads(obj["Body"].read())

        graph, notes = process_payload(payload, key)
        base = os.path.basename(key).replace(".json", "")

        if graph is None:
            out_key = f"{REJECTED_PREFIX}{base}.report.txt"
            client.put_object(Bucket=bucket, Key=out_key, Body=notes.encode(), ContentType="text/plain")
            print(f"  REJECTED (failed SHACL validation) -> {out_key}")
        else:
            out_key = f"{VALIDATED_PREFIX}{base}.ttl"
            client.put_object(
                Bucket=bucket, Key=out_key,
                Body=graph.serialize(format="turtle").encode(),
                ContentType="text/turtle",
            )
            print(f"  VALIDATED ({len(graph)} triples) -> {out_key}")
            if "NOTE:" in notes:
                print("  " + notes.split("\n\n")[0])

        processed.add(key)

    save_manifest(client, bucket, processed)
    print("Manifest updated. Next run will skip everything already processed here.")


def run_local():
    """Test mode: reads ./sample_landing/*.json, writes ./validated_output/*.ttl. No R2 needed."""
    here = os.path.dirname(os.path.abspath(__file__))
    in_dir = os.path.join(here, "sample_landing")
    out_dir = os.path.join(here, "validated_output")
    os.makedirs(out_dir, exist_ok=True)

    files = [f for f in os.listdir(in_dir) if f.endswith(".json")]
    print(f"Found {len(files)} local sample file(s).")
    for fname in files:
        with open(os.path.join(in_dir, fname)) as f:
            payload = json.load(f)

        print(f"Processing {fname} ...")
        graph, notes = process_payload(payload, fname)

        if graph is None:
            print("  REJECTED (failed SHACL validation):")
            print(notes)
        else:
            out_path = os.path.join(out_dir, fname.replace(".json", ".ttl"))
            graph.serialize(destination=out_path, format="turtle")
            print(f"  VALIDATED ({len(graph)} triples) -> {out_path}")
            if "NOTE:" in notes:
                print("  " + notes.split("\n\n")[0])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--local", action="store_true", help="run against sample_landing/ instead of R2")
    args = parser.parse_args()

    if args.local:
        run_local()
    else:
        run_r2()
