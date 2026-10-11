# Station data

**5,523 of 6,626 US station IDs have accepted coordinates (83.4%). 5,504 distinct sites are eligible for planning.**

Prices come only from the supplied CSV. Station coordinates are prepared in advance; trip requests do not search station directories.

## Coverage

| Measure | Count |
| --- | ---: |
| Input price observations | 8,151 |
| Non-US observations excluded | 620 |
| US observations | 7,531 |
| Repeated US observations | 905 |
| Unique US station IDs | 6,626 |
| Mapped station IDs | 5,523 |
| Unresolved station IDs | 1,103 |
| Mapped IDs excluded by status | 17 |
| Eligible station IDs | 5,506 |
| Extra IDs for duplicate physical sites | 2 |
| Distinct eligible sites | 5,504 |

Repeated observations use their arithmetic mean. Reviewed duplicate properties combine all their price observations. 5,506 of 5,506 eligible IDs have opening status recorded as unknown.

## Coordinate methods

Each mapped ID appears once, by its selected coordinate source. These counts do not separate direct downloads from extracts or scripted work from browser review.

| Selected source/method | IDs | How the point was obtained |
| --- | ---: | --- |
| Operator or station-network point | 2,830 | Extract published map coordinates; match store identity and locality. |
| Explicitly labeled operator-directory extract | 299 | Match records in downloaded operator-directory extracts. |
| Historical identity bridge to an operator point | 291 | Join old store identities to current points through the same street address. |
| Premises review and conflict resolution | 1,059 | Resolve identities and conflicting points with scripted checks and individual reviews. |
| Published point after property review | 129 | Retain a published point after checking station identity and property. |
| Published street matched to mapped premises | 465 | Match a published street and station identity to a mapped property. |
| Fleet-fuel locator point | 285 | Match Commercial Fueling Network (CFN) or Fuelman locator records. |
| Direct OpenStreetMap station match | 72 | Match distinctive station names or store numbers to OpenStreetMap fuel features. |
| Federal station GPS record | 62 | Match streets to US Alternative Fuels Data Center (AFDC) station GPS records. |
| CAT Scale host-station property | 25 | Match the host truck stop; use its on-property CAT Scale point. |
| Archived operator-network coordinate | 6 | Match original numbered stations to older operator-network records. |
| **Total** | **5,523** | |

Some operator points arrived through AllThePlaces extracts, which collect public store locators into GeoJSON. Extracts and their original feeds are not independent sources. Historical joins handle renamed or renumbered stations.

At least 74 records in the premises-review group carry evidence from automated conflict checks. Other records include browser listing, Street View or aerial review. There is no reliable total separating those activities. Google Maps website review was used; bulk Google Places API search was not.

## Status exclusions

| Recorded reason | IDs |
| --- | ---: |
| CAT Scale temporary-closure label | 7 |
| Maps temporary-closure label | 2 |
| Maps permanent-closure label | 6 |
| Property advertised as a former gas station | 1 |
| Conflicting operating and closure claims | 1 |
| **Total** | **17** |

These are exclusions, not all confirmed permanent closures. A CAT Scale label may refer to the scale rather than every fueling service. The conflicting case is Broadway/former Flying J in Pasco, WA. The API currently groups these under `known_closed_stations`. See [status reviews](coordinate-sources/status-exclusion-reviews.json) for station IDs and evidence.

## Unresolved stations

| Recorded reason | IDs |
| --- | ---: |
| No exact independent branch identity | 983 |
| No compatible mapped fuel feature within 100 metres | 35 |
| Generic brand without a street address | 32 |
| Candidate branch/property still needs confirmation | 17 |
| Recorded identity or equipment concern | 16 |
| Multiple nearby mapped fuel properties | 9 |
| Conflicting street addresses | 5 |
| City-only identity | 5 |
| Conflicting independent coordinates | 1 |
| **Total** | **1,103** |

Reasons are mutually exclusive. Unresolved means the applied checks did not establish a reliable station match; it does not mean closed or impossible to locate. Missing mapped fuel can reflect incomplete map coverage.

## Supporting searches

The original Census batch returned 75 exact address matches. These were search anchors, not accepted station premises. City centres and highway exits were also search anchors, not fuel-stop coordinates.

TomTom made 1,189 HTTP requests: 7 pilot and 1,182 bulk requests covering 1,109 IDs. It assisted 3 accepted matches; a separate audit accepted 3 more. All use independently sourced final points and are already counted above. TomTom response bodies were not retained.

## Files and limits

[verified-station-coordinates.csv](verified-station-coordinates.csv) is the only file in `data/` required for the application import. Git retains eight final data files: the mapping, two reports, accepted and unresolved station records, alias and status reviews, and one live validation record. Research intermediates, duplicate summaries and historical debugging records remain local and are ignored. Full research rebuilds require those local inputs.

[Detailed counts](station-coordinate-report.json) and [selected evidence](coordinate-sources/merged.json) preserve station IDs, sources and reviews.

Accepted points can locate a station building, canopy, scale or premises; they do not guarantee entrance access, current operation or fuel availability. Missing stations can remove a cheaper option or prevent a feasible plan. Optimization therefore covers the eligible subset, not every real station or road route.
