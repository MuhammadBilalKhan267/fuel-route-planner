"""Cache and reproduce individually reviewed Quarles fuel-canopy coordinates.

Official numbered addresses and fuel-network identities locate the search area.
Only an explicitly recorded visual fuel-premises review produces a candidate.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import UTC, datetime
from urllib.parse import urlencode

import httpx
from loves_ta import DATA, ROOT, PublicDownloads, write_json

EXPORT = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/export"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--ids", help="Comma-separated OPIS IDs for a narrow follow-up")
    parser.add_argument(
        "--census", action="store_true", help="Inspect the independent street anchor"
    )
    args = parser.parse_args()
    if args.offset < 0 or args.limit < 0:
        parser.error("offset and limit must be nonnegative")
    targets = json.loads(
        (DATA / "supplemental-quarles-aerial-review-targets.json").read_text(encoding="utf-8")
    )
    selected_ids = {int(value) for value in args.ids.split(",")} if args.ids else None
    selected = [row for row in targets if selected_ids is None or row["opis_id"] in selected_ids]
    anchors = {}
    if args.census:
        anchors = {
            row["opis_id"]: row
            for row in json.loads(
                (DATA / "supplemental-quarles-census-anchors.json").read_text(encoding="utf-8")
            )
            if row["match_status"] == "Match"
        }
    downloads = PublicDownloads()
    request_count = 0
    for target in selected[args.offset : args.offset + args.limit]:
        station_id = target["opis_id"]
        suffix = "-census" if args.census else ""
        image_path = DATA / f"supplemental-quarles-aerial-{station_id}{suffix}.png"
        metadata_path = image_path.with_suffix(".json")
        if metadata_path.exists() or not args.download:
            continue
        center = anchors.get(station_id) if args.census else target
        if center is None:
            continue
        lat, lon = center["latitude"], center["longitude"]
        bounds = [lon - 0.0015, lat - 0.0015, lon + 0.0015, lat + 0.0015]
        parameters = {
            "bbox": ",".join(str(value) for value in bounds),
            "bboxSR": "4326",
            "imageSR": "4326",
            "size": "700,700",
            "format": "png",
            "f": "image",
        }
        url = EXPORT + "?" + urlencode(parameters)
        try:
            response = downloads.client.get(url)
        except httpx.RequestError as exc:
            failures_path = DATA / "supplemental-quarles-aerial-download-failures.json"
            failures = (
                json.loads(failures_path.read_text(encoding="utf-8"))
                if failures_path.exists()
                else []
            )
            failures.append(
                {
                    "opis_id": station_id,
                    "source_url": url,
                    "attempted_at": datetime.now(UTC).isoformat(),
                    "error_type": type(exc).__name__,
                    "automatic_retry": False,
                }
            )
            write_json(failures_path, failures)
            print({"stopped_downloads": station_id, "error_type": type(exc).__name__}, flush=True)
            break
        if response.status_code in {401, 403, 429}:
            raise SystemExit(f"Stopped public aerial downloads: HTTP {response.status_code}")
        response.raise_for_status()
        if not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise SystemExit("Public aerial service returned a non-PNG response")
        image_path.write_bytes(response.content)
        write_json(
            metadata_path,
            {
                "source_url": url,
                "retrieved_at": datetime.now(UTC).isoformat(),
                "bounding_box": bounds,
                "image_spatial_reference": 4326,
                "width": 700,
                "height": 700,
                "search_target": target,
                "search_anchor": center,
                "coordinate_verification": "pending_visual_review",
            },
        )
        request_count += 1
        print({"cached_aerial": station_id, "requests": request_count}, flush=True)
    downloads.client.close()
    review_path = DATA / "supplemental-quarles-aerial-reviews.json"
    reviews = json.loads(review_path.read_text(encoding="utf-8")) if review_path.exists() else {}
    by_id = {target["opis_id"]: target for target in targets}
    candidates = []
    for key, review in reviews.items():
        if not review.get("accepted"):
            continue
        station_id = int(key)
        target = by_id[station_id]
        variant = review.get("image_variant")
        tile_variant = variant in {"tiles", "tiles-street"}
        suffix = "-census" if variant == "census" else ""
        tile_name = review.get("tile_variant", "street" if variant == "tiles-street" else "")
        tile_suffix = f"-{tile_name}" if tile_name else ""
        metadata_path = (
            DATA / "root-manual" / f"named-{station_id}{tile_suffix}-tiles.json"
            if tile_variant
            else DATA / f"supplemental-quarles-aerial-{station_id}{suffix}.json"
        )
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        x, y = review["pixel_center"]
        left, top, right, bottom = review["canopy_pixel_bounds"]
        width, height = metadata["size"] if tile_variant else (700, 700)
        if not 0 <= left < x < right <= width or not 0 <= top < y < bottom <= height:
            raise ValueError(f"Invalid reviewed canopy bounds for {station_id}")
        if tile_variant:
            if str(ROOT) not in sys.path:
                sys.path.insert(0, str(ROOT))
            from scripts.coordinates.aerial_tiles import BASE, pixel_coordinate

            latitude, longitude = pixel_coordinate(
                x, y, metadata["left_tile"], metadata["top_tile"], metadata["zoom"]
            )
            source_url = (
                f"{BASE}/{metadata['zoom']}/"
                f"{metadata['top_tile'] + math.floor(y / 256)}/"
                f"{metadata['left_tile'] + math.floor(x / 256)}"
            )
            image_reason = (
                f" Reviewed fuel installation bounds {review['canopy_pixel_bounds']}, "
                f"center {review['pixel_center']}; EPSG:3857 XYZ mosaic at zoom "
                f"{metadata['zoom']}, left tile {metadata['left_tile']}, "
                f"top tile {metadata['top_tile']}."
            )
        else:
            west, south, east, north = metadata["bounding_box"]
            latitude = north - y / metadata["height"] * (north - south)
            longitude = west + x / metadata["width"] * (east - west)
            source_url = metadata["source_url"]
            image_reason = (
                f" Reviewed fuel installation bounds {review['canopy_pixel_bounds']}, "
                f"center {review['pixel_center']}, EPSG:4326 extent "
                f"{metadata['bounding_box']}."
            )
        candidates.append(
            {
                "opis_id": station_id,
                "latitude": latitude,
                "longitude": longitude,
                "source": "manually_reviewed_station_premises",
                "source_url": source_url,
                "external_id": f"reviewed-quarles-entrance/{station_id}"
                if review.get("coordinate_role") == "site_entrance"
                else f"reviewed-quarles-canopy/{station_id}",
                "matched_name": target["name"],
                "matched_address": target["official_address"],
                "csv_name": target["name"],
                "csv_city": target["city"],
                "csv_state": target["state"],
                "coordinate_role": review.get("coordinate_role", "fuel_canopy"),
                "estimated_coordinate_accuracy_meters": review.get(
                    "estimated_coordinate_accuracy_meters", 10
                ),
                "dispenser_position_known": review.get("coordinate_role") != "site_entrance",
                "identity_source_url": target["official_source_url"],
                "evidence": [
                    {
                        "source_url": target["official_source_url"],
                        "reason": f"Current official guide confirms the CSV Quarles number at {target['official_address']}, {target['city']}, {target['state']}.",
                    },
                    {
                        "source_url": target["network_source_url"],
                        "reason": target.get(
                            "network_identity_reason",
                            f"Public network record {target['network_ids']} names {target['network_names']} at the exact guide street and city/state. Its point is a search anchor; the actual visually reviewed fuel installation is returned.",
                        ),
                    },
                    {
                        "source_url": source_url,
                        "reason": review["reason"] + image_reason,
                    },
                    *review.get("extra_evidence", []),
                ],
            }
        )
        if not tile_variant:
            metadata.update(
                coordinate_verification="manually_reviewed_actual_fuel_premises",
                canopy_pixel_center=review["pixel_center"],
                canopy_pixel_bounds=review["canopy_pixel_bounds"],
                latitude=latitude,
                longitude=longitude,
                review_reason=review["reason"],
            )
            write_json(metadata_path, metadata)
    write_json(DATA / "supplemental-quarles-aerial-candidates.json", candidates)
    print({"requests": request_count, "reviewed_fuel_canopies": len(candidates)})


if __name__ == "__main__":
    main()
