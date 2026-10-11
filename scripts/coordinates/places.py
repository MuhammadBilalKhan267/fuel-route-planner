"""Use public city boundaries to qualify named OSM premises with no city tag."""

import io
import json
import zipfile
from collections import defaultdict
from pathlib import Path

from shapely import make_valid, prepare
from shapely.geometry import Point, shape

from scripts.coordinates.matching import normalized

ROOT = Path(__file__).resolve().parents[2]
SOURCE_URL = "https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_place_500k.zip"


def place_boundaries(rows):
    import shapefile

    wanted = {(row["State"], normalized(row["City"])) for row in rows.values()}
    states = {
        item["properties"]["STATE"]: item["properties"]["STUSAB"]
        for item in json.loads((ROOT / "trips/data/us-states.geojson").read_text())["features"]
    }
    places = defaultdict(list)
    with zipfile.ZipFile(ROOT / "data/coordinate-sources/census2025places.zip") as archive:
        prefix = "cb_2025_us_place_500k"
        reader = shapefile.Reader(
            shp=io.BytesIO(archive.read(prefix + ".shp")),
            shx=io.BytesIO(archive.read(prefix + ".shx")),
            dbf=io.BytesIO(archive.read(prefix + ".dbf")),
            encoding="utf-8",
        )
        for item in reader.iterShapeRecords():
            fields = item.record.as_dict()
            key = states.get(fields["STATEFP"]), normalized(fields["NAME"])
            if key in wanted:
                geometry = make_valid(shape(item.shape.__geo_interface__))
                prepare(geometry)
                places[key].append(geometry)
    return places


def inside_named_place(row, point, places):
    location = Point(point["longitude"], point["latitude"])
    return any(
        boundary.covers(location)
        for boundary in places.get((row["State"], normalized(row["City"])), [])
    )
