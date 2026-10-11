"""Regression checks for physical NRHA table-column extraction."""

import importlib
import unittest

nrha = importlib.import_module("scripts.coordinates.regional-nrha")


def word(text, x, y=272.5):
    return {"text": text, "x0": x, "top": y}


class MerchantTableTests(unittest.TestCase):
    def setUp(self):
        # Verified against the original Tarkio row on PDF page 82.
        self.words = [
            word("Petrocard ", 52.356),
            word("24 ", 91.962),
            word("#911021", 104.504),
            word("Highway ", 186),
            word("136", 221.715),
            word("Tarkio", 360.72),
            word("Atchison", 456.06),
            word("MO", 541.656),
            word("64491", 565.344),
            word("Yes", 616.836),
            word("Yes", 635.652),
            word("No", 654.468),
        ]

    def test_tarkio_store_identity_is_not_lost_to_layout_letter_spacing(self):
        (row,) = nrha.character_rows(self.words, 82)
        self.assertEqual(row["name"], "Petrocard 24 #911021")
        self.assertEqual(row["address"], "Highway 136")
        self.assertEqual((row["city"], row["state"]), ("Tarkio", "MO"))
        self.assertEqual(row["page"], 82)
        self.assertEqual(row["diesel"], "Yes")
        self.assertEqual(row["truck_access"], "No")

    def test_missing_county_does_not_shift_city_or_state(self):
        rows = list(nrha.character_rows([w for w in self.words if w["text"] != "Atchison"], 82))
        self.assertEqual(rows[0]["county"], "")
        self.assertEqual(rows[0]["state"], "MO")
        self.assertEqual(rows[0]["postal_code"], "64491")

    def test_multiword_city_remains_in_its_physical_cell(self):
        words = [w for w in self.words if w["text"] != "Tarkio"]
        words += [word("Saint ", 360.72), word("Paul", 387)]
        self.assertEqual(next(nrha.character_rows(words, 82))["city"], "Saint Paul")

    def test_table_header_is_not_a_merchant(self):
        words = [
            word("Store Name", 52),
            word("Address", 186),
            word("City", 360),
            word("County", 456),
            word("StateZip", 541),
        ]
        self.assertEqual(list(nrha.character_rows(words, 1)), [])

    def test_invalid_zip_is_retained_without_inventing_a_postal_code(self):
        words = [w if w["text"] != "64491" else word("not-a-zip", 565.344) for w in self.words]
        row = next(nrha.character_rows(words, 82))
        self.assertEqual(row["postal_code"], "")
        self.assertEqual(row["postal_code_raw"], "not-a-zip")

    def test_malformed_zip_suffix_keeps_only_the_published_base_zip(self):
        words = [w if w["text"] != "64491" else word("64491-160", 565.344) for w in self.words]
        row = next(nrha.character_rows(words, 82))
        self.assertEqual(row["postal_code"], "64491")
        self.assertEqual(row["postal_code_raw"], "64491-160")

    def test_adjacent_pdf_rows_are_not_combined(self):
        words = self.words + [{**w, "top": w["top"] + 10} for w in self.words]
        self.assertEqual(len(list(nrha.character_rows(words, 82))), 2)

    def test_touching_name_and_street_are_separated_by_character_positions(self):
        # PDF page 110 word extraction merges '#49' and house number '3033'.
        characters = [
            word("Nice-N-Easy Grocery Shoppe #", 52.356),
            word("4", 173.868),
            word("9", 178.781),
            word("3", 186),
            word("0", 190.913),
            word("3", 195.825),
            word("3", 200.738),
            word(" State Rt 26", 205.65),
            word("Constableville", 360.72),
            word("Lewis", 456.06),
            word("NY", 541.656),
            word("13325", 565.344),
        ]
        row = next(nrha.character_rows(characters, 110))
        self.assertEqual(row["name"], "Nice-N-Easy Grocery Shoppe #49")
        self.assertEqual(row["address"], "3033 State Rt 26")

    def test_touching_street_and_city_do_not_drop_the_merchant(self):
        # PDF page 19 word extraction merges '301/Cr3' and 'Sparr'.
        characters = [
            word("Kangaroo Express #2321", 52.356),
            word("13002 Ne Highway 200A Ne Cor Old 301/Cr", 186),
            word("3", 356.71),
            word("Sparr", 360.72),
            word("Marion", 456.06),
            word("FL", 541.656),
            word("32113", 565.344),
        ]
        row = next(nrha.character_rows(characters, 19))
        self.assertEqual(row["address"], "13002 Ne Highway 200A Ne Cor Old 301/Cr3")
        self.assertEqual(row["city"], "Sparr")


if __name__ == "__main__":
    unittest.main()
