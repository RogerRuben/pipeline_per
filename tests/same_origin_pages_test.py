# -*- coding: utf-8 -*-
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SameOriginPageTests(unittest.TestCase):
    def test_cost_page_routes_stay_on_this_site(self):
        from cost_effectiveness_analysis.embed import classify, mount_config, read_static
        self.assertEqual(classify("GET", "/cost-effectiveness"), ("redirect", "/cost-effectiveness/"))
        self.assertEqual(classify("GET", "/cost-effectiveness/"), ("static", "index.html"))
        self.assertEqual(classify("GET", "/cost-effectiveness/app.js"), ("static", "app.js"))
        self.assertEqual(classify("GET", "/cost-effectiveness/api/schemes"), ("schemes",))
        self.assertEqual(classify("GET", "/cost-effectiveness/api/schemes/agreement:1"), ("scheme", "agreement:1"))
        self.assertEqual(classify("POST", "/cost-effectiveness/api/analyze"), ("analyze",))
        self.assertIsNone(classify("GET", "/api/schemes"))
        page = read_static("index.html").decode("utf-8")
        script = read_static("app.js").decode("utf-8")
        self.assertIn('href="styles.css"', page)
        self.assertIn('src="app.js"', page)
        self.assertIn('api("/api/analyze"', script)
        self.assertIn("/cost-effectiveness", script)
        config = mount_config(
            {"price_service_url": "http://127.0.0.1:18101",
             "effectiveness_service_url": "http://127.0.0.1:18102",
             "timeout_seconds": 15},
            {"unit": "yuan", "scale": 2, "signature": "ignored"},
            "data/protocol_demo.db",
        )
        self.assertEqual(config["price_output"], {"unit": "yuan", "scale": 2})
        self.assertEqual(config["recommendation_url"], "/portal")
        self.assertNotIn("17000", json.dumps(config))

    def test_price_page_shows_sensitivity_only_in_its_own_section(self):
        script = (ROOT / "app/static/deep_price/r6.js").read_text(encoding="utf-8")
        self.assertIn("['design','mechanism']", script)
        self.assertNotIn("['design','sensitivity','mechanism']", script)
        self.assertIn("本次情景改动的单字段影响 Top5", script)
        self.assertNotIn("<h3>单字段影响 Top5</h3>", script)


if __name__ == "__main__":
    unittest.main()
