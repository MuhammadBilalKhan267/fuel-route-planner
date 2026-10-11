"""Credential removal preserves public station identities and review references."""

import unittest

from scripts.coordinates.sanitize_sources import sanitized_record, sanitized_text, sanitized_url


class SourceSanitationTests(unittest.TestCase):
    def test_query_credentials_removed_but_station_query_and_fragment_retained(self):
        result = sanitized_url(
            "https://example.com/stations?store=123&API_KEY=embedded&state=NY&token=value#fuel"
        )
        self.assertEqual(result, "https://example.com/stations?store=123&state=NY#fuel")

    def test_unaffected_url_preserves_exact_encoding_and_query_order(self):
        value = "https://example.com/search?bounds=18%2C-180&query=North%20Main&rows=50"
        self.assertEqual(sanitized_url(value), value)

    def test_client_authentication_parameters_do_not_remove_station_identifiers(self):
        result = sanitized_url(
            "https://example.com/stations?clientId=embedded&external_id=123&"
            "CLIENT_SECRET=embedded&store=0045&client-id=embedded#fuel"
        )
        self.assertEqual(result, "https://example.com/stations?external_id=123&store=0045#fuel")

    def test_nested_source_and_rejection_urls_remain_identical(self):
        value = "https://example.com/stations?key=embedded&store=123"
        record = {
            "source_url": value,
            "latitude": 40.123,
            "longitude": -75.456,
            "external_id": "00123",
            "address": "123 Main Street",
            "rejected_candidates": [{"source_url": value}],
        }
        actual = sanitized_record(record)
        self.assertEqual(actual["source_url"], actual["rejected_candidates"][0]["source_url"])
        for key in ["latitude", "longitude", "external_id", "address"]:
            self.assertEqual(actual[key], record[key])
        self.assertEqual(record["source_url"], value)

    def test_literal_header_and_javascript_client_keys_are_redacted(self):
        for value in [
            "headers = {'x-api-key': 'public_client_key_123'}",
            'mapbox.accessToken = "public_client_key_123";',
            'const apiKey = "public_client_key_123";',
        ]:
            with self.subTest(value=value):
                self.assertNotIn("public_client_key_123", sanitized_text(value))
                self.assertIn("REDACTED", sanitized_text(value))

    def test_ordinary_station_text_and_short_category_keys_are_unchanged(self):
        value = 'Station 123: key="Diesel"; address="123 Token Road"; latitude=40.12'
        self.assertEqual(sanitized_text(value), value)


if __name__ == "__main__":
    unittest.main()
