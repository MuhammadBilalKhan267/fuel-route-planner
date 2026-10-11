"""Extract cached operator street identities; published pins still need review."""

import html
import json
import math
import re
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "data/coordinate-sources"


def enmarket_locations(document):
    marker = "var map_info ="
    start = document.index(marker) + len(marker)
    data, _ = json.JSONDecoder().raw_decode(document[start:].lstrip())
    return [
        {
            "external_id": str(row["id"]),
            "store_number": row["title"].rsplit(" - ", 1)[1],
            "name": "Enmarket",
            "street": row["street"],
            "city": row["city"],
            "state": row["state"],
            "postal_code": row["zip"],
            "country": row["country"],
            "phone": row["phone"],
            "amenities": row["amenities"],
            "latitude": float(row["latitude"]),
            "longitude": float(row["longitude"]),
            "source_url": "https://enmarket.com/find-a-location/",
            "coordinate_verified": False,
            "retrieved_on": "2026-10-11",
        }
        for row in data["map_info"]
    ]


def jet_locations(document):
    blocks = re.split(r'<li id="location-(\d+)"[^>]*class="location-listing">', document)
    rows = []
    for index in range(1, len(blocks), 2):
        external_id, body = blocks[index : index + 2]
        address = re.search(
            r'<span class="loc-address"><strong>Store #([^<]+):</strong>\s*([^<]+)</span>',
            body,
        )
        coordinate = re.search(
            r'href="https://www\.google\.com/maps/dir/[^"<>]+/@(-?\d+\.\d+),(-?\d+\.\d+),',
            body,
        )
        if not address or not coordinate:
            raise ValueError(f"Incomplete primary Jet location {external_id}")
        raw_address = html.unescape(address[2]).strip()
        parts = [part.strip() for part in raw_address.rsplit(",", 2)]
        street, city, state = parts if len(parts) == 3 else ("", "", "")
        phone = re.search(r'href="tel:([^"<>]+)"', body)
        fuel_brand = re.search(r'class="loc_brand ([a-z_]+)"', body)
        rows.append(
            {
                "external_id": external_id,
                "store_number": address[1].strip(),
                "name": "Jet Food Store",
                "street": street,
                "address_raw": raw_address,
                "city": city,
                "state": state,
                "country": "US",
                "phone": phone[1] if phone else "",
                "fuel_brand": fuel_brand[1] if fuel_brand else "",
                "latitude": float(coordinate[1]),
                "longitude": float(coordinate[2]),
                "source_url": "https://www.jetfoodstores.com/locations/",
                "coordinate_verified": False,
                "retrieved_on": "2026-10-11",
            }
        )
    return rows


def hutch_locations(document):
    amenities = {
        number: re.findall(r'<img[^>]+alt="([^"]+)"', body)
        for number, body in re.findall(
            r"<h3>Hutch's #(\d+)</h3>(.*?)(?=<h3>|$)", document, re.DOTALL
        )
    }
    rows = []
    for attributes, body in re.findall(
        r'<div class="marker"([^>]*)>(.*?)</a>\s*</div>', document, re.DOTALL
    ):
        number = re.search(r"Hutch's #(\d+)</h4>", body)
        address = re.search(r'<div class="address">([^<]+)</div>', body)
        link = re.search(r'<a href="(https://www\.hutchs\.net/[^"]+)"', body)
        if not number or not address or not link:
            raise ValueError("Incomplete primary Hutch location")
        parts = [part.strip() for part in html.unescape(address[1]).split(",")]
        if parts[-1] == "USA":
            parts.pop()
        state = re.fullmatch(r"(OK|KS)(?:\s+(\d{5}))?", parts[-1])
        if not state or len(parts) < 3:
            raise ValueError("Incomplete primary Hutch address")
        coordinates = [re.search(rf'data-{name}="([^"]+)"', attributes) for name in ("lat", "lng")]
        try:
            values = [float(match[1]) if match else None for match in coordinates]
        except ValueError:
            values = [None, None]
        if not all(value is not None and math.isfinite(value) for value in values) or not (
            -90 <= values[0] <= 90 and -180 <= values[1] <= 180
        ):
            values = [None, None]
        phone = re.search(r'<div class="phone">([^<]+)</div>', body)
        rows.append(
            {
                "external_id": f"hutch:{number[1]}",
                "store_number": number[1],
                "name": f"Hutch's #{number[1]}",
                "street": ", ".join(parts[:-2]),
                "city": parts[-2],
                "state": state[1],
                "postal_code": state[2] or "",
                "country": "US",
                "phone": phone[1] if phone else "",
                "amenities": amenities.get(number[1], []),
                "latitude": values[0],
                "longitude": values[1],
                "source_url": html.unescape(link[1]),
                "directory_url": "https://www.hutchs.net/locations/",
                "coordinate_verified": False,
                "retrieved_on": "2026-10-11",
            }
        )
    return rows


def main():
    for name, parser in [("enmarket", enmarket_locations), ("jet", jet_locations)]:
        document = (SOURCES / f"regional-street-{name}.html").read_text(encoding="utf-8")
        rows = parser(document)
        destination = SOURCES / f"regional-street-{name}-stations.json"
        destination.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        print({"operator": name, "published_identities": len(rows), "verified_pins": 0})
    hutch = SOURCES / "regional-hutch-locations.html"
    if hutch.exists():
        rows = hutch_locations(hutch.read_text(encoding="utf-8"))
        (SOURCES / "regional-hutch-stations.json").write_text(
            json.dumps(rows, indent=2) + "\n", encoding="utf-8"
        )
        print({"operator": "hutch", "published_identities": len(rows), "verified_pins": 0})


if __name__ == "__main__":
    main()
