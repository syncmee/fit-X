"""Phase A protections: per-email login failure counting and idempotency
status separation (missing key from a genuine duplicate submit)."""
import re
import unittest

from tests.base import AppContextTestCase, make_user

from app.extensions import db
from app.models import MealEntry, WaterLog
from sqlalchemy import select, func


def _csrf(client, path):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token"[^>]*value="([^"]+)"', html).group(1)


class LoginFailureCounterTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        from app import security

        security._login_failures.clear()
        make_user(self.app, name="joe", email="joe@test.dev")
        self.client = self.app.test_client()

    def _post_login(self, email, password):
        token = _csrf(self.client, "/login")
        return self.client.post("/login", data={
            "email": email, "password": password, "csrf_token": token,
            "action": "login",
        })

    def _failures(self):
        for _ in range(5):
            r = self._post_login("joe@test.dev", "wrong-password")
            self.assertEqual(r.status_code, 401)

    def test_correct_password_rejected_after_limit(self):
        self._failures()
        r = self._post_login("joe@test.dev", "pw123456")
        self.assertEqual(r.status_code, 429)
        self.assertIn("Too many failed sign-in attempts", r.get_data(as_text=True))

    def test_other_email_unaffected(self):
        from app.security import login_blocked_for_email

        self._failures()
        self.assertFalse(login_blocked_for_email("other@test.dev"))

    def test_success_clears_counter(self):
        from app.security import _login_failures

        self._failures()
        _login_failures["joe@test.dev"].clear()  # expire the window
        r = self._post_login("joe@test.dev", "pw123456")
        self.assertEqual(r.status_code, 302)
        self.assertNotIn("joe@test.dev", _login_failures)


class IdempotencyStatusTestCase(AppContextTestCase):
    def setUp(self):
        super().setUp()
        make_user(self.app, name="joe", email="joe@test.dev")
        self.client = self.app.test_client()
        self.login()

    def login(self):
        from tests.base import login

        login(self.client, self.app, "joe@test.dev")

    def _post_meal(self, key=None):
        token = _csrf(self.client, "/dashboard")
        data = {"name": "Dal rice", "calories": "430", "meal_type": "lunch",
                "csrf_token": token}
        if key is not None:
            data["idempotency_key"] = key
        return self.client.post("/meals/add", data=data)

    def _flash(self, response):
        html = response.get_data(as_text=True)
        match = re.search(r'<div[^>]*class="[^"]*flash[^"]*"[^>]*>([^<]+)', html)
        return match.group(1).strip() if match else ""

    def test_duplicate_key_rejected_once(self):
        self._post_meal("key-dup-1")
        r = self._post_meal("key-dup-1")
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            db.session.scalar(select(func.count(MealEntry.id))), 1)
        r2 = self.client.get("/dashboard")
        self.assertIn("already logged", r2.get_data(as_text=True))

    def test_missing_key_is_not_called_a_duplicate(self):
        r = self._post_meal(None)
        self.assertEqual(r.status_code, 302)
        # No meal persisted, and the message tells the user to refresh rather
        # than claiming the request was already processed.
        self.assertEqual(
            db.session.scalar(select(func.count(MealEntry.id))), 0)
        r2 = self.client.get("/dashboard")
        body = r2.get_data(as_text=True)
        self.assertIn("refresh the page", body)
        self.assertNotIn("already logged", body)

    def test_duplicate_water_key(self):
        token = _csrf(self.client, "/dashboard")
        data = {"amount_ml": "500", "csrf_token": token}
        self.client.post("/water/add", data={**data, "idempotency_key": "w1"})
        r = self.client.post("/water/add", data={**data, "idempotency_key": "w1"})
        self.assertEqual(r.status_code, 302)
        self.assertEqual(
            db.session.scalar(select(func.count(WaterLog.id))), 1)


if __name__ == "__main__":
    unittest.main()
