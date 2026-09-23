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

BRANCH_FIELDS = ("BedrijfstakkenBranchesSBI2008", "BedrijfstakkenBranchesSBI2025")
COUNTRY_FIELD = "Landen"


def _keep_top_level_branch(row: dict) -> bool:
    # Checks both the SBI 2008 and SBI 2025 field names -- a row has at
    # most one of the two (CBS tables use one scheme or the other, never
    # both), so this is safe regardless of which generation a table is on.
    for field in BRANCH_FIELDS:
        branch = row.get(field)
        if branch is not None:
            return bool(TOP_LEVEL_BRANCH_RE.match(branch))
    return False


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
    # SBI 2025 tables: 86280NED is the direct successor to 81589NED (same
    # "Bedrijven; bedrijfstak" shape, branch-only dimension), so it almost
    # certainly has the same many-hundred-branch row-count problem --
    # scoped preemptively on that basis. 86281NED/86282NED/86285NED/
    # 86341NED/86342NED are NOT listed yet -- their real row counts
    # haven't been checked against actual landed data, so they pass
    # through untouched under --scope poc until confirmed one way or the
    # other (same principle as 83827NED earlier: don't scope a table
    # whose real volume hasn't actually been checked).
    "86280NED": _keep_top_level_branch,
    # Conjunctuurenquête (business cycle survey) tables: checked their real
    # DataProperties metadata -- 85609NED has 145 measure columns per row
    # (far more than any other table here), branch-dimensioned across
    # roughly 400+ SBI codes at full granularity. Even after
    # fetch_cbs_tables.py's latest-period-only filter, that's rows x 145
    # measures -- scoped to the same 21 top-level sections as everything
    # else. 85611NED adds a second dimension (bedrijfsgrootte) on top of
    # branch, so its row count multiplies further -- scoped for the same
    # reason. 85610NED (region-only, no branch) and 85612NED/85614NED/
    # 81234ned (branch-dimensioned but only 4 measure columns each) are
    # NOT listed -- their real volume is small enough not to need it.
    "85609NED": _keep_top_level_branch,
    "85611NED": _keep_top_level_branch,
    # 86165NED (neighbourhood demographics): confirmed against real CBS
    # DataProperties -- 59 measure columns, 18,495 rows spanning national
    # (NL00) / municipality (GM...) / district (WK...) / neighbourhood
    # (BU...) levels. Loaded in full that's ~18,495 x 59 x 3 relationships
    # (dataset + variable + region links) -- roughly 3.2 million, nowhere
    # close to fitting Free tier even with headroom to spare on every other
    # table. TIGHTENED 2026-09-23 (Aura Free hit 399,716/400,000 -- 99.9%
    # full -- on the previous scope): now keeps every municipality + the
    # national total ONLY (~343 rows, a genuine nationwide comparison --
    # this is the actual granularity the regional KPIs operate at). The
    # Buurt -> Wijk -> Gemeente breakdown for a flagship city has been
    # dropped entirely -- it was never KPI data, just a nicety for the
    # region-hierarchy demo query in load/README.md. Re-add
    # FLAGSHIP_GEMEENTE_CODES below once you're on a paid tier and have
    # headroom for it again.
    "86165NED": lambda row: _keep_demographics_subset(row),
}


def _keep_demographics_subset(row: dict) -> bool:
    code = row.get("Codering_3")
    if not code:
        return False
    # NL = national total, GM = municipality -- the actual granularity
    # the regional KPIs (Addressable Market Size, Business Density) use.
    # WK (district) / BU (neighbourhood) rows are dropped entirely now --
    # see the SCOPE_TABLES comment above.
    return code.startswith("NL") or code.startswith("GM")


def apply_scope(rows: list, table_id: str, scope: str | None) -> list:
    if scope != "poc":
        return rows
    keep_fn = SCOPE_TABLES.get(table_id)
    if keep_fn is None:
        return rows  # not one of the tables that needs scoping -- pass through in full
    return [r for r in rows if keep_fn(r)]
