"""Reproduce explicit supplemental station-premises conflict reviews offline."""

from __future__ import annotations

import json
import math
import sys

from loves_ta import DATA, write_json
from loves_ta_conflicts import distance, independent_records

# Each feature was inspected against the operator's numbered address. These are
# reviewed identities, not a nearest-station algorithm.
PREMISES = {
    20105: ("way", 877383155),
    64274: ("way", 1478450002),
    65686: ("way", 815912036),
    66461: ("way", 867504621),
    67664: ("node", 12403487158),
    68916: ("way", 1189775103),
    69110: ("node", 4340419706),
    69719: ("way", 1318569349),
    69830: ("way", 996684171),
    70893: ("way", 879812731),
    70906: ("way", 404114302),
    70983: ("way", 710868599),
    71090: ("way", 802747958),
    71116: ("way", 1386591175),
    71511: ("way", 1133961011),
    71573: ("node", 12686758627),
    71574: ("node", 12649899209),
    71686: ("way", 914588633),
    71839: ("way", 1308285388),
    72011: ("way", 1462682495),
    72013: ("way", 662537677),
    72014: ("way", 1226883084),
    72017: ("way", 1071068484),
    72443: ("way", 883982854),
    72554: ("node", 11491996376),
    72721: ("node", 12645870624),
    72761: ("node", 12649844385),
    73047: ("way", 1382750937),
    73129: ("node", 12077649254),
}

NOTES = {
    66461: "The primary locator's 110 Arnett Street point is 750 meters northeast of the premises. Independently mapped Yesway store way/679165488 has the exact 110 Arnett address; its adjacent Valero fuel building agrees with the primary Valero numbered property 0000027395. Use the mapped fuel premises, not the displaced Allsup's/Yesway feed pin.",
    67664: "The official CEFCO feed's Godley point is over 30 kilometers away. The exact-address named CEFCO fuel node and store way/573025359 corroborate the Corpay point and current USDA CEFCO 16 identity at 8845 North Highway 171. Historical Texas tank facility 79813 / RN103962106 reports differential GPS on the same property. The adjacent Shell has a different house number 8851 and is excluded.",
    70906: "The current numbered Quarles guide binds site 4403 to 11450 Baltimore Avenue, where the independently named Quarles diesel roof is mapped. Corpay and an older mapped Quarles point instead identify 11055 Baltimore Avenue about 800 meters away; they do not identify the current numbered property. Preserve their historical address evidence without using it as the current routing point.",
    70983: "The current numbered Quarles guide binds site 4943 to 3275 New Market Road. The mapped fuel roof and store both carry that exact house and street. Corpay instead publishes 3500 New Market Road at a different point; that older address does not locate the current guide's premises.",
    71116: "The CSV's CEFCO 101 identity is tied by historical address records to 549 South Walcott Street; the current primary Casey's 4797 page identifies that exact street. A separately mapped physical fuel roof lies 29 meters away, with no competing fuel feature within 500 meters. Corpay's point is in a different Texas city, roughly 575 kilometers away.",
    71573: "The primary RaceTrac 2588 Sligh page publishes 5951 US 301. The physical fuel node agrees within three meters and the RaceTrac shop is 36 meters away on the same forecourt. The shop's mapped house number is 5931; that numbering discrepancy is explicit and does not justify selecting Corpay's point 1.6 kilometers west.",
    71574: "The primary numbered RaceTrac 2594 Dublin page identifies 2262 South Highway 441. The separately mapped truck fuel node and service-area polygon agree with the primary point within 33 meters; the next fuel property is over 280 meters away. Corpay is about 6.5 kilometers north.",
    71686: "Primary AMBEST explicitly separates Allsup's 2391 at I-20 exit 269 from Allsup's 2390 at exit 270. Current USDA identifies Allsups 102391 at 9883 Interstate 20. Independently mapped Allsup's store way/914588634 at 1101 North 7th Street and its adjacent DK fuel roof agree with the numbered exit-269 AMBEST point within 20 meters. The Allsup's feed assigns its 1101 North 7th Street / 2391 record to the distinct exit-270 property 2.7 kilometers southeast; reject that displaced point while retaining its correct identity address.",
    72013: "Historical CEFCO 1081 records and the current primary Casey's 4881 page agree on 309 Highway 59 North. Named CEFCO store way/662537676 has the exact house, street and city; its adjoining Exxon fuel roof is returned. The competing Valero is on the opposite side of the highway at 402 Highway 59 South and is excluded.",
    72014: "Historical CEFCO 1086 identity and current primary Casey's 4783 page identify 306 South Eastman Road. The named CEFCO fuel roof is 87 meters from the primary current point, on its same eastern property. The nearer Valero roofs belong to the separately numbered 401 South Eastman property and are excluded. Another automated exact-street candidate at 3305 South Eastman is also a different property.",
    72554: "Current USDA record 1540149 binds CEFCO 422 to 5622 Highway 4, Baker. The independently mapped store explicitly names Cefco Convenience Store 422 at that same house and street and documents fuel availability. The original official feed incorrectly says 4622 and places the store about 46 kilometers away. The adjacent Ritz at 5612 Highway 4 is excluded. The returned coordinate is the verified station building; dispenser position is not claimed.",
    72721: "The primary numbered RaceTrac 2625 page identifies 1855 Highway 72 East at Shields Road. Independently mapped passenger and truck fuel nodes and their service-area polygon agree within 80 meters; the closest different Texaco is 180 meters west. Corpay's point is 2 kilometers east of this property.",
    72761: "The primary RaceTrac 2629 page and numbered Comdata directory bind the store to 76 Highway 20, using Tuscumbia/Muscle Shoals postal cities for the same premises. The mapped passenger fuel node agrees within eight meters, with truck fuel and its service area on the same property. Corpay is more than 25 kilometers east.",
    73129: "The primary Sheetz 837 page and federal AFDC station 397234 agree at 33380 Wick Road. The independently named physical Sheetz fuel node agrees within 21 meters. The mapped shop uses 33080, a stated house-number discrepancy, but belongs to the same property; Corpay's point is 829 meters west.",
}

SNAP_URL = "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/snap_retailer_location_data/FeatureServer/0"


def main() -> None:
    targets = json.loads((DATA / "supplemental-conflict-targets.json").read_text(encoding="utf-8"))
    records = {(r["type"], r["id"]): r for r in independent_records()}
    aerial_path = DATA / "supplemental-conflict-aerial-reviews.json"
    aerial = json.loads(aerial_path.read_text(encoding="utf-8")) if aerial_path.exists() else {}
    pending = json.loads(
        (DATA / "supplemental-conflict-pending-evidence.json").read_text(encoding="utf-8")
    )
    known_rejections = json.loads(
        (DATA / "supplemental-rejected-points.json").read_text(encoding="utf-8")
    )
    reviews = []
    exclusions = []
    unresolved = []
    for target in targets:
        station_id = target["opis_id"]
        image_review = aerial.get(str(station_id))
        identity = target.get("candidate") or (
            image_review.get("identity") if image_review else None
        )
        if (station_id not in PREMISES and image_review is None) or identity is None:
            unresolved.append(
                {
                    "opis_id": station_id,
                    **pending[str(station_id)],
                }
            )
            continue
        if image_review is not None:
            sys.path.insert(0, str(DATA.parents[1]))
            from scripts.coordinates.aerial_tiles import BASE, pixel_coordinate

            variant = image_review.get("image_variant", "supplemental-audit")
            image = DATA / "root-manual" / f"named-{station_id}-{variant}-tiles.json"
            metadata = json.loads(image.read_text(encoding="utf-8"))
            x, y = image_review["pixel_center"]
            left, top, right, bottom = image_review["review_pixel_bounds"]
            width, height = metadata["size"]
            if not 0 <= left < x < right <= width or not 0 <= top < y < bottom <= height:
                raise ValueError(f"Invalid visual review bounds for {station_id}")
            lat, lon = pixel_coordinate(
                x, y, metadata["left_tile"], metadata["top_tile"], metadata["zoom"]
            )
            osm_url = f"{BASE}/{metadata['zoom']}/{metadata['top_tile'] + math.floor(y / 256)}/{metadata['left_tile'] + math.floor(x / 256)}"
            witness = {
                "type": "reviewed-aerial-premises",
                "id": station_id,
                "latitude": lat,
                "longitude": lon,
                "tags": {"amenity": "fuel"},
            }
            if image_review.get("coordinate_role", "fuel_canopy") == "fuel_canopy":
                witness["tags"]["building"] = "roof"
        else:
            witness = records[PREMISES[station_id]]
            osm_url = f"https://www.openstreetmap.org/{witness['type']}/{witness['id']}"
        evidence = list(identity["evidence"])
        reason = NOTES.get(
            station_id,
            "The operator's numbered station/address identity agrees with independently mapped named fuel or station premises. The selected mapped feature belongs to this exact property; conflicting displaced points do not.",
        )
        if image_review is not None:
            reason = image_review["reason"]
            evidence.extend(image_review.get("extra_evidence", []))
            reason += f" Reviewed premises pixel {image_review['pixel_center']}, bounds {image_review['review_pixel_bounds']}, EPSG:3857 XYZ mosaic at zoom {metadata['zoom']}, left tile {metadata['left_tile']}, top tile {metadata['top_tile']}."
        evidence.append({"source_url": osm_url, "reason": reason, "tags": witness["tags"]})
        if station_id in {66461, 67664, 71686, 72554}:
            evidence.append(
                {
                    "source_url": SNAP_URL,
                    "reason": "Cached current USDA retailer identity corroborates the numbered store and exact street address; its coordinate is only supporting search evidence.",
                }
            )
        if station_id == 71686:
            evidence.append(
                {
                    "source_url": "https://am-best.com/ambest_new/public/api/get-user-locations-and-fuel-price",
                    "reason": "Primary record 11009 explicitly identifies Allsups 2391-Merkel at I-20 exit 269; separate record 11008 identifies 2390 at exit 270.",
                }
            )
        row = target["row"]
        accepted = {
            **identity,
            "source": "manually_reviewed_station_premises",
            "source_url": osm_url,
            "external_id": f"{witness['type']}/{witness['id']}",
            "latitude": witness["latitude"],
            "longitude": witness["longitude"],
            "evidence": evidence,
            "coordinate_role": "fuel_canopy"
            if witness["tags"].get("building") == "roof"
            else ("fuel_point" if witness["tags"].get("amenity") == "fuel" else "station_building"),
            "csv_name": row["Truckstop Name"],
            "csv_address": row["Address"],
            "csv_city": row["City"],
            "csv_state": row["State"],
            "rejected_candidates": [],
        }
        if image_review is not None:
            accepted["estimated_coordinate_accuracy_meters"] = 8
            accepted["coordinate_role"] = image_review.get("coordinate_role", "fuel_canopy")
        if station_id == 72554:
            accepted["matched_address"] = "5622 Highway 4"
        conflict = target.get("conflict") or {}
        seen = set()
        for candidate in conflict.get("candidates", []):
            meters = distance(candidate, accepted)
            key = (
                candidate["source_url"],
                str(candidate.get("external_id", "")),
                candidate["latitude"],
                candidate["longitude"],
            )
            if meters <= 400 or key in seen:
                continue
            seen.add(key)
            rejection = {
                "opis_id": station_id,
                "source_url": candidate["source_url"],
                "external_id": str(candidate.get("external_id", "")),
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "reason": f"Point is {meters:.0f} meters from the verified numbered station premises. {reason}",
                "evidence": [{"source_url": osm_url, "reason": reason}],
            }
            accepted["rejected_candidates"].append(rejection)
            exclusions.append(rejection)
        for rejection in known_rejections:
            if rejection["opis_id"] != station_id:
                continue
            key = (
                rejection["source_url"],
                str(rejection["external_id"]),
                rejection["latitude"],
                rejection["longitude"],
            )
            if key not in seen:
                seen.add(key)
                accepted["rejected_candidates"].append(rejection)
                exclusions.append(rejection)
        if not accepted["rejected_candidates"]:
            raise ValueError(f"Conflict {station_id} has no specifically rejected point")
        reviews.append(accepted)
    write_json(DATA / "supplemental-conflict-reviews.json", reviews)
    write_json(DATA / "supplemental-conflict-rejected-points.json", exclusions)
    write_json(DATA / "supplemental-conflict-unresolved.json", unresolved)
    print(
        json.dumps(
            {
                "reviewed": len(reviews),
                "rejected_points": len(exclusions),
                "unresolved": len(unresolved),
            }
        )
    )


if __name__ == "__main__":
    main()
