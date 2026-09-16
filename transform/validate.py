"""
Runs the graph produced by rdf_mapper.py through the SHACL shapes in
shacl_shapes.ttl. This is the Validation Gate box, as a standalone,
testable function -- run_transform.py calls this before anything is
written to the validated/ output.
"""

import os

from pyshacl import validate
from rdflib import Graph

# Resolve relative to this file, not the caller's working directory --
# this makes the script safe to invoke as `python transform/run_transform.py`
# from a repo root (e.g. in GitHub Actions) as well as from inside transform/.
SHAPES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shacl_shapes.ttl")


def run_validation(data_graph: Graph):
    """
    Returns (conforms: bool, report_text: str).
    conforms=False means: do not load this batch. Fix the mapping or
    the source data, don't force it through.
    """
    shapes_graph = Graph().parse(SHAPES_PATH, format="turtle")
    conforms, results_graph, results_text = validate(
        data_graph,
        shacl_graph=shapes_graph,
        inference="none",
        abort_on_first=False,
        allow_infos=True,
        allow_warnings=True,
    )
    return conforms, results_text
