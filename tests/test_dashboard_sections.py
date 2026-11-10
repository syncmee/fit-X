"""The coach chat's refreshDashboardData() swaps dashboard sections by
stable id. This test keeps the template and the JS swap list in sync:
every id in the JS list must exist in the rendered dashboard, and the
list must never grow to include the live chat containers.
"""
import re
import json
import unittest
from pathlib import Path

from tests.base import AppContextTestCase, login, make_user

REFRESH_LIST_RE = re.compile(r"const REFRESH_SECTION_IDS = \[(.*?)\]", re.S)
ID_RE = re.compile(r"'([a-z0-9-]+)'")
NEVER_SWAP = {"chatBody", "coachForm"}


class DashboardSectionIdsTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        make_user(self.app, name="joe", email="joe@test.dev")
        self.client = self.app.test_client()
        login(self.client, self.app, "joe@test.dev")
        from app.admin import seed_starter_foods
        seed_starter_foods()

    def test_rendered_dashboard_contains_refresh_section_ids(self):
        template = (Path(__file__).resolve().parent.parent
                    / "templates" / "dashboard.html").read_text(encoding="utf-8")
        match = REFRESH_LIST_RE.search(template)
        self.assertIsNotNone(match, "REFRESH_SECTION_IDS missing from the template")
        js_ids = ID_RE.findall(match.group(1))
        self.assertTrue(js_ids, "refresh swap list is empty")
        for bad in NEVER_SWAP & set(js_ids):
            self.fail(f"{bad} must never be swapped (it holds the live chat)")

        html = self.client.get("/dashboard").get_data(as_text=True)
        for section_id in js_ids:
            self.assertIn(
                f'id="{section_id}"', html,
                f"refreshDashboardData swaps #{section_id} but the dashboard "
                "does not render it — every swap target needs a stable id")

        # Drift guard: the swap list is deliberately 9 sections today. If this
        # trips, a section was added or removed — verify the new section has
        # no load-time JS (charts/listeners) before updating the count.
        self.assertEqual(len(js_ids), 9)

    def test_chart_data_is_present_and_valid_json(self):
        html = self.client.get("/dashboard").get_data(as_text=True)
        self.assertIn('id="chart-data"', html)
        self.assertIn('id="macro-targets-card"', html)
        match = re.search(
            r'<script type="application/json" id="chart-data">(.*?)</script>',
            html,
            re.S,
        )
        self.assertIsNotNone(match, "chart-data JSON block missing")
        chart_data = json.loads(match.group(1))
        self.assertIn("today_totals", chart_data)


if __name__ == "__main__":
    unittest.main()
