"""Local station filtering and point-to-segment projection; no external I/O."""

import json
import math
from functools import lru_cache
from itertools import pairwise
from pathlib import Path

from pyproj import Geod, Transformer
from shapely import STRtree, make_valid, prepare
from shapely.geometry import LineString, Point, box, shape
from shapely.ops import unary_union

from .fuel import LocatedStation, miles

GEOD = Geod(ellps="WGS84")
METERS_PER_MILE = 1609.344


@lru_cache(maxsize=1)
def us_boundary():
    path = Path(__file__).parent / "data" / "us-states.geojson"
    with path.open(encoding="utf-8") as handle:
        features = json.load(handle)["features"]
    boundary = unary_union([make_valid(shape(feature["geometry"])) for feature in features])
    prepare(boundary)
    return boundary


def in_usa(longitude, latitude):
    return us_boundary().covers(Point(longitude, latitude))


class StationIndex:
    def __init__(self, stations):
        self.stations = tuple(stations)
        self.tree = STRtree([Point(s.longitude, s.latitude) for s in self.stations])

    def along_route(self, route, corridor_miles):
        if not self.stations or route.distance_miles == 0:
            return []
        coordinates = route.coordinates
        radius = float(corridor_miles) * METERS_PER_MILE
        # This conservative angular envelope only selects candidates; it does
        # not supply road distances or claim to measure a driving detour.
        max_latitude = max(abs(lat) for _, lat in coordinates)
        padding = radius / (110_000 * max(math.cos(math.radians(max_latitude)), 0.02))
        candidate_ids = self.tree.query(
            LineString(coordinates).buffer(padding), predicate="intersects"
        )
        if len(candidate_ids) == 0:
            return []

        latitude = sum(lat for _, lat in coordinates) / len(coordinates)
        longitude = math.degrees(
            math.atan2(
                sum(math.sin(math.radians(lon)) for lon, _ in coordinates),
                sum(math.cos(math.radians(lon)) for lon, _ in coordinates),
            )
        )
        local = f"+proj=aeqd +lat_0={latitude} +lon_0={longitude} +datum=WGS84 +units=m"
        forward = Transformer.from_crs("EPSG:4326", local, always_xy=True)
        inverse = Transformer.from_crs(local, "EPSG:4326", always_xy=True)
        xs, ys = forward.transform(*zip(*coordinates))
        projected = list(zip(xs, ys))
        segments, vertex_ids = [], []
        for index, (start, end) in enumerate(pairwise(projected)):
            if start != end:
                segments.append(LineString([start, end]))
                vertex_ids.append(index)
        if not segments:
            return []
        segment_tree = STRtree(segments)
        road_miles = route.vertex_miles()
        matches = []
        for candidate_id in candidate_ids:
            station = self.stations[int(candidate_id)]
            point = Point(*forward.transform(station.longitude, station.latitude))
            visits = {}
            _, distances = segment_tree.query_nearest(
                point, return_distance=True, all_matches=False
            )
            nearest_distance = float(distances[0])
            tolerance = 0.01  # One centimetre resolves floating-point ties on retraced roads.
            search_radius = nearest_distance + tolerance
            nearby = segment_tree.query(
                box(
                    point.x - search_radius,
                    point.y - search_radius,
                    point.x + search_radius,
                    point.y + search_radius,
                )
            )
            for segment_id in nearby:
                segment_id = int(segment_id)
                segment = segments[segment_id]
                if segment.distance(point) > nearest_distance + tolerance:
                    continue
                fraction = segment.project(point) / segment.length
                vertex = vertex_ids[segment_id]
                position = road_miles[vertex] + fraction * (
                    road_miles[vertex + 1] - road_miles[vertex]
                )
                nearest = segment.interpolate(fraction, normalized=True)
                lon, lat = inverse.transform(nearest.x, nearest.y)
                deviation = abs(GEOD.inv(station.longitude, station.latitude, lon, lat)[2])
                if deviation <= radius and 0 <= position < float(route.distance_miles):
                    position = miles(position)
                    previous = visits.get(position)
                    if previous is None or deviation < previous:
                        visits[position] = deviation
            # Retraced roads can revisit a station. Adjacent segments meeting
            # at a vertex still describe one visit at the same route mile.
            matches.extend(
                LocatedStation(station, position, deviation / METERS_PER_MILE)
                for position, deviation in visits.items()
            )
        return sorted(matches, key=lambda item: (item.mile, item.station.opis_id))
