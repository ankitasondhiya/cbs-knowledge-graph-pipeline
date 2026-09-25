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

BRANCH_FIELDS = ("BedrijfstakkenBranchesSBI2008", "BedrijfstakkenBranchesSBI2025", "SectorBranchesSIC2008", "SBI2008", "SBI2025")
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
    # scoped preemptively on that basis. 86285NED (fast-growers, swapped in
    # 2026-09-23 for retired 48051NED) confirmed via real DataProperties to
    # have the exact same shape -- a single BedrijfstakkenBranchesSBI2025
    # dimension at full SBI 2025 granularity -- so it's scoped on the same
    # basis (hit in practice: 401,298/400,000 on Aura Free with it
    # unscoped). 86281NED/86282NED/86341NED/86342NED are still NOT listed --
    # their real row counts haven't been checked against actual landed
    # data, so they pass through untouched under --scope poc until
    # confirmed one way or the other (same principle as 83827NED earlier:
    # don't scope a table whose real volume hasn't actually been checked).
    "86280NED": _keep_top_level_branch,
    "86285NED": _keep_top_level_branch,
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
    # KPI gap-analysis tables (added 2026-09): checked their real
    # DataProperties metadata. 84466NED (Zelfstandigen; inkomen, vermogen,
    # kenmerken -- Succession Pressure Index) is branch-dimensioned
    # (BedrijfstakkenBranchesSBI2008) AND multiplies further across
    # TypeZelfstandige x Geslacht x Kenmerken x Perioden -- confirmed in
    # practice to blow well past 625k+ triples for this table alone (Aura
    # Free hit 401,412/400,000 mid-load on it, 2026-09-23). 86119NED
    # (digital foundation / AI adoption survey -- Digital Foundation Gap +
    # AI Depth Ratio KPIs) is branch- AND company-size-dimensioned with
    # 229 measure columns per row, by far the largest measure count of any
    # table here -- scoped preemptively on that basis, same principle as
    # 85609NED above. 80567NED (Vacatures; vacaturegraad -- Labour Scarcity
    # Pressure) is also branch-dimensioned but has only 1 measure column
    # (Vacaturegraad_1) -- left unscoped, same reasoning as
    # 85612NED/85614NED/81234ned above.
    "84466NED": _keep_top_level_branch,
    "86119NED": _keep_top_level_branch,
    # Full audit of every still-unchecked IMPORTANT_TABLES entry (2026-09-23,
    # after 84466NED/86119NED forced a 3rd cap hit) -- checked each one's real
    # DataProperties AND, for the branch-dimensioned ones, the actual
    # BedrijfstakkenBranchesSBI* category list (not just measure count) to
    # rule out the 83827NED-style exception (branch field present but CBS
    # only ever reports it at a coarse division level, where a top-level
    # filter would silently delete every row).
    # 81588NED (Bedrijven; bedrijfsgrootte en rechtsvorm, 32 measures) and its
    # SBI2025 successor 86281NED (21 measures): confirmed ~130-150 branch
    # codes at full granularity, real top-level rows present -- scoped.
    "81588NED": _keep_top_level_branch,
    "86281NED": _keep_top_level_branch,
    # 83148NED (business starts) and 83149NED (business closures), plus their
    # SBI2025 successor 86282NED: confirmed ~130-180 branch codes at full
    # granularity -- scoped.
    "83148NED": _keep_top_level_branch,
    "83149NED": _keep_top_level_branch,
    "86282NED": _keep_top_level_branch,
    # 85828NED (Handel en diensten; omzet en productie, 23 measures):
    # confirmed 74 branch codes with real top-level rows present (e.g. "G
    # Handel") -- unlike 83827NED's coarse-only exception, this one behaves
    # normally -- scoped.
    "85828NED": _keep_top_level_branch,
    # 82244NED (Faillissementen; SBI 2008): TypeGefailleerde + branch, only 1
    # measure but branch-dimensioned at full granularity -- scoped for
    # consistency (cheap insurance, low cost either way given 1 measure).
    "82244NED": _keep_top_level_branch,
    # 85821NED (Buitenlandse zeggenschap bedrijven in Nederland): branch AND
    # LandVanZeggenschap (country of control) dimensions, 12 measures --
    # applying the branch-level trim here only narrows ONE of its two
    # multiplying dimensions (LandVanZeggenschap has no scoping predicate
    # yet -- different field name than 84765NED's "Landen"). If a future
    # load still overflows and the log points at this table, that's the
    # next place to add a country-of-control filter, not guessed now.
    "85821NED": _keep_top_level_branch,
    # Checked and confirmed FINE unscoped (real DataProperties, low
    # multiplier -- same reasoning as 85610NED/85612NED/85614NED/81234ned):
    # 82242NED (TypeGefailleerde only, 1 measure, no branch/region),
    # 82522NED (TypeGefailleerde + RegioS, 1 measure),
    # 86413NED (Bedrijfsgrootte + RegioS, 2 measures).
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


# ---------------------------------------------------------------------
# KPI measure trim (poc only). Row filters alone weren't enough: a 10-table
# KPI dry run came to ~1.72M relationship ops against Aura Free's 400k cap,
# almost entirely from WIDE tables -- every measure column in a row becomes
# its own Observation node with 3-6 relationships. 86165NED has ~118 measure
# columns and the KPIs use 2; 86119NED has 229 and the KPIs use 2.
# For these tables, poc keeps only the measure columns the KPI dashboard
# actually reads (plus a few curated ones for drill-down), and only the
# LATEST period each kept measure was published in -- the KPIs only ever
# show the latest value anyway. Matched on the raw CBS field name.
# Full-scope output (no --scope) is untouched and keeps everything.
POC_MEASURES = {
    # KPI 9 (+ a few curated demographics for drill-down)
    "86165NED": re.compile(r"^(AantalInwoners|BedrijfsvestigingenTotaal|HuishoudensTotaal|GemiddeldeWOZWaardeVanWoningen|GemiddeldInkomenPerInwoner)_\d+$"),
    # KPI 2 (ERP) + KPI 3 (AI)
    "86119NED": re.compile(r"(ERP|Erp|EnterpriseResource|AIGebruikt|KunstmatigeIntelligentie)"),
    # KPI 4 (number of self-employed). Only Zelfstandigen_1 -- the table also
    # has Zelfstandigen_11 and ZelfstandigenMetBedrijfsvermogen_8/_18 (other
    # topic groups whose auto-labels look identical), which the KPI must not mix in.
    "84466NED": re.compile(r"^Zelfstandigen_1$"),
    # KPI 8. Confirmed from the real transform notes (2026-09-25): this
    # regional Conjunctuurenquete has no single "Ondernemersvertrouwen"
    # column -- its headline indicators are the Saldo* balances (% positive
    # minus % negative answers), e.g. SaldoEconomischKlimaatKomende3Mnd_133.
    # Keep only those 7 balances; the ~138 per-answer percentage columns
    # (is verbeterd / gelijk gebleven / verslechterd ...) are dropped.
    "85610NED": re.compile(r"^Saldo"),
}
_MEASURE_KEY_RE = re.compile(r"_\d+$")


def _trim_measures(rows: list, table_id: str) -> list:
    pat = POC_MEASURES.get(table_id)
    if pat is None or not rows:
        return rows
    # Suffixed keys that glossary.py declares as dimensions/labels (e.g.
    # 86165NED's Codering_3 region code, Gemeentenaam_1) are NOT measures
    # and must survive the trim -- the region link depends on Codering_3.
    from glossary import DIMENSION_FIELDS
    measure_keys = {k for r in rows[:200] for k, v in r.items()
                    if _MEASURE_KEY_RE.search(k) and k not in DIMENSION_FIELDS
                    and (v is None or isinstance(v, (int, float)))}
    keep = {k for k in measure_keys if pat.search(k)}
    if not keep:
        # Safety valve: never silently empty a KPI table because CBS worded
        # a column differently than expected -- keep everything and say so.
        print(f"  WARNING: poc measure trim for {table_id} matched no columns "
              f"(pattern {pat.pattern!r}); keeping all {len(measure_keys)} measures.")
        return rows
    drop = measure_keys - keep
    # Latest period in which each kept measure actually has a value.
    latest = {}
    for r in rows:
        p = r.get("Perioden")
        for k in keep:
            if r.get(k) is not None and (k not in latest or (p or "") > (latest[k] or "")):
                latest[k] = p
    out = []
    for r in rows:
        p = r.get("Perioden")
        live = [k for k in keep if r.get(k) is not None and latest.get(k) == p]
        if not live:
            continue
        nr = {k: v for k, v in r.items() if k not in drop}
        for k in keep:
            if k not in live:
                nr[k] = None  # rdf_mapper skips None -> no Observation
        out.append(nr)
    print(f"  poc measure trim for {table_id}: kept {sorted(keep)} "
          f"(dropped {len(drop)} other measures), latest period(s) {sorted(set(map(str, latest.values())))}, "
          f"{len(out)}/{len(rows)} rows")
    return out


# ---------------------------------------------------------------------
# Breakdown trim (poc only). 84466NED is a deep cube: branch x several
# person-characteristic breakdowns, 28,665 rows for ONE year even after the
# top-level-branch filter -- ~650k of a 688k relationship dry run on its own.
# KPI 4 only ever reads (a) the row where every extra breakdown is at its
# Totaal member, and (b) rows where exactly ONE breakdown is an age band
# 55+/65+ and the rest are Totaal. Keep only those rows. Same Totaal rule
# as the dashboard's RX_TOTAL, so the transform keeps exactly what the KPI
# query will select.
_TOTAL_RE = re.compile(r"^(totaal|alle |nederland|mannen en vrouwen|beide geslachten)", re.I)
POC_BREAKDOWN = {
    # 55-65 and 65+ bands only -- NOT "15 tot 55 jaar" (which also contains "55 jaar").
    "84466NED": re.compile(r"(55 tot 65|55 jaar of ouder|55 jaar en ouder|65 jaar of ouder|65 jaar en ouder|65 tot \d+ jaar|75 jaar of ouder|75 jaar en ouder)", re.I),
}
_NON_BREAKDOWN_KEYS = {"ID", "Perioden", "Marges", "Seizoencorrectie"} | set(BRANCH_FIELDS)


def _trim_breakdowns(rows: list, table_id: str) -> list:
    part_re = POC_BREAKDOWN.get(table_id)
    if part_re is None or not rows:
        return rows
    dims = sorted({k for r in rows[:500] for k, v in r.items()
                   if k not in _NON_BREAKDOWN_KEYS and isinstance(v, str)
                   and not _MEASURE_KEY_RE.search(k)})
    measures = sorted({k for r in rows[:200] for k, v in r.items()
                       if _MEASURE_KEY_RE.search(k) and isinstance(v, (int, float))})

    # Pick each breakdown's "all members" value. First choice: a label that
    # reads like a total (Totaal..., Alle ...). CBS doesn't always word it
    # that way (the first real run of this on 84466NED found none), so the
    # fallback is structural: in a CBS cube the all-members value is the
    # aggregate of the others, so it has the LARGEST summed measure of any
    # value in that breakdown. This works regardless of wording.
    total_of = {}
    for d in dims:
        sums = {}
        for r in rows:
            v = r.get(d)
            if v is None:
                continue
            v = str(v).strip()
            sums[v] = sums.get(v, 0.0) + sum(float(r[m]) for m in measures if isinstance(r.get(m), (int, float)))
        labelled = [v for v in sums if _TOTAL_RE.match(v)]
        total_of[d] = labelled[0] if labelled else (max(sums, key=sums.get) if sums else None)
        vals = sorted(sums)
        how = "label" if labelled else "largest-sum"
        print(f"  {table_id} breakdown {d}: {len(vals)} values, total member = {total_of[d]!r} (by {how}); e.g. {vals[:15]}")

    out = []
    for r in rows:
        parts = [str(r[d]).strip() for d in dims
                 if r.get(d) is not None and str(r[d]).strip() != total_of[d]]
        if not parts or (len(parts) == 1 and part_re.search(parts[0])):
            # Make the chosen all-members value recognisable downstream: the
            # dashboard (RX_TOTAL) identifies totals by a leading "Totaal",
            # so a total found by largest-sum (e.g. plain "Zelfstandigen")
            # is relabelled "Totaal (Zelfstandigen)".
            r = dict(r)
            for d in dims:
                v = r.get(d)
                if v is not None and str(v).strip() == total_of[d] and not _TOTAL_RE.match(total_of[d]):
                    r[d] = f"Totaal ({total_of[d]})"
            out.append(r)
    n_total = sum(1 for r in out if all(r.get(d) is None or _TOTAL_RE.match(str(r[d]).strip()) for d in dims))
    n_age = len(out) - n_total
    if n_total == 0:
        print(f"  WARNING: breakdown trim for {table_id} found no all-total row; keeping untrimmed rows.")
        return rows
    if n_age == 0:
        print(f"  WARNING: breakdown trim for {table_id} found no 55+/65+ age rows (pattern {part_re.pattern!r}) -- "
              f"KPI 4 will be empty until the age wording is added; loading totals only to stay within the Aura cap.")
    print(f"  poc breakdown trim for {table_id}: kept {len(out)}/{len(rows)} rows ({n_total} total rows, {n_age} age-band rows)")
    return out


def apply_scope(rows: list, table_id: str, scope: str | None) -> list:
    if scope != "poc":
        return rows
    keep_fn = SCOPE_TABLES.get(table_id)
    if keep_fn is not None:
        rows = [r for r in rows if keep_fn(r)]
    rows = _trim_measures(rows, table_id)
    return _trim_breakdowns(rows, table_id)
