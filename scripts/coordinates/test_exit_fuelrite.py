import unittest

from scripts.coordinates.exit_fuelrite import directory_identities

URL = "https://fuelrite.com/fuelrite-nj-locations"
PAGE = """2 locations in New Jersey shown below.
* ### Mahwah — BOLLA MARKET - VALERO (NORTH)
193 NJ-17 North, Mahwah, NJ 07430
I-87 Exit 15; I-287 Exit 66. Northbound property.
(201) 642-4111
* ### Mahwah — BOLLA MARKET - VALERO (SOUTH)
198 NJ-17 South, Mahwah, NJ 07430
I-87 Exit 15; I-287 Exit 66. Southbound property.
(201) 252-8967
Loading map...
"""


class DirectoryIdentityTests(unittest.TestCase):
    def test_opposite_direction_properties_remain_separate_and_unverified(self):
        rows = directory_identities(PAGE, "NJ", URL)
        self.assertEqual(len(rows), 2)
        self.assertNotEqual(rows[0]["street_address"], rows[1]["street_address"])
        for row in rows:
            self.assertFalse(row["identity_verified"])
            self.assertFalse(row["coordinate_verified"])
            self.assertNotIn("latitude", row)
            self.assertNotIn("longitude", row)
            self.assertEqual(row["explicit_route_exits"][1], {"route": "I-287", "exit_ref": "66"})

    def test_partial_rendered_directory_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Incomplete"):
            directory_identities(PAGE.replace("2 locations", "3 locations"), "NJ", URL)

    def test_wrong_state_and_non_https_source_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "outside expected state"):
            directory_identities(PAGE, "NY", URL)
        with self.assertRaisesRegex(ValueError, "HTTPS"):
            directory_identities(PAGE, "NJ", "http://fuelrite.com/")

    def test_rendered_line_labels_and_commas_in_numeric_street(self):
        page = """L38: 1 locations in New Jersey shown below.
L39:
L40: * ### Elizabeth — BOLLA MARKET - SUNOCO
L41:
L42: 828 US-1 North, #9, Elizabeth, NJ 07202
L43:
L44: I-95 Exit 13. Route 439 north.
L49: (908) 282-0222
L50: Loading map...
"""
        row = directory_identities(page, "NJ", URL)[0]
        self.assertEqual(row["street_address"], "828 US-1 North, #9")
        self.assertEqual(row["explicit_route_exits"], [{"route": "I-95", "exit_ref": "13"}])

    def test_conflicting_exit_suffixes_are_preserved_without_fallback(self):
        page = PAGE.replace("I-87 Exit 15; I-287 Exit 66.", "I-80 Exit 4B; I-80 Exit 4C.")
        row = directory_identities(page, "NJ", URL)[0]
        self.assertEqual(
            row["explicit_route_exits"],
            [{"route": "I-80", "exit_ref": "4B"}, {"route": "I-80", "exit_ref": "4C"}],
        )


if __name__ == "__main__":
    unittest.main()
