"""Parse cached public Fuelrite directories into identity evidence, never coordinates."""

import argparse
import json
import re
from pathlib import Path

STATION = re.compile(r"^\s*\*?\s*###\s+(.+?)\s+—\s+(.+?)\s*$", re.MULTILINE)
PHONE = re.compile(r"\(\d{3}\)\s*\d{3}-\d{4}")
EXPLICIT_EXIT = re.compile(
    r"\bI[- ](\d{1,3})\s+(?:(?:N|S|E|W|N/S|E/W)\s*,?\s*)?Exit\s+([A-Z]?-?\d+[A-Z]?)\b",
    re.IGNORECASE,
)


def directory_identities(rendered_text, state, source_url):
    """Require the complete declared count; preserve conflicting statements separately."""
    if not re.fullmatch(r"[A-Z]{2}", state) or not source_url.startswith("https://"):
        raise ValueError("A state abbreviation and HTTPS source URL are required")
    text = re.sub(r"(?<!\S)L\d+:[ \t]*", "\n", rendered_text)
    headings = list(STATION.finditer(text))
    declared = re.search(r"\b(\d+) locations in [^\n]+ shown below", text)
    if not declared or not headings or len(headings) != int(declared[1]):
        raise ValueError("Incomplete directory: station headings differ from the published count")
    records = []
    for index, heading in enumerate(headings):
        end = headings[index + 1].start() if index + 1 < len(headings) else len(text)
        block = text[heading.end() : end].split("Loading map...", 1)[0]
        address = next(
            (line.strip() for line in block.splitlines() if re.match(r"^\s*\d[^\n]+,", line)),
            None,
        )
        if not address:
            raise ValueError(f"Missing numeric street for {heading[2]}")
        street, city, state_zip = [part.strip() for part in address.rsplit(",", 2)]
        match = re.fullmatch(r"([A-Z]{2})\s+(\d{5}(?:-\d{4})?)", state_zip)
        if not match or match[1] != state:
            raise ValueError(f"Published address is outside expected state for {heading[2]}")
        phone = PHONE.search(block)
        records.append(
            {
                "name": heading[2],
                "street_address": street,
                "city": city,
                "heading_city": heading[1],
                "state": match[1],
                "postcode": match[2],
                "phone": phone[0] if phone else None,
                "explicit_route_exits": [
                    {"route": f"I-{route}", "exit_ref": exit_ref.upper()}
                    for route, exit_ref in dict.fromkeys(EXPLICIT_EXIT.findall(block))
                ],
                "published_context": [
                    line.strip()
                    for line in block.splitlines()
                    if line.strip()
                    and line.strip() != address
                    and not PHONE.search(line)
                    and not line.strip().startswith("Image:")
                ],
                "source_url": source_url,
                "identity_verified": False,
                "coordinate_verified": False,
            }
        )
    return records


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--state", required=True)
    parser.add_argument("--source-url", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    records = directory_identities(
        args.input.read_text(encoding="utf-8"), args.state, args.source_url
    )
    args.output.write_text(
        json.dumps(
            {
                "source_url": args.source_url,
                "cached_rendered_source": str(args.input),
                "records": records,
                "purpose": "Published identities and literal directions only; actual station equipment requires independent review.",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"Parsed {len(records)} identity records; no coordinates produced")


if __name__ == "__main__":
    main()
