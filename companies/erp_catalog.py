"""
Shared ERP catalogue: which products we recognise, how we spot them in text, and their lifecycle.

lifecycle:
  current  a product the vendor actively sells / develops today
  legacy   an older or on-premise generation that vendors are steering customers away from --
           the classic "migration candidate" (new ERP selection, cloud move, re-implementation).
           This is a SALES HINT, not a support-date claim: check the vendor's current maintenance
           dates before quoting a deadline to a prospect.
  unknown  the text names the vendor but not the product generation ("we run SAP")

Used by erp_enrich.py (website scan, vendor reference pages), signals_enrich.py (job vacancies)
and load_companies.py.
"""
import re

# name -> vendor, lifecycle, aliases (accepted in your CSVs), strong (host/script fragments = medium evidence),
#         weak (regexes on lower-cased text = low evidence), note (shown to sellers)
CATALOG = {
    "SAP S/4HANA": dict(vendor="SAP", lifecycle="current", aliases=["sap s4hana", "sap s/4hana", "s/4hana", "s4hana"],
                        strong=["s4hana.ondemand.com", "sapbydesign"], weak=[r"s/?4 ?hana"],
                        note="SAP's current ERP generation"),
    "SAP ECC / R/3": dict(vendor="SAP", lifecycle="legacy", aliases=["sap ecc", "sap r/3", "sap r3", "ecc 6", "ecc6"],
                          strong=[], weak=[r"sap ecc", r"sap r/?3\b", r"\becc ?6"],
                          note="Older SAP generation; customers face a S/4HANA migration decision"),
    "SAP (version unknown)": dict(vendor="SAP", lifecycle="unknown", aliases=["sap", "sap erp"],
                                  strong=[], weak=[r"\bsap erp\b", r"ervaring met sap", r"experience (?:with|in) sap"],
                                  note="Runs SAP; generation not known -- qualify ECC vs S/4HANA"),
    "SAP Business One": dict(vendor="SAP", lifecycle="current", aliases=["sap business one", "sap b1", "business one"],
                             strong=[], weak=[r"sap business one", r"\bsap b1\b"], note="SAP for smaller companies"),
    "Microsoft Dynamics 365": dict(vendor="Microsoft", lifecycle="current",
                                   aliases=["dynamics 365", "d365", "business central", "microsoft dynamics 365"],
                                   strong=["businesscentral.dynamics.com", "operations.dynamics.com"],
                                   weak=[r"dynamics 365", r"business central", r"\bd365\b"], note="Current Dynamics generation"),
    "Microsoft Dynamics NAV / AX": dict(vendor="Microsoft", lifecycle="legacy",
                                        aliases=["navision", "dynamics nav", "dynamics ax", "axapta", "microsoft dynamics nav"],
                                        strong=[], weak=[r"dynamics (?:nav|ax)\b", r"navision", r"axapta"],
                                        note="Predecessor of Business Central / D365 F&O -- upgrade or replace decision"),
    "Microsoft Dynamics (version unknown)": dict(vendor="Microsoft", lifecycle="unknown",
                                                 aliases=["dynamics", "microsoft dynamics"], strong=[],
                                                 weak=[r"microsoft dynamics\b(?! ?(?:365|nav|ax))"],
                                                 note="Runs Dynamics; generation not known"),
    "Oracle NetSuite": dict(vendor="Oracle", lifecycle="current", aliases=["netsuite", "oracle netsuite"],
                            strong=["netsuite.com"], weak=[r"netsuite"], note="Cloud ERP, common in growth companies"),
    "Oracle ERP Cloud / JD Edwards": dict(vendor="Oracle", lifecycle="current",
                                          aliases=["oracle", "oracle erp", "oracle fusion", "jd edwards", "oracle ebs",
                                                   "e-business suite", "peoplesoft"],
                                          strong=[], weak=[r"oracle fusion", r"oracle erp", r"jd ?edwards", r"e-business suite", r"peoplesoft"],
                                          note="Oracle enterprise ERP family"),
    "Exact Online": dict(vendor="Exact", lifecycle="current", aliases=["exact", "exact online"],
                         strong=["exactonline.nl", "exactonline.com", "exactonline.be"], weak=[r"exact online"],
                         note="Cloud ERP/accounting for SMEs"),
    "Exact Globe / Synergy": dict(vendor="Exact", lifecycle="legacy", aliases=["exact globe", "exact synergy"],
                                  strong=[], weak=[r"exact (?:globe|synergy)"],
                                  note="On-premise Exact products; cloud-migration candidates"),
    "AFAS": dict(vendor="AFAS", lifecycle="current", aliases=["afas", "afas profit", "afas online", "afas insite"],
                 strong=["afasinsite", "afasonlineconnector", "afas.online"], weak=[r"afas (?:profit|software|online|insite)"],
                 note="Dutch all-in-one ERP/HR, strong in services & public sector"),
    "Unit4": dict(vendor="Unit4", lifecycle="current", aliases=["unit4", "agresso", "unit4 erp"],
                  strong=["unit4.com"], weak=[r"unit4", r"agresso"], note="Services / public sector ERP"),
    "Infor": dict(vendor="Infor", lifecycle="current", aliases=["infor", "infor ln", "infor m3", "infor cloudsuite"],
                  strong=["infor.com"], weak=[r"infor (?:ln|m3|cloudsuite|syteline|visual)"], note="Manufacturing / distribution ERP"),
    "Baan": dict(vendor="Infor", lifecycle="legacy", aliases=["baan"], strong=[], weak=[r"\bbaan\b"],
                 note="Very old ERP generation (Infor LN's ancestor) -- almost certainly a replacement candidate"),
    "IFS": dict(vendor="IFS", lifecycle="current", aliases=["ifs", "ifs cloud", "ifs applications"],
                strong=["ifs.com"], weak=[r"ifs (?:cloud|applications|erp)"], note="Asset-intensive / project industries"),
    "Visma": dict(vendor="Visma", lifecycle="current", aliases=["visma", "visma net", "visma severa"],
                  strong=[], weak=[r"visma net"], note="Nordic/Dutch SME ERP & accounting"),
    "Sage": dict(vendor="Sage", lifecycle="current", aliases=["sage", "sage 100", "sage x3", "sage intacct"],
                 strong=[], weak=[r"sage (?:x3|100|intacct|200)"], note="SME ERP"),
    "Odoo": dict(vendor="Odoo", lifecycle="current", aliases=["odoo"], strong=["odoo.com"], weak=[r"\bodoo\b"],
                 note="Open-source ERP, SME"),
    "Epicor": dict(vendor="Epicor", lifecycle="current", aliases=["epicor", "epicor kinetic"], strong=["epicor.com"],
                   weak=[r"epicor"], note="Manufacturing / distribution ERP"),
    "QAD": dict(vendor="QAD", lifecycle="current", aliases=["qad", "qad adaptive erp"], strong=[],
                weak=[r"qad (?:adaptive|erp)"], note="Manufacturing ERP"),
    "Ridder": dict(vendor="Ridder", lifecycle="current", aliases=["ridder", "ridder iq"], strong=["ridder.nl", "ridder.com"],
                   weak=[r"ridder ?(?:iq|erp)"], note="Dutch horticulture / manufacturing ERP"),
    "Workday Financials": dict(vendor="Workday", lifecycle="current", aliases=["workday", "workday financials"],
                               strong=["myworkday.com"], weak=[r"workday financial"], note="Enterprise finance/HR cloud"),
    "Acumatica": dict(vendor="Acumatica", lifecycle="current", aliases=["acumatica"], strong=[], weak=[r"acumatica"],
                      note="Cloud ERP, mid-market"),
}

_ALIAS = {}
for _name, _d in CATALOG.items():
    _ALIAS[_name.lower()] = _name
    for _a in _d["aliases"]:
        _ALIAS.setdefault(_a.lower(), _name)


def canonical(name):
    """Free-text ERP name -> (catalog name, vendor, lifecycle). Unknown names are kept as typed."""
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if key in _ALIAS:
        c = _ALIAS[key]
        return c, CATALOG[c]["vendor"], CATALOG[c]["lifecycle"]
    n = (name or "").strip()
    return n, n, "unknown"


def lifecycle_of(erp_name):
    return CATALOG.get(erp_name, {}).get("lifecycle", "unknown")


def note_of(erp_name):
    return CATALOG.get(erp_name, {}).get("note")


def slug(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")


def mentions(text):
    """Pure function: raw text/HTML -> [(erp name, 'strong'|'weak', matched fragment, snippet)].
    'strong' = a host/script fragment of the product itself; 'weak' = the product merely named in text."""
    low = text.lower()
    hits = []
    for name, d in CATALOG.items():
        s = next((f for f in d["strong"] if f in low), None)
        if s:
            hits.append((name, "strong", s, f"references '{s}'"))
            continue
        for pat in d["weak"]:
            m = re.search(pat, low)
            if m:
                a, b = max(0, m.start() - 60), min(len(low), m.end() + 60)
                snippet = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text[a:b])).strip()
                hits.append((name, "weak", m.group(0), f"...{snippet}..."))
                break
    return hits
