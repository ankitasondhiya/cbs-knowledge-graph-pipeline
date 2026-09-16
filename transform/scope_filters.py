"""
Optional row-level scoping for the POC / Free-tier Neo4j load.

Nothing here changes what gets fetched or what validated/ contains by
default -- run_transform.py only applies these filters when explicitly
asked (`--scope poc`), so the full, faithful dataset is always still
produced and available for whenever you move to AuraDB Professional.

Why filter at THIS stage rather than at load time: by the time data is
N-Triples in validated/, row boundaries are gone -- there's no clean way
to say "drop this business row" from a flat triple stream. Here, rows are
still plain dicts, so a filter is just a predicate.

Grounded in your real landed data (not guessed): checked actual distinct
values in 81589NED and 84765NED before picking anything below.

Branch scoping (BedrijfstakkenBranchesSBI2008): CBS tags every branch
value with its position in the SBI hierarchy. A single-letter code
followed by a space ("A Landbouw, bosbouw en visserij", "G Handel", ...)
is one of the 21 broadest, most recognizable sections -- the same
categories a business stakeholder already thinks in. Everything else is a
progressively finer subdivision (01, 011, 0111, 01131, ...) -- CBS's
81589NED alone has 1,487 distinct branch values at that finest level,
which is what inflates 81578NED to 163,520 rows. Keeping only the 21
top-level sections collapses every branch-dimensioned table by 1-2 orders
of magnitude while keeping the same 21 industries recognizable across
every dataset that uses them -- which is itself a better story for a
demo, not just a smaller one.

Country scoping (Landen, used by 84765NED): keeps a curated list of NL's
actual major trading partners plus the grand-total row, instead of all
101 distinct values (many of which are continent/region aggregates like
"Europa (exclusief Nederland)" that would double-count against the
country-level rows).
"""

import re

TOP_LEVEL_BRANCH_RE = re.compile(r"^[A-Z] ")  # e.g. "G Handel" -- CBS SBI section codes are always "<letter> <name>"

POC_COUNTRIES = {
    "Totaal landen",       # grand total -- keep as the baseline comparison point
    "Duitsland",           # Germany -- NL's #1 trading partner
    "België",              # Belgium
    "Verenigde Staten",    # United States
    "China",
    "Verenigd Koninkrijk", # United Kingdom
    "Frankrijk",           # France
    "Japan",
    "Rusland",             # Russia
}

BRANCH_FIELD = "BedrijfstakkenBranchesSBI2008"
COUNTRY_FIELD = "Landen"


def _keep_top_level_branch(row: dict) -> bool:
    branch = row.get(BRANCH_FIELD)
    return branch is not None and bool(TOP_LEVEL_BRANCH_RE.match(branch))


def _keep_poc_country(row: dict) -> bool:
    return row.get(COUNTRY_FIELD) in POC_COUNTRIES


# Deliberately opt-in per table_id, NOT a blanket "any row with this field
# gets filtered" rule. Learned the hard way: 83827NED (wholesale turnover)
# also has BedrijfstakkenBranchesSBI2008, but CBS only ever reports it at
# the "46 Groothandel..." 2-digit division level, never at a top-level
# section -- a blanket top-level-only filter silently deleted all 24 of
# its rows. Only tables that actually have a row-count problem (checked
# against real landed data, not guessed) are listed here; everything else
# passes through untouched even under --scope poc, since it's already
# small enough not to need it.
SCOPE_TABLES = {
    "81578NED": _keep_top_level_branch,  # 163,520 rows -> the big one
    "81589NED": _keep_top_level_branch,  # 1,487 rows (1,487 distinct branches alone)
    "83631NED": _keep_top_level_branch,  # 8,514 rows
    "83635NED": _keep_top_level_branch,  # 8,385 rows
    "84765NED": _keep_poc_country,       # 4,141 rows, 101 distinct countries
}


def apply_scope(rows: list, table_id: str, scope: str | None) -> list:
    if scope != "poc":
        return rows
    keep_fn = SCOPE_TABLES.get(table_id)
    if keep_fn is None:
        return rows  # not one of the tables that needs scoping -- pass through in full
    return [r for r in rows if keep_fn(r)]
