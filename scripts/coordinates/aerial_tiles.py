"""Cache a georeferenced public aerial mosaic for an explicit station review.

Cached images are evidence for manual review, never automatic station candidates.
"""

import argparse
import io
import json
import math
import re
import time
from datetime import UTC, datetime

import httpx

from scripts.coordinates.match_osm import SOURCES

BASE = "https://services.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile"
TILE_SIZE = 256


def tile_position(latitude, longitude, zoom):
    scale = 2**zoom
    x = (longitude + 180) / 360 * scale
    y = (1 - math.asinh(math.tan(math.radians(latitude))) / math.pi) / 2 * scale
    return x, y


def pixel_coordinate(x, y, left_tile, top_tile, zoom):
    scale = 2**zoom
    longitude = (left_tile + x / TILE_SIZE) / scale * 360 - 180
    latitude = math.degrees(
        math.atan(math.sinh(math.pi * (1 - 2 * (top_tile + y / TILE_SIZE) / scale)))
    )
    return latitude, longitude


def matches_cached_location(metadata, latitude, longitude, zoom):
    expected = metadata.get("search_coordinate", [])
    return (
        metadata.get("zoom") == zoom
        and len(expected) == 2
        and all(
            math.isclose(actual, wanted, rel_tol=0, abs_tol=1e-9)
            for actual, wanted in zip(expected, [longitude, latitude], strict=True)
        )
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("opis_id", type=int)
    parser.add_argument("latitude", type=float)
    parser.add_argument("longitude", type=float)
    parser.add_argument("--zoom", type=int, default=17, choices=range(15, 20))
    parser.add_argument("--download", action="store_true")
    parser.add_argument("--variant", default="")
    args = parser.parse_args()
    if not -85 < args.latitude < 85 or not -180 <= args.longitude <= 180:
        parser.error("Coordinates are outside the supported imagery range.")
    if args.variant and not re.fullmatch(r"[a-z0-9-]{1,32}", args.variant):
        parser.error("Variant must contain 1-32 lowercase letters, digits or hyphens.")
    folder = SOURCES / "root-manual"
    folder.mkdir(exist_ok=True)
    suffix = f"-{args.variant}" if args.variant else ""
    image_path = folder / f"named-{args.opis_id}{suffix}-tiles.png"
    if image_path.exists():
        metadata = json.loads(image_path.with_suffix(".json").read_text())
        if not matches_cached_location(metadata, args.latitude, args.longitude, args.zoom):
            parser.error("Cached imagery has a different center or zoom. Use a new --variant.")
        print({"cached": str(image_path), "requests": 0})
        return
    if not args.download:
        print("No image cached. Use --download to request nine public map tiles.")
        return
    from PIL import Image

    x, y = tile_position(args.latitude, args.longitude, args.zoom)
    left, top = math.floor(x) - 1, math.floor(y) - 1
    mosaic = Image.new("RGB", (TILE_SIZE * 3, TILE_SIZE * 3))
    urls = []
    with httpx.Client(timeout=20) as client:
        for row in range(3):
            for column in range(3):
                url = f"{BASE}/{args.zoom}/{top + row}/{left + column}"
                path = folder / f"tile-{args.zoom}-{top + row}-{left + column}.jpg"
                if not path.exists():
                    time.sleep(1)
                    response = client.get(url)
                    response.raise_for_status()
                    with Image.open(io.BytesIO(response.content)) as tile:
                        if tile.size != (TILE_SIZE, TILE_SIZE):
                            raise ValueError("Unexpected public tile dimensions.")
                    path.write_bytes(response.content)
                with Image.open(path) as tile:
                    mosaic.paste(tile, (column * TILE_SIZE, row * TILE_SIZE))
                urls.append(url)
    mosaic.save(image_path)
    image_path.with_suffix(".json").write_text(
        json.dumps(
            {
                "source_urls": urls,
                "source_url": f"{BASE}/{args.zoom}/{math.floor(y)}/{math.floor(x)}",
                "retrieved_at": datetime.now(UTC).isoformat(),
                "opis_id": args.opis_id,
                "zoom": args.zoom,
                "left_tile": left,
                "top_tile": top,
                "size": list(mosaic.size),
                "image_projection": "EPSG:3857 XYZ tiles",
                "search_coordinate": [args.longitude, args.latitude],
                "search_pixel": [(x - left) * TILE_SIZE, (y - top) * TILE_SIZE],
                "verification": "pending_manual_fuel_premises_review",
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print({"cached_aerial": args.opis_id, "tiles": len(urls)})


if __name__ == "__main__":
    main()
