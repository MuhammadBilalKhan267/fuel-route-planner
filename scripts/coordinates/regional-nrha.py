"""Extract the cached NRHA directory by its physical PDF table columns."""

import argparse
import csv
import re
from bisect import bisect_right
from collections import defaultdict
from pathlib import Path

SOURCES = Path(__file__).resolve().parents[2] / "data/coordinate-sources"

FIELDS = (
    "name",
    "address",
    "city",
    "county",
    "state",
    "postal_code",
    "gas",
    "diesel",
    "truck_access",
)
# These boundaries are between the verified columns in the retained PDF.
COLUMN_BOUNDARIES = (185.9, 360.6, 455.9, 541.5, 565.1, 616.6, 635.4, 654.2)


def records_from_cells(rows, page_number):
    """Retain source fields and malformed ZIP evidence without inventing values."""
    for cells in rows:
        record = dict(zip(FIELDS, (cell.strip() for cell in cells)))
        if not re.fullmatch(r"[A-Z]{2}", record["state"]):
            continue
        if not all(record[field] for field in ("name", "address", "city")):
            continue
        record["postal_code_raw"] = record["postal_code"]
        if record["postal_code"] and not re.fullmatch(r"\d{5}(?:-?\d{4})?", record["postal_code"]):
            base_zip = re.match(r"^(\d{5})(?:-|$)", record["postal_code"])
            record["postal_code"] = base_zip[1] if base_zip else ""
        record["page"] = page_number
        yield record


def character_rows(characters, page_number):
    """Separate touching columns that PDF word extraction merges together."""
    rows = defaultdict(list)
    for character in characters:
        rows[round(character["top"], 1)].append(character)
    for baseline in sorted(rows):
        cells = [[] for _ in FIELDS]
        for character in sorted(rows[baseline], key=lambda item: item["x0"]):
            cells[bisect_right(COLUMN_BOUNDARIES, character["x0"])].append(character["text"])
        yield from records_from_cells([["".join(cell) for cell in cells]], page_number)


def extract(source, destination):
    import pdfplumber

    count = 0
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        with (
            pdfplumber.open(source) as pdf,
            temporary.open("w", encoding="utf-8", newline="") as output,
        ):
            writer = csv.DictWriter(
                output, fieldnames=(*FIELDS, "postal_code_raw", "page"), delimiter="\t"
            )
            writer.writeheader()
            for page_number, page in enumerate(pdf.pages, 1):
                for record in character_rows(page.chars, page_number):
                    writer.writerow(record)
                    count += 1
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return count


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--source", type=Path, default=SOURCES / "regional-nrha-fuel-merchant-directory.pdf"
    )
    parser.add_argument("--output", type=Path, default=SOURCES / "regional-nrha-merchant-table.tsv")
    args = parser.parse_args()
    print({"merchant_rows": extract(args.source, args.output)})
