"""
Shared helpers for the company layer (KVK + GLEIF + Wikidata -> Neo4j).

The company layer answers "WHICH companies" underneath the CBS KPIs, which
only say "how many / what share per industry". CBS never publishes company
names, so companies are linked to the CBS graph through CLASSIFICATIONS,
not IDs:

    Company --IN_BRANCH-->  CBS :Branch      via the KVK SBI code (division -> SBI 2008 section)
    Company --IN_SIZE_CLASS--> size band      via KVK 'totaalWerkzamePersonen'
    Company --LOCATED_IN--> CBS :Gemeente    via the KVK address (city name)
    Company <--> GLEIF LEI                    via the KVK number (GLEIF 'registeredAs', authority RA000463)

Everything lands in ../landing_zone/companies/ (git-ignored, like the CBS data).
"""
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "landing_zone" / "companies"
LANDING.mkdir(parents=True, exist_ok=True)

GLEIF_KVK_AUTHORITY = "RA000463"   # GLEIF registration-authority code of the Dutch Chamber of Commerce (KVK)

# SBI 2008 top-level sections exactly as they appear as :Branch prefLabelNl in
# the graph (taken from 81589NED / 86119NED), so a company links to the SAME
# node KPI 1-5 use.
SBI2008_SECTION_LABEL = {
    "A": "A Landbouw, bosbouw en visserij",
    "B": "B Delfstoffenwinning",
    "C": "C Industrie",
    "D": "D Energievoorziening",
    "E": "E Waterbedrijven en afvalbeheer",
    "F": "F Bouwnijverheid",
    "G": "G Handel",
    "H": "H Vervoer en opslag",
    "I": "I Horeca",
    "J": "J Informatie en communicatie",
    "K": "K Financiële dienstverlening",
    "L": "L Verhuur en handel van onroerend goed",
    "M": "M Specialistische zakelijke diensten",
    "N": "N Verhuur en overige zakelijke diensten",
    "O": "O Openbaar bestuur en overheidsdiensten",
    "P": "P Onderwijs",
    "Q": "Q Gezondheids- en welzijnszorg",
    "R": "R Cultuur, sport en recreatie",
    "S": "S Overige dienstverlening",
    "T": "T Huishoudens",
    "U": "U Extraterritoriale organisaties",
}

# SBI 2008 division (first 2 digits of the SBI code) -> section letter.
# SBI 2025 kept almost all division NUMBERS (it re-lettered sections), so
# mapping on the division number puts a company in the SBI 2008 section
# the CBS ICT survey (86119NED) uses, whichever SBI version KVK returns.
_DIVISION_RANGES = [
    ("A", 1, 3), ("B", 5, 9), ("C", 10, 33), ("D", 35, 35), ("E", 36, 39),
    ("F", 41, 43), ("G", 45, 47), ("H", 49, 53), ("I", 55, 56), ("J", 58, 63),
    ("K", 64, 66), ("L", 68, 68), ("M", 69, 75), ("N", 77, 82), ("O", 84, 84),
    ("P", 85, 85), ("Q", 86, 88), ("R", 90, 93), ("S", 94, 96), ("T", 97, 98),
    ("U", 99, 99),
]


def sbi_section(sbi_code):
    """'5610' / '56101' / '01.1' -> 'I' (SBI 2008 section letter) or None."""
    if not sbi_code:
        return None
    digits = re.sub(r"\D", "", str(sbi_code))
    if len(digits) < 2:
        return None
    div = int(digits[:2])
    for letter, lo, hi in _DIVISION_RANGES:
        if lo <= div <= hi:
            return letter
    return None


def size_bands(staff):
    """Staff count -> (CBS business-count band, CBS ICT-survey band). Both
    are the labels used in the CBS tables, so a company lines up with the
    size groups KPI 2/3 show."""
    if staff is None:
        return None, None
    s = int(staff)
    if s < 10:
        count_band = "0 tot 10 werkzame personen"
        ict_band = None                      # ICT survey covers 10+ only
    elif s < 20:
        count_band, ict_band = "10 tot 20 werkzame personen", "10 tot 50 werkzame personen"
    elif s < 50:
        count_band, ict_band = "20 tot 50 werkzame personen", "10 tot 50 werkzame personen"
    elif s < 100:
        count_band, ict_band = "50 tot 100 werkzame personen", "50 tot 250 werkzame personen"
    elif s < 250:
        count_band, ict_band = "100 of meer werkzame personen", "50 tot 250 werkzame personen"
    else:
        count_band, ict_band = "100 of meer werkzame personen", "250 of meer werkzame personen"
    return count_band, ict_band


def norm_kvk(v):
    """KVK numbers are 8 digits; GLEIF sometimes stores them with spaces or
    a leading/trailing extra (e.g. a 12-digit vestigingsnummer)."""
    if v is None:
        return None
    d = re.sub(r"\D", "", str(v))
    return d[:8] if len(d) >= 8 else (d.zfill(8) if d else None)


def is_euro(currency):
    """Wikidata revenue unit -> True for euro (label 'euro'/'EUR' or the bare item id Q4916)."""
    c = (currency or "").strip().lower()
    return c in ("euro", "eur", "€", "q4916") or c.endswith("/q4916")


# Wikidata 'industry' (P452) names -> SBI 2008 section. Used only when a company
# has no official NACE code on Wikidata. First matching keyword wins, so more
# specific words come first (e.g. 'scheepsbouw' before 'bouw', 'kunstmatige
# intelligentie' before 'kunst'). Dutch and English names.
INDUSTRY_KEYWORDS = [
    ("kunstmatige intelligentie", "J"), ("artificial intelligence", "J"), ("computerspel", "J"), ("video game", "J"),
    ("software", "J"), ("informatietechnologie", "J"), ("information technology", "J"), ("telecom", "J"),
    ("internet", "J"), ("uitgeve", "J"), ("publishing", "J"), ("media", "J"), ("film", "J"), ("muziek", "J"),
    ("music", "J"), ("omroep", "J"), ("broadcast", "J"), ("geospatial", "J"), ("data", "J"),
    ("scheepsbouw", "C"), ("shipbuilding", "C"), ("olie-industrie", "C"), ("petroleum", "C"),
    ("brouwerij", "C"), ("brewing", "C"), ("dranken", "C"), ("beverage", "C"), ("vlees", "C"), ("meat", "C"),
    ("levensmiddelen", "C"), ("voedsel", "C"), ("food industry", "C"), ("food processing", "C"),
    ("kleding", "C"), ("clothing", "C"), ("textiel", "C"), ("cosmetica", "C"), ("cosmetic", "C"),
    ("farmac", "C"), ("pharmac", "C"), ("chemi", "C"), ("auto-industrie", "C"), ("automotive", "C"),
    ("rijwiel", "C"), ("bicycle", "C"), ("machine", "C"), ("elektronica", "C"), ("electronics", "C"),
    ("halfgeleider", "C"), ("semiconductor", "C"), ("staal", "C"), ("steel", "C"), ("luchtvaartindustrie", "C"),
    ("aerospace", "C"), ("vliegtuigbouw", "C"), ("ruimtevaart", "C"), ("wapenindustrie", "C"), ("defensie-industrie", "C"),
    ("semiconductor", "C"),
    ("detailhandel", "G"), ("retail", "G"), ("groothandel", "G"), ("wholesale", "G"), ("e-commerce", "G"),
    ("tankstation", "G"), ("supermarkt", "G"), ("handel", "G"), ("trade", "G"),
    ("wegenbouw", "F"), ("waterbouw", "F"), ("bouw", "F"), ("construction", "F"), ("civil engineering", "F"),
    ("verzekering", "K"), ("insurance", "K"), ("fintech", "K"), ("bank", "K"), ("financ", "K"), ("investment", "K"),
    ("openbaar vervoer", "H"), ("public transport", "H"), ("logistiek", "H"), ("logistics", "H"), ("vervoer", "H"),
    ("transport", "H"), ("luchtvaart", "H"), ("aviation", "H"), ("airline", "H"), ("scheepvaart", "H"), ("shipping", "H"),
    ("afval", "E"), ("waste", "E"), ("recycling", "E"), ("milieu", "E"), ("drinkwater", "E"),
    ("energie", "D"), ("energy", "D"), ("elektriciteit", "D"), ("electricity", "D"),
    ("horeca", "I"), ("hotel", "I"), ("restaurant", "I"), ("catering", "I"), ("hospitality", "I"),
    ("vastgoed", "L"), ("onroerend", "L"), ("real estate", "L"),
    ("arbeidsbemiddeling", "N"), ("humanresources", "N"), ("human resources", "N"), ("verhuur en lease", "N"), ("leasing", "N"), ("uitzend", "N"), ("staffing", "N"), ("schoonmaak", "N"),
    ("cleaning", "N"), ("beveiliging", "N"), ("security", "N"), ("reisbureau", "N"), ("travel agenc", "N"),
    ("wetenschappelijk onderzoek", "M"), ("research", "M"), ("advies", "M"), ("consult", "M"), ("accountan", "M"),
    ("architect", "M"), ("reclame", "M"), ("advertising", "M"), ("juridisch", "M"), ("legal", "M"), ("engineering", "M"),
    ("ziekenhuis", "Q"), ("hospital", "Q"), ("zorg", "Q"), ("health", "Q"), ("gezondheid", "Q"),
    ("onderwijs", "P"), ("education", "P"), ("universiteit", "P"), ("university", "P"),
    ("landbouw", "A"), ("agricult", "A"), ("tuinbouw", "A"), ("horticult", "A"), ("visserij", "A"), ("fishing", "A"),
    ("mijnbouw", "B"), ("mining", "B"), ("olie- en gas", "B"), ("oil and gas", "B"), ("delfstof", "B"),
    ("sport", "R"), ("recreatie", "R"), ("recreation", "R"), ("cultuur", "R"), ("museum", "R"), ("kunst", "R"),
    ("entertainment", "R"), ("gokken", "R"), ("gambling", "R"),
    ("vereniging", "S"), ("vakbond", "S"), ("association", "S"), ("trade union", "S"),
    ("overheid", "O"), ("openbaar bestuur", "O"), ("government", "O"), ("defensie", "O"), ("defence", "O"),
    # generic last: many Dutch names end in '-industrie' even for services
    ("industrie", "C"), ("manufactur", "C"), ("industry", "C"),
]


def section_from_industry_names(names):
    """['brouwerijsector', 'bouw'] -> 'C' (first name that maps), or None."""
    low = [(n or "").lower() for n in names or []]
    for kw, letter in INDUSTRY_KEYWORDS:          # most specific keyword wins, whichever name it's in
        if any(kw in n for n in low):
            return letter
    return None


def read_json(path, default=None):
    p = Path(path)
    if not p.exists():
        return default
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def write_json(path, obj):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)
    os.replace(tmp, p)


def read_jsonl(path):
    p = Path(path)
    if not p.exists():
        return []
    out = []
    with open(p, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def append_jsonl(path, obj):
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")
