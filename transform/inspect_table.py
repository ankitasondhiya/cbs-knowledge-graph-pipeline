"""
Print the REAL column list of a CBS table straight from CBS (DataProperties): key, title, unit, type.

Use it when a KPI is missing a measure and you need to know what CBS actually calls it, e.g.
    python inspect_table.py 81156NED
No Azure or Neo4j access needed -- only internet. Also runnable from GitHub Actions ("Inspect CBS table columns").
"""
import sys

import cbsodata


def main():
    if len(sys.argv) < 2:
        sys.exit("usage: python inspect_table.py TABLE_ID   (e.g. 81156NED)")
    table = sys.argv[1].strip()
    props = cbsodata.get_meta(table, "DataProperties")
    print(f"{table}: {len(props)} columns\n")
    print(f"{'Key':38} {'Type':18} {'Unit':16} Title")
    for p in props:
        print(f"{str(p.get('Key')):38} {str(p.get('Type')):18} {str(p.get('Unit') or ''):16} {p.get('Title')}")


if __name__ == "__main__":
    main()
