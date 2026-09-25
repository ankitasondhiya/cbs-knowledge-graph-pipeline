"""
Glossary Alignment (Transform & Validate stage, box 2 of 4).

This is the ONE file a business/data-governance person should ever need to
open. Every entry here is what turns a raw CBS field name into the
searchable business glossary described in the semantic-layer design.

Two kinds of entries, because your 13 tables genuinely have two kinds of
columns:

1. DIMENSION_FIELDS -- columns that classify a row (which region, which
   industry branch, which period, which country...). These are handled
   generically by rdf_mapper.py; this dict just says what KIND of thing
   each raw field name represents, so the mapper knows which predicate
   and class to use.

2. GLOSSARY -- columns that hold an actual measured number. Keyed by
   (table_id, field_name) because the same suffix (e.g. "_1") means a
   completely different thing in each table.

Honest state of this file, field by field, verified against the REAL
field names in your landing_zone (not guessed): a curated "headline"
subset per table has confident labels/definitions below. Everything else
falls through to rdf_mapper's auto-fallback (a human-readable label
derived from the field name, tagged verified=False) so the pipeline
doesn't silently drop 90% of every table while this glossary is still
being filled in by hand. Call unmapped_fields(table_id, row) any time to
see exactly what's still running on the fallback for a given table.

Two tables (85828NED, 85958NED) have repeated column name patterns
(Ongecorrigeerd_1/_4/_6..., OorspronkelijkeReeks_1/_4/_6...) that can't
be reliably told apart from the field name alone -- CBS's DataProperties
metadata is the only reliable source for what each one means. Left
entirely to auto-fallback on purpose rather than guessed.
"""

import re


# Non-measure columns that classify a row rather than measure something.
# short_name is what rdf_mapper.py uses to build the predicate/class names.
DIMENSION_FIELDS = {
    "ID": {"short_name": "row_id", "skip": True},  # CBS's internal row counter -- not meaningful data
    "WijkenEnBuurten": {"short_name": "region", "skip": True},  # handled specially: municipal GM/WK/BU hierarchy
    "Gemeentenaam_1": {"short_name": "region_label", "skip": True},
    "SoortRegio_2": {"short_name": "region_type_label", "skip": True},
    "Codering_3": {"short_name": "region_code_alt", "skip": True},
    "IndelingswijzigingGemeenteWijkBuurt_4": {"short_name": "boundary_change_flag", "skip": True},
    "IndelingswijzigingWijkenEnBuurten_4": {"short_name": "boundary_change_flag", "skip": True},
    "BedrijfstakkenBranchesSBI2008": {"short_name": "branch", "class": "Branch", "predicate": "branch"},
    # SBI 2025 replaces SBI 2008 as CBS's industry classification, and new
    # tables (86280NED, 86281NED, 86282NED, ...) use this field name
    # instead. Deliberately mapped to the SAME short_name/class/predicate
    # as SBI 2008 -- CBS's top-level section labels ("A Landbouw, bosbouw
    # en visserij", "G Handel", ...) are textually identical between the
    # two schemes (confirmed against CBS's real SBI2025 code list), and
    # dimension_value_uri() derives node URIs from that label text. So an
    # SBI2025 table and an SBI2008 table both referencing "G Handel" land
    # on the exact same Branch node -- old and new tables interlink
    # automatically rather than becoming two disconnected taxonomies.
    # Below the top level, SBI2025 does reorganize some finer-grained
    # codes -- not a concern here since --scope poc only keeps top-level
    # sections anyway (see scope_filters.py).
    "BedrijfstakkenBranchesSBI2025": {"short_name": "branch", "class": "Branch", "predicate": "branch"},
    "RegioS": {"short_name": "regio_category", "class": "RegioCategory", "predicate": "regioCategory"},
    # IMPORTANT: RegioS is NOT the same thing as WijkenEnBuurten. On the
    # business tables it's a national/foreign/unclassified breakdown
    # (e.g. "Nederland, Buitenland, Niet in te delen"), not a municipal
    # code -- do not merge it into the Buurt/Wijk/Gemeente hierarchy.
    "Perioden": {"short_name": "period", "literal": True},  # kept as a plain literal on the Observation, not a node
    "Diensten": {"short_name": "service_category", "class": "ServiceCategory", "predicate": "serviceCategory"},
    "Landen": {"short_name": "country", "class": "Country", "predicate": "country"},
    "InEnUitvoer": {"short_name": "trade_direction", "class": "TradeDirection", "predicate": "tradeDirection"},
    # New dimension fields seen on the bankruptcy/survey tables added for
    # the broader B2B set.
    "TypeGefailleerde": {"short_name": "bankruptcy_party_type", "skip": True},  # 82242NED/82522NED/82244NED: company vs. sole proprietor etc. -- still skipped, secondary breakdown, not central to the industry/region story these tables were added for.
    # Marges and Seizoencorrectie used to be skipped entirely (silently
    # dropped) -- promoted to literal properties on every Observation
    # instead, closing the "status/seasonal-adjustment flag" gap flagged
    # in the KPI gap analysis. This matters most for the survey tables
    # (85610NED/85609NED/85611NED/85612NED/85614NED/81234ned) and for
    # 80567NED's vacancy rate, which the KPI doc explicitly warned is NOT
    # seasonally adjusted -- without this flag surviving into the graph, a
    # naive quarter-over-quarter comparison downstream would look like a
    # real market shift when it might just be seasonal noise.
    #
    # Stored as the RAW CBS value (whatever string Seizoencorrectie/Marges
    # actually contains for a given row), not pre-interpreted into a clean
    # boolean -- the exact encoding (e.g. "Gecorrigeerde cijfers" vs a
    # numeric code) hasn't been confirmed against real landed rows for
    # these tables yet. Once they've landed, check a real sample and
    # tighten this into a proper seasonallyAdjusted: true/false if the
    # encoding turns out to be a clean two-value field -- don't guess it
    # here first.
    "Marges": {"short_name": "marginType", "literal": True},  # survey tables (Conjunctuurenquête, Ondernemersvertrouwen, Producentenvertrouwen): margin variant, raw CBS value
    "Seizoencorrectie": {"short_name": "seasonalAdjustment", "literal": True},  # same survey tables + 80567NED-style series: raw CBS value, not yet boolean-verified
}

GLOSSARY = {
    # ---- 86165NED: Kerncijfers wijken en buurten ----
    ("86165NED", "AantalInwoners_5"): {
        "slug": "population-count", "label_en": "Population count", "label_nl": "Aantal inwoners",
        "definition_en": "Total number of registered residents in the region.",
        "unit": "person", "topic": "demographics", "verified": True,
    },
    ("86165NED", "HuishoudensTotaal_29"): {
        "slug": "total-households", "label_en": "Total households", "label_nl": "Huishoudens totaal",
        "definition_en": "Total number of private households in the region.",
        "unit": "household", "topic": "demographics", "verified": True,
    },
    ("86165NED", "GemiddeldeWOZWaardeVanWoningen_39"): {
        "slug": "avg-dwelling-value", "label_en": "Average dwelling value (WOZ)", "label_nl": "Gemiddelde WOZ-waarde woningen",
        "definition_en": "Average assessed property value (WOZ) per dwelling, in thousand euros.",
        "unit": "thousand-eur", "topic": "housing", "verified": True,
    },
    ("86165NED", "GemiddeldInkomenPerInwoner_78"): {
        "slug": "avg-income-per-resident", "label_en": "Average income per resident", "label_nl": "Gemiddeld inkomen per inwoner",
        "definition_en": "Average standardised income per resident, in thousand euros.",
        "unit": "thousand-eur", "topic": "income", "verified": True,
    },
    ("86165NED", "BedrijfsvestigingenTotaal_95"): {
        "slug": "business-establishments-total", "label_en": "Total business establishments", "label_nl": "Bedrijfsvestigingen totaal",
        "definition_en": "Total number of business establishments physically located in the region.",
        "unit": "establishment", "topic": "business", "verified": True,
    },

    # ---- 81589NED / 81588NED: Bedrijven; bedrijfstak / bedrijfsgrootte ----
    ("81589NED", "TotaalBedrijven_1"): {
        "slug": "total-businesses-by-industry", "label_en": "Total businesses (by industry)", "label_nl": "Bedrijven totaal",
        "definition_en": "Total number of businesses registered in this industry branch.",
        "unit": "business", "topic": "business", "verified": True,
    },
    ("81588NED", "TotaalBedrijven_1"): {
        "slug": "total-businesses-by-size", "label_en": "Total businesses (by size/legal form)", "label_nl": "Bedrijven totaal",
        "definition_en": "Total number of businesses registered, broken down by size class and legal form.",
        "unit": "business", "topic": "business", "verified": True,
    },
    ("81588NED", "TotaalRechtspersonen_26"): {
        "slug": "total-legal-entities", "label_en": "Total legal entities", "label_nl": "Rechtspersonen totaal",
        "definition_en": "Total number of businesses structured as a legal entity (BV, NV, foundation, etc.), as opposed to a sole proprietorship.",
        "unit": "business", "topic": "business", "verified": True,
    },

    # ---- 83148NED / 83149NED / 83147NED: business starts / closures / M&A ----
    ("83148NED", "TotaalOprichtingenVanBedrijven_1"): {
        "slug": "business-formations", "label_en": "Business formations (starts)", "label_nl": "Oprichtingen van bedrijven",
        "definition_en": "Total number of new businesses started in the period.",
        "unit": "business", "topic": "business-dynamics", "verified": True,
    },
    ("83149NED", "TotaalOpheffingenVanBedrijven_1"): {
        "slug": "business-closures", "label_en": "Business closures", "label_nl": "Opheffingen van bedrijven",
        "definition_en": "Total number of businesses that ceased operating in the period.",
        "unit": "business", "topic": "business-dynamics", "verified": True,
    },
    ("83147NED", "TotaalFusiesEnOvernames_1"): {
        "slug": "mergers-and-acquisitions", "label_en": "Mergers & acquisitions", "label_nl": "Fusies en overnames",
        "definition_en": "Total number of mergers and acquisitions involving businesses in the period.",
        "unit": "event", "topic": "business-dynamics", "verified": True,
    },

    # ---- 81578NED / 83631NED / 83635NED: establishments by sector & region ----
    ("81578NED", "Vestigingen_1"): {
        "slug": "business-establishments", "label_en": "Business establishments", "label_nl": "Vestigingen van bedrijven",
        "definition_en": "Number of physical business establishments (a business can have several).",
        "unit": "establishment", "topic": "business", "verified": True,
    },
    ("83631NED", "OprichtingenVanVestigingen_1"): {
        "slug": "establishment-formations", "label_en": "Establishment formations", "label_nl": "Oprichtingen van vestigingen",
        "definition_en": "Number of new business establishments opened in the period.",
        "unit": "establishment", "topic": "business-dynamics", "verified": True,
    },
    ("83635NED", "OpheffingenVanVestigingen_1"): {
        "slug": "establishment-closures", "label_en": "Establishment closures", "label_nl": "Opheffingen van vestigingen",
        "definition_en": "Number of business establishments closed in the period.",
        "unit": "establishment", "topic": "business-dynamics", "verified": True,
    },

    # ---- 83827NED: wholesale turnover ----
    ("83827NED", "IndexcijfersOmzet_1"): {
        "slug": "wholesale-turnover-index", "label_en": "Wholesale turnover index", "label_nl": "Indexcijfers omzet",
        "definition_en": "Turnover index for wholesale trade (base-year = 100).",
        "unit": "index", "topic": "trade", "verified": True,
    },
    ("83827NED", "OmzetontwikkelingTOVEenJaarEerder_2"): {
        "slug": "wholesale-turnover-yoy-growth", "label_en": "Wholesale turnover growth (y/y)", "label_nl": "Omzetontwikkeling t.o.v. een jaar eerder",
        "definition_en": "Year-on-year percentage change in wholesale turnover.",
        "unit": "percent", "topic": "trade", "verified": True,
    },

    # ---- 84765NED: international trade in services, by country ----
    ("84765NED", "InvoerVanDiensten_1"): {
        "slug": "services-imports", "label_en": "Services imports", "label_nl": "Invoer van diensten",
        "definition_en": "Value of services imported, in million euros.",
        "unit": "million-eur", "topic": "trade", "verified": True,
    },
    ("84765NED", "UitvoerVanDiensten_2"): {
        "slug": "services-exports", "label_en": "Services exports", "label_nl": "Uitvoer van diensten",
        "definition_en": "Value of services exported, in million euros.",
        "unit": "million-eur", "topic": "trade", "verified": True,
    },
    ("84765NED", "SaldoDiensten_3"): {
        "slug": "services-trade-balance", "label_en": "Services trade balance", "label_nl": "Saldo diensten",
        "definition_en": "Net balance (exports minus imports) of services trade, in million euros.",
        "unit": "million-eur", "topic": "trade", "verified": True,
    },

    # ---- 82244NED: Faillissementen; bedrijven en instellingen, SBI 2008 ----
    # Highest-usage single uncurated field across the whole graph (1,116
    # Observations) -- verified against CBS's real DataProperties for
    # 82244NED, not guessed.
    ("82244NED", "UitgesprokenFaillissementen_1"): {
        "slug": "bankruptcies-declared", "label_en": "Bankruptcies declared", "label_nl": "Uitgesproken faillissementen",
        "definition_en": "Number of sole proprietorships, businesses, and institutions declared bankrupt by court order in the period.",
        "unit": "count", "topic": "business-dynamics", "verified": True,
    },

    # ---- 85821NED: Buitenlandse zeggenschap bedrijven in Nederland ----
    # By far the biggest cluster of uncurated measures (10 fields, ~34,000
    # Observations combined) -- all 10 verified against CBS's real
    # DataProperties for 85821NED before writing labels/units, following
    # the same rule as everywhere else in this file: never guess.
    ("85821NED", "AantalBedrijven_1"): {
        "slug": "foreign-controlled-business-count", "label_en": "Number of businesses (foreign-controlled)", "label_nl": "Aantal bedrijven",
        "definition_en": "Count of foreign-controlled businesses in the business register for this industry.",
        "unit": "business", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "Omzet_2"): {
        "slug": "foreign-controlled-turnover", "label_en": "Turnover (foreign-controlled businesses)", "label_nl": "Omzet",
        "definition_en": "Turnover of foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "Productiewaarde_3"): {
        "slug": "foreign-controlled-production-value", "label_en": "Production value (foreign-controlled businesses)", "label_nl": "Productiewaarde",
        "definition_en": "Value of goods and services actually produced, based on sales, for foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "ToegevoegdeWaardeTegenFactorkosten_4"): {
        "slug": "foreign-controlled-value-added", "label_en": "Value added at factor cost (foreign-controlled businesses)", "label_nl": "Toegevoegde waarde tegen factorkosten",
        "definition_en": "Gross income from business activity after correcting for subsidies and indirect taxes, for foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "TotaleAankoopVanGoederenEnDiensten_5"): {
        "slug": "foreign-controlled-total-purchases", "label_en": "Total purchases of goods & services (foreign-controlled businesses)", "label_nl": "Totale aankoop van goederen en diensten",
        "definition_en": "Total value of all goods and services purchased during the reporting period by foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "AankoopVanGoederenEnDiensten_6"): {
        "slug": "foreign-controlled-resale-purchases", "label_en": "Purchases for resale (foreign-controlled businesses)", "label_nl": "Aankoop van goederen en diensten, ingekocht voor wederverkoop",
        "definition_en": "Value of goods and services purchased for resale in their original state (not further processed), by foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "BrutoInvesteringenInMateriele_8"): {
        "slug": "foreign-controlled-gross-investment", "label_en": "Gross investment in tangible goods (foreign-controlled businesses)", "label_nl": "Bruto-investeringen in materiële goederen",
        "definition_en": "Investment in all tangible goods during the reference period by foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "Personeelskosten_7"): {
        "slug": "foreign-controlled-staff-costs", "label_en": "Staff costs (foreign-controlled businesses)", "label_nl": "Personeelskosten",
        "definition_en": "Total remuneration, in cash or in kind, paid by the employer to employees for work done, for foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "AantalWerkzamePersonen_9"): {
        "slug": "foreign-controlled-employment", "label_en": "Number of persons employed (foreign-controlled businesses)", "label_nl": "Aantal werkzame personen",
        "definition_en": "Total number of persons working at foreign-controlled businesses (employees and self-employed), in thousands.",
        "unit": "thousand-persons", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "TotaleOOUitgavenBinnenshuis_10"): {
        "slug": "foreign-controlled-rd-expenditure", "label_en": "In-house R&D expenditure (foreign-controlled businesses)", "label_nl": "Totale O&O-uitgaven binnenshuis",
        "definition_en": "Total in-house expenditure on research and experimental development (creative, systematic work to increase knowledge) by foreign-controlled businesses, in million euros.",
        "unit": "million-eur", "topic": "internationalization", "verified": True,
    },
    ("85821NED", "TotaalAantalOOWerknemers_11"): {
        "slug": "foreign-controlled-rd-staff", "label_en": "R&D staff (foreign-controlled businesses)", "label_nl": "Totaal aantal O&O-werknemers",
        "definition_en": "Total number of staff engaged in research and experimental development at foreign-controlled businesses, in thousands.",
        "unit": "thousand-persons", "topic": "internationalization", "verified": True,
    },
}


def lookup(table_id: str, cbs_field_name: str):
    """Returns the glossary entry for a raw CBS field in a specific table, or None if unmapped."""
    return GLOSSARY.get((table_id, cbs_field_name))


# Branch-field aliases. CBS has used several keys for the same SBI
# industry dimension over the years -- older labour-market tables such as
# 80567NED (Vacatures; vacaturegraad) use e.g. "SectorBranchesSIC2008"
# rather than "BedrijfstakkenBranchesSBI2008". Before this rule existed,
# any such alias fell through to the "measure" path, and because its
# value is a string (not a number) it was SILENTLY DROPPED -- which is
# exactly why KPI 5 showed 48 Observations with 0 HAS_BRANCH links.
BRANCH_FIELD_RE = re.compile(r"^(BedrijfstakkenBranches|SectorBranches|Bedrijfstakken|BedrijfstakkenSBI)", re.I)
BRANCH_INFO = {"short_name": "branch", "class": "Branch", "predicate": "branch"}

# CBS convention: measure ("Topic") keys always end in _<number>
# (Vestigingen_1, AantalInwoners_5); dimension keys never do (RegioS,
# Perioden, Bedrijfsgrootte, Kenmerken...). Used as a safety net below.
MEASURE_KEY_RE = re.compile(r"_\d+$")


def dimension_info(cbs_field_name: str, value=None):
    """
    Returns the dimension descriptor for a raw field name, or None if
    it's a measure column.

    Resolution order:
      1. an explicit DIMENSION_FIELDS entry (curated, always wins)
      2. a known branch-field alias (BRANCH_FIELD_RE)
      3. SAFETY NET: any un-suffixed key carrying a string value is a CBS
         dimension we haven't curated yet (e.g. a company-size, age-group
         or sex breakdown). It becomes a generic :OtherDimension node via
         HAS_DIMENSION instead of being silently discarded -- the node
         keeps its raw CBS key in `dimensionKey`, so KPIs can still filter
         on it (e.g. "only the Totaal member") and it can be promoted to a
         curated dimension later.
    """
    info = DIMENSION_FIELDS.get(cbs_field_name)
    if info is not None:
        return info
    if BRANCH_FIELD_RE.match(cbs_field_name):
        return BRANCH_INFO
    if (value is not None and isinstance(value, str)
            and not MEASURE_KEY_RE.search(cbs_field_name)):
        return {
            "short_name": f"dim-{cbs_field_name.lower()}",
            "class": "OtherDimension",
            "predicate": "otherDimension",
            "dimension_key": cbs_field_name,
        }
    return None


def unmapped_fields(table_id: str, row: dict) -> list:
    """
    Given one raw CBS row from a given table, returns the measure field
    names present that this glossary does NOT yet have a curated entry
    for (they're still running on rdf_mapper's auto-fallback labels).
    """
    result = []
    for key, value in row.items():
        if key in DIMENSION_FIELDS or value is None:
            continue
        if not isinstance(value, (int, float)):
            continue
        if (table_id, key) not in GLOSSARY:
            result.append(key)
    return result
