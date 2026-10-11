"""Station identity rules shared by offline source matching and its tests."""

import re
import unicodedata


def normalized(value):
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    text = re.sub(r"\bsaint\b", "st", text)
    text = re.sub(r"\bfort\b", "ft", text)
    text = re.sub(r"\bmount\b", "mt", text)
    return re.sub(r"[^a-z0-9]", "", text)


def station_name(value):
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    text = text.replace("&", " and ")
    text = re.sub(r"\bdiesel\s+only\b", "", text)
    text = re.sub(r"\btruck\s*stops?\b|\btravel\s+(?:centers?|plazas?|stops?)\b", "", text)
    text = re.sub(r"\b(?:inc|llc|the|fuel|gas|station|service|center|plaza)\b", "", text)
    return re.sub(r"[^a-z0-9]", "", text)


def station_name_keys(value, city=""):
    key = station_name(value)
    keys = {key}
    suffix = normalized(city)
    if suffix and key.endswith(suffix) and len(key) - len(suffix) >= 7:
        keys.add(key[: -len(suffix)])
    return keys


def store_number(value):
    match = re.search(r"#\s*(\d+)", value)
    if not match:
        match = re.search(
            r"^\s*(?:casey'?s(?:\s+general\s+store)?|quick\s+fuel|quik\s*trip|kwik\s*(?:trip|star)|"
            r"pilot(?:\s+travel\s+center)?|flying\s+j|speedway|7[\s-]*eleven|circle\s+k|"
            r"love'?s(?:\s+travel\s+stop)?)[\s-]+(?:store\s+)?(\d+)\b",
            value,
            re.IGNORECASE,
        )
    if not match:
        match = re.search(
            r"\b(?:go[\s-]*mart|kwik[\s-]*fill|spinx|quarles|qff|par[\s-]*mar|"
            r"sheetz|rutters|flyers|pump\s*(?:&|and)\s*pantry)[\s#-]+(?:store\s+)?(\d+)\b",
            value,
            re.IGNORECASE,
        )
    return str(int(match.group(1))) if match else None


def brand(value):
    name = normalized(value)
    for key, variants in {
        "pilot": ("pilot", "flyingj", "one9", "mrfuel"),
        "loves": ("loves",),
        "ta": (
            "travelcentersofamerica",
            "petrostopping",
            "petrotravel",
        ),
        "kwiktrip": ("kwiktrip", "kwikstar"),
        "caseys": ("caseys",),
        "circlek": ("circlek",),
        "speedway": ("speedway",),
        "7eleven": ("7eleven", "seveneleven"),
        "stripes": ("stripes",),
        "maverik": ("maverik",),
        "quiktrip": ("quiktrip",),
        "roadranger": ("roadranger",),
        "sapp": ("sappbros", "sappbrothers"),
        "ambest": ("ambest",),
        "quarles": ("quarles", "qff"),
        "quickfuel": ("quickfuel",),
        "allsups": ("allsups",),
        "yesway": ("yesway",),
        "sheetz": ("sheetz",),
        "rutters": ("rutters",),
        "racetrac": ("racetrac",),
        "raceway": ("raceway",),
        "cefco": ("cefco",),
        "kumgo": ("kumandgo", "kumgo"),
        "cubbys": ("cubbys",),
        "pumpandpantry": ("pumpandpantry", "pumppantry"),
        "sunstop": ("sunstop",),
        "thorntons": ("thorntons",),
        "tootntotum": ("tootntotum", "tootandtotum"),
        "gasnwash": ("gasnwash", "gasandwash"),
        "gomart": ("gomart",),
        "spinx": ("spinx",),
        "kwikfill": ("kwikfill",),
        "royalfarms": ("royalfarms",),
        "dandy": ("dandymini",),
        "alltown": ("alltown",),
        "hucks": ("hucks",),
        "bucees": ("bucees",),
        "faststop": ("faststop",),
        "petroserve": ("petroserve",),
        "mfa": ("mfa",),
        "flyers": ("flyers",),
        "parmar": ("parmar",),
        "marathon": ("marathon",),
        "sunoco": ("sunoco",),
        "texaco": ("texaco",),
        "citgo": ("citgo",),
        "amoco": ("amoco",),
        "valero": ("valero",),
        "phillips66": ("phillips66",),
        "conoco": ("conoco",),
        "union76": ("union76",),
        "arco": ("arco",),
        "irving": ("irving",),
        "gulf": ("gulfoil",),
        "hess": ("hess",),
        "holiday": ("holidaystation",),
        "duckthru": ("duckthru",),
        "parkers": ("parkers",),
        "kentkwik": ("kentkwik",),
        "townpump": ("townpump",),
        "bellstores": ("bellstores",),
        "minitmart": ("minitmart",),
        "hutchs": ("hutchs",),
        "good2go": ("good2go",),
        "mapco": ("mapco",),
        "mirabito": ("mirabito",),
        "mcclure": ("mcclureoil",),
        "enmarket": ("enmarket",),
        "bolla": ("bollamarket",),
        "onvo": ("onvo",),
        "beaconbridge": ("beaconandbridge",),
        "maplefields": ("maplefields",),
        "sprintfoods": ("sprintfoods",),
        "jacksons": ("jacksonsfood",),
        "fasmart": ("fasmart",),
        "familyexpress": ("familyexpress",),
        "friendship": ("friendshipfood",),
        "flashmarket": ("flashmarket",),
        "choctaw": ("choctaw",),
        "mach1": ("mach1", "machone"),
        "stinker": ("stinker",),
        "terribleherbst": ("terribleherbst",),
        "cenex": ("cenex",),
        "sinclair": ("sinclair",),
        "shell": ("shell",),
        "chevron": ("chevron",),
        "exxon": ("exxon", "mobil"),
        "bp": ("britishpetroleum",),
    }.items():
        if any(part in name for part in variants):
            return key
    if re.search(r"\bDK\b", value, re.IGNORECASE):
        return "dk"
    if re.search(r"\bGULF\b", value, re.IGNORECASE):
        return "gulf"
    if re.search(r"\bbp\b", value.casefold()):
        return "bp"
    if re.match(r"^ta(?:\d|travel|express|[a-z])", name) and value.upper().startswith("TA "):
        return "ta"
    return None
