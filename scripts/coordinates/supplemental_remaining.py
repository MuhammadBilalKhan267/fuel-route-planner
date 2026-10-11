"""Reproduce individual street, rebrand and actual-premises reviews offline."""

from __future__ import annotations

import json

from loves_ta import DATA, write_json
from supplemental import targets

REVIEWS = {
    68112: (
        "minnesota",
        "way",
        1466263664,
        "3079 Neil Armstrong Boulevard, Eagan, MN 55121",
        "https://www.quickfuel.com/locations/minnesota/eagan-3079-neil-armstrong-blvd",
        "The CSV identifies Quick Fuel in Saint Paul at I-35 exit 98 and County Road 26. The independently named highway directory lists Quick Fuel Eagan at I-35E exit 98 using Saint Paul as its city. Independent road mapping identifies the adjacent Lone Oak Road as County Highway 26. The current operator locator and historical NRHA numbered directory both confirm the sole Eagan Quick Fuel at 3079 Neil Armstrong Boulevard. Its primary locator coordinate agrees with the actual independently mapped Quick Fuel installation within one meter. The I-35E branch and Eagan municipal city are recorded explicitly; the CSV's legacy number 3138 is not asserted to be an alias of historical 1601.",
    ),
    5030: (
        "iowa",
        "way",
        482784803,
        "1317 East Mary Street",
        "https://ottumwa.us/files/council_meetings/packet_2024-10-01_64521.pdf",
        "Ottumwa's official licensing packet identifies Yesway #1030 at 1317 East Mary Street. The independently named Yesway fuel canopy is on the same mapped property as that exact-address restaurant, 23 meters from the documented street's Census search anchor.",
    ),
    10135: (
        "kansas",
        "node",
        11200072994,
        "1035 West Kansas Avenue",
        "https://services1.arcgis.com/RLQu0rK7h4kbsBq5/arcgis/rest/services/snap_retailer_location_data/FeatureServer/0",
        "Current USDA retailer records explicitly identify Yesway 1116 at 1035 West Kansas Avenue, McPherson. The mapped actual fuel point is 31 meters from the retailer address point and 34 meters from its independently acquired Census street anchor; the next mapped fuel station is more than 1.2 kilometers away. The actual mapped fuel point is returned.",
    ),
    69993: (
        "texas",
        "way",
        1015358574,
        "3025 East Austin Street",
        "https://www.caseys.com/general-store/tx-giddings/3025-east-austin-street/4764",
        "Historical USDA identity confirms CEFCO 99 at 3025 East Austin Street, Giddings. The current official Casey's #4764 page identifies the identical street and active diesel/truck facilities. Fuelman's current Casey's point agrees with the independently named CEFCO fuel canopy within four meters; the mapped canopy is returned.",
    ),
    72019: (
        "texas",
        "way",
        1278745101,
        "905 North Broadway Street",
        "https://www.caseys.com/general-store/tx-post/905-north-broadway/4866",
        "Historical USDA and fuel-card records identify CEFCO 2081 at 905 North Broadway Street, Post. The mapped CEFCO fuel canopy has that exact house, street, city and ZIP, and the current official Casey's #4866 page confirms active diesel fuel at the same address. The legacy Fuelman CEFCO point in Mississippi is rejected.",
    ),
    72479: (
        "texas",
        "way",
        1317478256,
        "2091 East Roby Highway",
        "https://www.caseys.com/general-store/tx-snyder/2091-e-roby-hwy/4740",
        "Primary Comdata and Texas environmental records identify CEFCO 2086 at 2091 East Roby Highway. The current official Casey's #4740 page confirms that identical street and active diesel/DEF/truck facilities. The independently named Casey's fuel premises agree with Fuelman's current numbered Casey's record within 23 meters. The mapped actual fueling property is returned.",
    ),
    65221: (
        "mississippi",
        "node",
        7644144414,
        "4200 Wynndale Road",
        "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        "The NRHA primary directory identifies CEFCO 544 at 4200 Wyndale, Terry; USDA confirms the normalized 4200 Wynndale Road identity. The mapped actual fuel station named Cefci is at CSV I-55 exit81. Current Fuelman Refuel 210 at the exact 4200 Wynndale street agrees with this mapped fuel point within 66 meters. The misspelled former operator name is recorded as this individual review; raw address geocodes are not returned.",
    ),
    72828: (
        "texas",
        "node",
        2375266973,
        "1393A Highway 80 East",
        "https://www.quarlesinc.com/wp-content/uploads/2026/10/QFF-pocket-site-guide-v10-1-26.pdf",
        "The current official Quarles guide identifies #54250 at 1393A Highway 80 East, Abilene. Census confirms this numbered highway street as a search anchor, 45 meters from the independently named Gascard fuel station beside US80. The actual mapped Gascard fuel point is returned; the inconsistent guide ZIP is not used to select another site.",
    ),
    72844: (
        "texas",
        "node",
        1581333003,
        "201 East 3rd Street",
        "https://www.quarlesinc.com/pecos-texas-fleet-fueling-sites/",
        "The current official Quarles guide identifies #54332 at 201 East 3rd Street, Pecos. The official Pecos page explicitly names this former GASCARD fuel site at the identical street, and its independently named GasCard fuel point is 67 meters from the exact Census street anchor. The mapped actual fuel point is returned.",
    ),
    61149: (
        "mississippi",
        "node",
        13418737452,
        "9733 Old Highway 45 North",
        "https://apps.msdh.ms.gov/food/FacilityRecordDetailsNewInspection.aspx?InspectionNumber=633100&PimsID=6244337&inspectionListing1Dir=Asc&inspectionListing1PageSize=30",
        "The primary NRHA directory and Mississippi health inspections identify former CEFCO 501 at 9733 Old Highway45 North, Lauderdale. The current state inspection identifies Double Quick203 as open at the exact same numbered street. Independently named Double Quick fuel and store premises agree on the mapped property south of the adjacent Dollar General. The mapped actual fuel point is returned, rather than the network's point 296 meters northeast beside the post office.",
    ),
    70797: (
        "texas",
        "node",
        10772528339,
        "1705 Briercroft Court, Carrollton, TX 75006",
        "https://www.linkedin.com/company/quick-fuel-automated-fueling",
        "The CSV identifies Quick Fuel in Carrollton at I-35E exit 442. The public iExit numbered-exit listing independently identifies Quick Fuel at 1705 Briercroft Court at that exact exit. The current fuel-network record 341632 identifies the same operator, city, state and street, within 19 meters of the independently mapped actual fuel point. The operator's own public company page and historical site directory identify its sole Carrollton Briercroft location as 1709, a recorded house-number difference. The site is accepted through the unique operator, city and exact highway-exit identity, corroborated by the actual mapped fuel installation. The CSV legacy number 3176 is not asserted to be an alias of current number 5304.",
    ),
}


def main() -> None:
    rows = {int(row["OPIS Truckstop ID"]): row for row in targets()}
    maps = {}
    candidates = []
    for station_id, (
        state_file,
        osm_type,
        osm_id,
        address,
        identity_url,
        reason,
    ) in REVIEWS.items():
        if state_file not in maps:
            records = json.loads(
                (DATA / "osm-states" / f"{state_file}.json").read_text(encoding="utf-8")
            )["records"]
            maps[state_file] = {(record["type"], record["id"]): record for record in records}
        point = maps[state_file][(osm_type, osm_id)]
        row = rows[station_id]
        url = f"https://www.openstreetmap.org/{osm_type}/{osm_id}"
        candidates.append(
            {
                "opis_id": station_id,
                "latitude": point["latitude"],
                "longitude": point["longitude"],
                "source": "manually_reviewed_station_premises",
                "source_url": url,
                "external_id": f"{osm_type}/{osm_id}",
                "matched_name": row["Truckstop Name"],
                "matched_address": address,
                "identity_source_url": identity_url,
                "csv_name": row["Truckstop Name"],
                "csv_address": row["Address"],
                "csv_city": row["City"],
                "csv_state": row["State"],
                "evidence": [
                    {"source_url": identity_url, "reason": reason},
                    {
                        "source_url": url,
                        "reason": "Actual mapped amenity=fuel point provides the station-premises coordinate.",
                    },
                ],
            }
        )
        if station_id == 72019:
            candidates[-1]["rejected_candidates"] = [
                {
                    "source_url": "https://sitelocator.fuelman.com/bin/sl/search?network=fuelman&zoom=10&fq=serviceTypes%3ADiesel&bounds=18%2C-180%2C72%2C-65&rows=50000",
                    "external_id": "200019",
                    "latitude": 32.414846,
                    "longitude": -88.65462,
                    "reason": "Network CEFCO 2081 street is Post, TX, but this coordinate is Mississippi, approximately 1,200 kilometers from the independently mapped exact-address fuel canopy in Texas.",
                }
            ]
        if station_id == 70797:
            candidates[-1].update(
                identity_match_method="unique_operator_city_highway_exit_and_actual_premises",
                legacy_store_number_verified=False,
                current_store_number="5304",
            )
            candidates[-1]["evidence"].extend(
                [
                    {
                        "source_url": "https://www.iexitapp.com/business/Quick%20Fuel/837129?esmid=49599&ratesaver=on",
                        "reason": "The independently indexed exit listing identifies Quick Fuel at 1705 Briercroft Court, Carrollton, and explicitly lists I-35 exit 442. The raw page returned HTTP 403 and was not retried; the indexed numbered-exit evidence is preserved separately.",
                    },
                    {
                        "source_url": "https://2019.dhxadv.com/quickfuel/images/Quick_Fuel_Site_List_w_CFN_Site_Numbers_5_3_19.pdf",
                        "reason": "The historical operator directory separately records its Carrollton site at 1709 Briercroft Court as CFN 6933. This supports the physical street identity without claiming a legacy 3176 number alias.",
                    },
                ]
            )
        if station_id == 68112:
            candidates[-1].update(
                identity_match_method="unique_operator_city_highway_exit_and_actual_premises",
                legacy_store_number_verified=False,
                historical_store_number="1601",
                csv_highway_branch_unspecified=True,
                verified_highway="I-35E",
                verified_exit="98",
            )
            candidates[-1]["evidence"].extend(
                [
                    {
                        "source_url": "https://www.findfuelstops.com/truck-stop-on-I-35E",
                        "reason": "The named highway directory explicitly lists Quick Fuel Eagan 1601 at I-35E exit 98 and Saint Paul, matching the CSV operator, city label and exact exit.",
                    },
                    {
                        "source_url": "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
                        "reason": "The historical primary fuel directory identifies Quick Fuel 1601 at the exact 3079 Neil Armstrong Boulevard street. It corroborates the physical site without claiming a legacy 3138 alias.",
                    },
                    {
                        "source_url": "https://www.openstreetmap.org/way/1078823315",
                        "reason": "Independent mapping identifies the adjacent Lone Oak Road as CH 26, corroborating the CSV's County Road 26 junction.",
                    },
                ]
            )
    # Direction order differs in historical and current street labels. This is
    # a specific reviewed physical-address join, not a generalized fuzzy match.
    caseys = json.loads((DATA / "regional-caseys-locations.json").read_text(encoding="utf-8"))
    point = next(record for record in caseys if record["external_id"] == "4881")
    row = rows[72013]
    candidates.append(
        {
            "opis_id": 72013,
            "latitude": point["latitude"],
            "longitude": point["longitude"],
            "source": "manually_reviewed_station_premises",
            "source_url": point["source_url"],
            "external_id": "4881",
            "matched_name": "Casey's #4881 (formerly CEFCO #1081)",
            "matched_address": point["address"],
            "csv_name": row["Truckstop Name"],
            "csv_address": row["Address"],
            "csv_city": row["City"],
            "csv_state": row["State"],
            "evidence": [
                {
                    "source_url": "https://fns-prod.azureedge.us/sites/default/files/resource-files/snap-retailer-locator-data2005-2025.zip",
                    "reason": "USDA identifies CEFCO 1081 at 309 North Highway59, Queen City. The current official Casey's #4881 locator uses 309 Highway59 North, the same numbered highway address.",
                },
                {
                    "source_url": "https://www.openstreetmap.org/way/662537676",
                    "reason": "Independent CEFCO fuel/store mapping explicitly identifies house309, USHighway59 North, Queen City, TX75572, corroborating the current operator's point on the same property.",
                },
            ],
        }
    )
    candidates.extend(aerial_reviews(rows))
    write_json(DATA / "supplemental-other-premises-candidates.json", candidates)
    print({"additional_reviewed_premises": len(candidates)})


def aerial_reviews(rows: dict[int, dict]) -> list[dict]:
    """Use visibly identified fuel canopies, with separately proved site identity."""
    reviews = {
        64479: {
            "pixel_center": [452, 447],
            "canopy_pixel_bounds": [413, 415, 497, 482],
            "address": "1701 Iowa Avenue East, Marshalltown, IA 50158",
            "identity_url": "https://marshalltownia.api.civicclerk.com/v1/Meetings/GetMeetingFileStream%28fileId%3D8452%2CplainText%3Dfalse%29",
            "reason": "The March2026 primary city license renewal explicitly identifies Yesway1148 at1701IowaAvenueEast and confirms its planned sale did not complete. Current Iowa DNR facility310468096 identifies active underground fuel tanks for the exact numbered Yesway/address. The independently inspected aerial image shows the actual retail fuel canopy, truck diesel canopy and truck parking on this property at US30exit186. The reviewed retail canopy center agrees with current Fuelman Yesway1148 coordinates within four meters; the Census road-interpolation point640meters west is rejected.",
            "extra_evidence": [
                {
                    "source_url": "https://facilityexplorer.iowadnr.gov/FacilityExplorer/SiteDetail.aspx?facID=310468096",
                    "reason": "Iowa DNR confirms Yesway1148 at the numbered street and Regulated Active underground storage tanks199918041. The DNR address-matching location is only corroborating search evidence; the returned coordinate comes from the visible fuel canopy.",
                }
            ],
        },
        71132: {
            "pixel_center": [590, 240],
            "canopy_pixel_bounds": [528, 213, 649, 264],
            "address": "700 North Railroad Avenue, Mount Enterprise, TX 75681",
            "identity_url": "https://records.tceq.texas.gov/cs/idcplg?IdcService=TCEQ_EXTERNAL_SEARCH_GET_FILE&Rendition=Web&dID=7489338",
            "reason": "Primary TCEQ record explicitly identifies CEFCO104, facility91190, RN111019907 at700NorthRailroadAvenue, MountEnterprise. The current operator is Casey's4811 at the identical street, corroborated by state accessibility registration and current diesel-network record575332. The independently inspected aerial image shows the actual retail and truck fuel canopies on the north side of the US259 junction. The reviewed retail canopy center is returned. Legacy network, USDA and secondary sample points south of the fuel premises are rejected.",
            "extra_evidence": [
                {
                    "source_url": "https://www.tdlr.texas.gov/TABS/Search/Print/TABS2025020857",
                    "reason": "Current state accessibility filing identifies Casey's4811 at700NorthRailroadAvenue, MountEnterprise; its April2026 inspection concerns remodeling the same store.",
                }
            ],
        },
        70796: {
            "pixel_center": [480, 442],
            "canopy_pixel_bounds": [469, 413, 491, 474],
            "address": "2451 Great Southwest Parkway, Fort Worth, TX 76106",
            "identity_url": "https://www.linkedin.com/company/quick-fuel-automated-fueling",
            "reason": "The CSV identifies Quick Fuel in Fort Worth at I-35W exit 56. The current public numbered-exit listing identifies the operator's 2451 Great Southwest Parkway site at exit 56A. The operator's public company page and historical CFN site directory confirm that exact street and city. Current network record 341633 identifies Quick Fuel 5305 at the same street. The independently inspected aerial image shows the actual small fuel canopy and separate fueling access loop on the mapped 2451 Great Southwest property; the reviewed canopy center is returned. The operator's other historical Fort Worth-area branch is in Haltom City on Minnis Drive and is not the property in the exit listing. The CSV legacy number 3177 is not asserted to be a current-number alias.",
            "extra_evidence": [
                {
                    "source_url": "https://www.iexitapp.com/Texas/I-35W/Exit%2056A/2795?fuel_type_id=5&origin=gas_prices",
                    "reason": "The independently indexed I-35W exit 56A listing explicitly identifies Quick Fuel at 2451 Great Southwest Parkway, Fort Worth, 1.44 road miles northwest of the exit. No competing Quick Fuel property appears in that exact numbered-exit listing.",
                },
                {
                    "source_url": "https://2019.dhxadv.com/quickfuel/images/Quick_Fuel_Site_List_w_CFN_Site_Numbers_5_3_19.pdf",
                    "reason": "The historical operator directory records the Great Southwest property as CFN 6934 and a separate Haltom City property as 6935 at 2509 Minnis Drive. It supplies street identity, without proving a legacy 3177 alias.",
                },
                {
                    "source_url": "https://www.openstreetmap.org/way/497725046",
                    "reason": "Independent mapping places the visible canopy on the property explicitly numbered 2451 Great Southwest Parkway.",
                },
            ],
        },
    }
    candidates = []
    for station_id, review in reviews.items():
        metadata_path = DATA / f"supplemental-premises-aerial-{station_id}.json"
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        west, south, east, north = metadata["bounding_box"]
        x, y = review["pixel_center"]
        left, top, right, bottom = review["canopy_pixel_bounds"]
        if not (
            0 <= left < x < right <= metadata["width"]
            and 0 <= top < y < bottom <= metadata["height"]
        ):
            raise ValueError(f"Invalid reviewed canopy bounds for {station_id}")
        latitude = north - y / metadata["height"] * (north - south)
        longitude = west + x / metadata["width"] * (east - west)
        metadata.update(
            coordinate_verification="manually_reviewed_actual_fuel_canopy",
            canopy_pixel_center=[x, y],
            canopy_pixel_bounds=[left, top, right, bottom],
            latitude=latitude,
            longitude=longitude,
            estimated_coordinate_accuracy_meters=8,
            review_reason=review["reason"],
        )
        write_json(metadata_path, metadata)
        row = rows[station_id]
        candidates.append(
            {
                "opis_id": station_id,
                "latitude": latitude,
                "longitude": longitude,
                "source": "manually_reviewed_station_premises",
                "source_url": metadata["source_url"],
                "external_id": f"reviewed-canopy/{station_id}",
                "matched_name": row["Truckstop Name"],
                "matched_address": review["address"],
                "identity_source_url": review["identity_url"],
                "csv_name": row["Truckstop Name"],
                "csv_address": row["Address"],
                "csv_city": row["City"],
                "csv_state": row["State"],
                "coordinate_role": "fuel_canopy",
                "estimated_coordinate_accuracy_meters": 8,
                "evidence": [
                    {"source_url": review["identity_url"], "reason": review["reason"]},
                    {
                        "source_url": metadata["source_url"],
                        "reason": f"Manually inspected georeferenced aerial image: actual fuel canopy bounds {review['canopy_pixel_bounds']}, center {review['pixel_center']}, EPSG4326 extent {metadata['bounding_box']}. The reviewed fuel-canopy center supplies the coordinate.",
                    },
                    *review["extra_evidence"],
                ],
            }
        )
        if station_id == 70796:
            candidates[-1].update(
                identity_match_method="unique_operator_city_highway_exit_and_actual_premises",
                legacy_store_number_verified=False,
                current_store_number="5305",
            )
    return candidates


if __name__ == "__main__":
    main()
