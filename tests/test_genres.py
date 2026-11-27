import unittest

from catalog_mirror.genres import apply_overrides, classify, genre_fields, split_genres


class GenreTests(unittest.TestCase):
    def test_order_and_duplicates(self):
        raw = "  Doom Metal / Black Metal; Doom Metal  "
        result = genre_fields(raw)
        self.assertEqual(result["genres_raw"], raw)
        self.assertEqual(result["genres_json"], ["Doom Metal", "Black Metal", "Doom Metal"])
        self.assertEqual(result["primary_genre"], "Doom Metal")
        self.assertEqual(result["genre_3"], "Doom Metal")

    def test_parenthetical_qualifiers_not_split(self):
        self.assertEqual(split_genres("Black Metal (early/middle), Heavy Metal"), ["Black Metal (early/middle)", "Heavy Metal"])

    def test_missing(self):
        fields = genre_fields(None)
        self.assertEqual(fields["genres_json"], [])
        self.assertEqual(fields["primary_genre"], "")
        self.assertEqual(fields["genre_count"], 0)

    def test_more_than_eight(self):
        fields = genre_fields("/".join(f"Genre {n}" for n in range(12)))
        self.assertEqual(fields["genre_count"], 12)
        self.assertEqual(fields["genre_8"], "Genre 7")
        self.assertEqual(fields["genres_json"][-1], "Genre 11")

    def test_classification_overlay(self):
        fields = genre_fields("Doom Metal / Death Metal")
        result = classify(fields, {"Doom Metal": {"ecommerce_classification": "Doom", "confidence": 0.95, "review_status": "approved"}})
        self.assertEqual(result["ecommerce_primary_class"], "Doom")
        self.assertEqual(result["ecommerce_secondary_tags"], ["Death Metal"])
        self.assertEqual(fields["genres_raw"], "Doom Metal / Death Metal")

    def test_protected_override(self):
        result = apply_overrides(genre_fields("Doom"), [{"field": "genres_raw", "override_value": "Black"}])
        self.assertEqual(result["genres_raw"], "Doom")


if __name__ == "__main__":
    unittest.main()
