import unittest
import base64

from app import app, inspect_links, load_json, normalize_detective, normalize_phone, parse_ai_json, validate_image


class ScamCheckTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_empty_message(self):
        response = self.client.post("/api/analyze", json={"message": ""})
        self.assertEqual(response.status_code, 400)

    def test_long_message(self):
        response = self.client.post("/api/analyze", json={"message": "a" * 5001})
        self.assertEqual(response.status_code, 400)

    def test_parser_accepts_fenced_json(self):
        self.assertEqual(parse_ai_json('```json\n{"risk":"safe"}\n```')["risk"], "safe")

    def test_parser_rejects_five_bad_formats(self):
        for value in ("", "không phải json", "[]", "{sai}", "```json\nnull\n```"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                parse_ai_json(value)

    def test_normalizer_always_returns_three_actions(self):
        for value in ({}, {"actions": None}, {"actions": []}, {"actions": "sai"}, {"risk": "khác"}):
            with self.subTest(value=value):
                result = normalize_detective(value)
                self.assertEqual(len(result["actions"]), 3)
                self.assertIn(result["risk"], {"safe", "suspicious", "danger"})

    def test_detects_five_spoof_patterns(self):
        for domain in ("vietcornbank.vn", "vietc0mbank.vn", "vietcom-bank.vn", "vietcombank-secure.vn", "vietcombank-verify.vn"):
            with self.subTest(domain=domain):
                result = inspect_links(f"Mở https://{domain}/x")
                self.assertIn("giả mạo", result[0]["warning"][0])

    def test_library_has_twelve_items_and_four_groups(self):
        items = load_json("scam_types.json")
        self.assertEqual(len(items), 12)
        self.assertEqual(len({item["category"] for item in items}), 4)

    def test_phone_formats_normalize_for_allowlist(self):
        self.assertEqual(normalize_phone("+84 912 345 678"), normalize_phone("0912.345.678"))

    def test_accepts_supported_screenshot(self):
        image = {"mimeType": "image/png", "data": base64.b64encode(b"png-data").decode()}
        self.assertEqual(validate_image(image), image)

    def test_rejects_wrong_image_type_and_bad_data(self):
        for image in (
            {"mimeType": "application/pdf", "data": "eA=="},
            {"mimeType": "image/png", "data": "not-base64"},
        ):
            with self.subTest(image=image), self.assertRaises(ValueError):
                validate_image(image)

    def test_rescue_rejects_unknown_choice(self):
        response = self.client.post("/api/rescue", json={"choice": "other"})
        self.assertEqual(response.status_code, 400)

    def test_no_action_skips_ai(self):
        response = self.client.post("/api/rescue", json={"choice": "none"})
        self.assertEqual(response.status_code, 200)
        self.assertIn("steps", response.get_json())


if __name__ == "__main__":
    unittest.main()
