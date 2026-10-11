"""Reproduce reviewed identity exceptions from cached primary evidence."""

from __future__ import annotations

import json

from loves_ta import DATA, write_json
from loves_ta_conflicts import distance


def main() -> None:
    records = json.loads((DATA / "supplemental-source-records.json").read_text(encoding="utf-8"))
    rows = json.loads((DATA / "supplemental-target-rows.json").read_text(encoding="utf-8"))
    identities = json.loads(
        (DATA / "supplemental-quickfuel-identity-records.json").read_text(encoding="utf-8")
    )
    reviews = []
    choices = {
        71116: (
            "CEFCO",
            "4797",
            "Current official Casey's #4797 and legacy official CEFCO #101 both identify 549 South Walcott Street, Jefferson. USDA and the primary fuel-card directory confirm CEFCO #101 at the identical street. The current Casey's point is retained for the rebranded premises.",
            "https://www.caseys.com/general-store/tx-jefferson/549-south-walcott-street/4797",
        ),
        72021: (
            "CEFCO",
            "4712",
            "Current official Casey's #4712 and legacy official CEFCO #30 both identify 111 West Gassaway, Lott. USDA and the primary fuel-card directory confirm CEFCO #30 at the identical street. The current Casey's point is retained for the rebranded premises.",
            "https://www.caseys.com/general-store/tx-lott/111-west-gassaway/4712",
        ),
        68685: (
            "QuickFuel",
            "63",
            "The NRHA primary directory, page 84, identifies Iron Street #2403 at 1225 Iron Street, Kansas City, MO. Fuelman's Quickfuel-Iron St at this exact address agrees within seven meters with the current official Quick Fuel point at 1201 Iron Street, North Kansas City. The house-number and municipal-city variants are specific to this documented fueling property.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        71841: (
            "QuickFuel",
            "86",
            "Trucker Path's station directory identifies Quick Fuel #3193 at 401 Kindelberger Road, Kansas City, KS. The current official Quick Fuel locator identifies this same street address as 401 Kindleberger Road. The reversed letters are recorded as this individual identity exception.",
            "https://truckerpath.com/locations/diesel-stations-near-me/kansas/kansas-city/",
        ),
        66810: (
            "QuickFuel",
            "51",
            "Illinois State Fire Marshal UST facility 2036100 identifies active Flyers Energy d/b/a Quick Fuel #3145 at 1285 North Ellis, Bensenville, matching the current official locator's street address and diesel tank status.",
            "https://webapps.sfm.illinois.gov/ustsearch/Facility.aspx?ID=2036100",
        ),
        70798: (
            "QuickFuel",
            "52",
            "The NRHA primary fuel directory identifies Quick Fuel Janesville #1926 at 3500 Newville Road; the current official locator specifies 3500 North Newville Road in the same city. The omitted North is recorded as an individual identity exception.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        71946: (
            "Allsup's",
            "12448",
            "Orion Foods Systems' 2026 Minnesota franchise disclosure document, page 133, identifies Yesway/Allsups 102450 at 502 West Broadway, Eden, TX 76837. This exact address joins the CSV identity to official Allsup's #2450; USDA's East directional label is inconsistent and its point is not used.",
            "https://pub-22da695f2b6542a48e234bc59bc67d23.r2.dev/fdds/2026/Orion_Foods_Systems__LLC_fb8a1961-3cf1-433a-8bdb-4ebfc3a3d82c.pdf",
        ),
        63704: (
            "QuickFuel",
            "56",
            "The NRHA primary fuel directory identifies Quick Fuel Cincinnati #2301 at 2502 Commerce Boulevard; the current locator and named OSM fuel canopy identify Sharonville at that exact street address.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        64492: (
            "QuickFuel",
            "54",
            "The NRHA primary directory identifies Quick Fuel Shadeland #2103 at 2651 North Shadeland Avenue, Indianapolis; the current official point omits North in its street label and agrees with the same numbered Fuelman record within six meters.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        64496: (
            "QuickFuel",
            "47",
            "The NRHA primary directory identifies Quick Fuel Romulus #1703 at 9880 Harrison Road; Fuelman omits Road and the named OSM fuel station confirms the same premises.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        64509: (
            "QuickFuel",
            "39",
            "The NRHA primary directory and Fuelman identify legacy #1002 at 1020 Boden Court; the current official locator uses 1010 West Boden Court. Fuelman and current official points are 61 meters apart on the same small fueling property, retaining the legacy identity without generalizing the address-number change.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        64662: (
            "QuickFuel",
            "43",
            "The NRHA primary directory explicitly identifies Quick Fuel West Allis #1011 at 435 South 116th Street with the postal city Milwaukee; the current official locator uses West Allis at the identical street address. Fuelman's coordinate is not used because it is fourteen kilometers away.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        65398: (
            "QuickFuel",
            "60",
            "The NRHA primary directory identifies Quick Fuel Brook Park #2306 at 15340 Diana Drive. The current official locator has that exact street address but its coordinate is central Cleveland, 13.4 kilometers away. The numbered Fuelman point coincides with the independently named OSM Quick Feul canopy on Diana Drive within five meters.",
            "https://nrha.com/media/pdf/partner/nfuelloc.pdf",
        ),
        68403: (
            "Rutter's",
            "16",
            "The official locator uses York (Loganville); the named OSM shop at 362 North Main Street explicitly uses York while its fuel canopy uses Loganville. CSV I-83 exit 10 and SR-214 identify this same property.",
            "https://www.rutters.com/stores/",
        ),
        70460: (
            "Allsup's",
            "4641",
            "The official professional-driver directory confirms #2371 at 8222 Highway 87 North, Grape Creek. The independently mapped Allsup's shop at that house and highway explicitly uses the postal city San Angelo, matching CSV US-87.",
            "https://yesway.com/professional-drivers/drivers-location/",
        ),
        71923: (
            "Sheetz",
            "740",
            "Store #740 is unique in Ohio, the official locator and independently named OSM store agree on 5010 Corrine Drive, South Bloomfield, ZIP43103. This property is at CSV US-23 and SR-752 in the Ashville postal area; the Comdata primary directory independently confirms the number and street.",
            "https://www.comdata.com/content/dam/comdatatrucking/GallonUpLocationsOnly%2004152024.pdf",
        ),
        72012: (
            "CEFCO",
            "108",
            "The official unique CEFCO #108 is at 101 West Moonlight Drive, Robinson in the Waco area, beside CSV US-77. The Comdata primary directory confirms number and street and independently named OSM CEFCO fuel premises corroborate the point.",
            "https://www.comdata.com/content/dam/comdatatrucking/GallonUpLocationsOnly%2004152024.pdf",
        ),
        72016: (
            "CEFCO",
            "11",
            "The official unique CEFCO #11 is at 4305 Bellmead Drive, Bellmead in the Waco area on CSV US-84. The McLennan County appraisal roll identifies CEFCO 11 at that exact street address; independently mapped CEFCO shop and diesel canopies agree.",
            "https://mclennancad.org/wp-content/uploads/2024/07/52-Bellmead-Certified-Roll-Report.pdf",
        ),
        72445: (
            "Sheetz",
            "639",
            "The official locator and named OSM store #639 agree on 1101 North Canfield Niles Road, Austintown, at CSV I-80 exit223 in the Youngstown area; the primary Comdata directory independently confirms #639 and street.",
            "https://www.comdata.com/content/dam/comdatatrucking/GallonUpLocationsOnly%2004152024.pdf",
        ),
        72447: (
            "Sheetz",
            "689",
            "The primary Comdata directory explicitly identifies Sheetz #689 at 550 South Airport Drive with postal city Henrico; the current official locator uses Sandston for the identical street. Independently mapped Sheetz premises corroborate the point.",
            "https://www.comdata.com/content/dam/comdatatrucking/GallonUpLocationsOnly%2004152024.pdf",
        ),
        72755: (
            "RaceTrac",
            "2620",
            "The official unique RaceTrac #2620 is at 7612 Pendleton Pike in Lawrence, within Indianapolis at CSV I-465 exit42. Store number, state and exit corridor agree; the municipal locality differs from the CSV metro name.",
            "https://www.racetrac.com/locations/indiana/lawrence/pendleton-pike",
        ),
        72761: (
            "RaceTrac",
            "2629",
            "The primary Comdata directory explicitly identifies RaceTrac #2629 at 76 Highway20, Tuscumbia. The current official locator uses Muscle Shoals at the same street address; fuel canopies are independently mapped at that location beside CSV Highway20/US72 corridor.",
            "https://www.comdata.com/content/dam/comdatatrucking/GallonUpLocationsOnly%2004152024.pdf",
        ),
        68864: (
            "QuickFuel",
            "83",
            "Ohio's Petroleum Underground Storage Tank Release Compensation Board identifies Quick Fuel #3190 at 5445 Webster Street, Dayton, in its September2024 meeting attachment; this is the exact street in the current official locator.",
            "https://www.petroboard.org/Pdfs/AttachmentsSept2024.pdf",
        ),
        68865: (
            "QuickFuel",
            "85",
            "Ohio's Petroleum Underground Storage Tank Release Compensation Board identifies Quick Fuel #3192 at 4945 Duff Road, Cincinnati, in its meeting attachment; the current locator uses 4945 Duff Drive, West Chester Township at the same premises and metropolitan city.",
            "https://petroboard.org/Pdfs/AttachmentsSept2022.pdf",
        ),
        71364: (
            "QuickFuel",
            "81",
            "Ohio's Petroleum Underground Storage Tank Release Compensation Board identifies Quick Fuel #3188 at 435 South VanDemark Road, Sidney, in its September2024 attachment; this exact street matches the current official locator.",
            "https://www.petroboard.org/Pdfs/AttachmentsSept2024.pdf",
        ),
    }
    for station_id, (brand, external_id, reason, url) in choices.items():
        record = next(
            row for row in records if row["brand"] == brand and row["external_id"] == external_id
        )
        csv_row = next(row for row in rows if int(row["OPIS Truckstop ID"]) == station_id)
        review = {
            "opis_id": station_id,
            "external_id": external_id,
            "brand": brand,
            "state": csv_row["State"],
            "csv_city": csv_row["City"].strip(),
            "official_address": record["address"],
            "evidence": [{"source_url": url, "reason": reason}],
        }
        cached = DATA / f"supplemental-osm-map-{station_id}.json"
        if cached.exists():
            osm = json.loads(cached.read_text(encoding="utf-8"))
            review["evidence"].append(
                {
                    "source_url": osm["source_url"],
                    "reason": "Public map extract retained for station-premises, street and highway review.",
                }
            )
        if station_id == 65398:
            identity = next(row for row in identities if row["store_number"] == "2306")
            review.update(
                latitude=identity["latitude"],
                longitude=identity["longitude"],
                source="reviewed_fuel_network_point",
                source_url=identity["source_url"],
            )
            review["rejected_candidates"] = [
                {
                    "source_url": record["source_url"],
                    "latitude": record["latitude"],
                    "longitude": record["longitude"],
                    "reason": "Official street identity is correct, but the point is central Cleveland, 13.4km from the independent named Diana Drive fuel premises.",
                }
            ]
        if station_id == 64509:
            review["evidence"].append(
                {
                    "source_url": "https://milwaukee.legistar.com/View.ashx?GUID=545E392A-FBC7-4C39-8879-31AC5646B601&ID=2963372&M=F",
                    "reason": "City of Milwaukee's licensing agenda independently identifies Jacobus Energy Inc., Quick Fuel, at 1010 West Boden Court, corroborating the current official address rather than the older directory's 1020 number.",
                }
            )
        reviews.append(review)
    write_json(DATA / "supplemental-manual-identities.json", reviews)
    # Chandler's canopy has no chain tag. The named service area and road operator
    # establish the site identity independently of the chain's own coordinate.
    reviewed = json.loads((DATA / "loves-ta-conflict-reviews.json").read_text(encoding="utf-8"))
    if not any(row["opis_id"] == 72104 for row in reviewed):
        conflict = next(
            row
            for row in json.loads((DATA / "conflicts.json").read_text(encoding="utf-8"))
            if row["opis_id"] == 72104
        )
        official = next(
            row for row in conflict["candidates"] if row["source"] == "loves_official_store"
        )
        reviewed.append(
            {
                **official,
                "source": "manually_reviewed_station_premises",
                "evidence": [
                    {
                        "source_url": official["source_url"],
                        "reason": "Official Love's #170 identifies Chandler, mile169 eastbound-only Turner Turnpike.",
                    },
                    {
                        "source_url": "https://www.openstreetmap.org/way/1018322181",
                        "reason": "Independent named Chandler Services area agrees within162m; its mapped fuel canopy https://www.openstreetmap.org/node/12138152100 agrees within88m.",
                    },
                    {
                        "source_url": "https://oklahoma.gov/ota/media/newsroom/2024/september/ota-board-awards-second-phase-of-stroud-service-plaza-project.html",
                        "reason": "Road operator confirms Chandler service plaza serves eastbound Turner Turnpike, while Stroud serves westbound. Together with official store number/address and mapped named plaza this identifies the fuel premises.",
                    },
                ],
                "rejected_candidates": [
                    {
                        "source_url": point["source_url"],
                        "source": point["source"],
                        "latitude": point["latitude"],
                        "longitude": point["longitude"],
                        "reason": f"The network point is {distance(official, point):.0f}m from the official store and independently named Chandler Services premises on the Turner Turnpike; it does not locate this plaza.",
                    }
                    for point in conflict["candidates"]
                    if point["source"] != "loves_official_store"
                ],
            }
        )
        write_json(DATA / "loves-ta-conflict-reviews.json", reviewed)
        write_json(DATA / "loves-ta-conflict-unresolved.json", [])
    print({"manual_supplemental_identities": len(reviews), "loves_ta_reviews": len(reviewed)})


if __name__ == "__main__":
    main()
