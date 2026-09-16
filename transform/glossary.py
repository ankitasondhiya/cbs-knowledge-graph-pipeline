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
    "RegioS": {"short_name": "regio_category", "class": "RegioCategory", "predicate": "regioCategory"},
    # IMPORTANT: RegioS is NOT the same thing as WijkenEnBuurten. On the
    # business tables it's a national/foreign/unclassified breakdown
    # (e.g. "Nederland, Buitenland, Niet in te delen"), not a municipal
    # code -- do not merge it into the Buurt/Wijk/Gemeente hierarchy.
    "Perioden": {"short_name": "period", "literal": True},  # kept as a plain literal on the Observation, not a node
    "Diensten": {"short_name": "service_category", "class": "ServiceCategory", "predicate": "serviceCategory"},
    "Landen": {"short_name": "country", "class": "Country", "predicate": "country"},
    "InEnUitvoer": {"short_name": "trade_direction", "class": "TradeDirection", "predicate": "tradeDirection"},
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
}


def lookup(table_id: str, cbs_field_name: str):
    """Returns the glossary entry for a raw CBS field in a specific table, or None if unmapped."""
    return GLOSSARY.get((table_id, cbs_field_name))


def dimension_info(cbs_field_name: str):
    """Returns the DIMENSION_FIELDS entry for a raw field name, or None if it's a measure column."""
    return DIMENSION_FIELDS.get(cbs_field_name)


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
