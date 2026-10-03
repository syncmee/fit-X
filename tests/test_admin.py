"""Admin gates, user management, challenge admin, and the promote CLI."""
import unittest
from datetime import datetime

from app.extensions import db
from sqlalchemy import select

from tests.base import AppContextTestCase, admin_client, csrf_token, login, make_user


class AdminTestCase(AppContextTestCase):
    def test_promote_cli_grant_revoke_and_unknown(self):
        make_user(self.app, name="joe", email="joe@test.dev")
        runner = self.app.test_cli_runner()
        grant = runner.invoke(args=["promote-user", "joe@test.dev"])
        self.assertIn("Promoted", grant.output)
        from app.extensions import db
        from app.models import User
        from sqlalchemy import select

        self.assertTrue(db.session.scalar(select(User.is_admin).where(
            User.email == "joe@test.dev")))
        revoke = runner.invoke(args=["promote-user", "joe@test.dev", "--revoke"])
        self.assertIn("Revoked", revoke.output)
        missing = runner.invoke(args=["promote-user", "nobody@test.dev"])
        self.assertEqual(missing.exit_code, 1)

    def test_admin_routes_are_gated(self):
        make_user(self.app, name="joe", email="joe@test.dev")
        client = self.app.test_client()

        r = client.get("/admin/")
        self.assertEqual(r.status_code, 302)  # anonymous -> login
        login(client, self.app, "joe@test.dev")
        self.assertEqual(client.get("/admin/").status_code, 403)  # non-admin
        self.assertEqual(client.get("/admin/users").status_code, 403)
        self.assertEqual(client.get("/admin/coach").status_code, 403)
        self.assertEqual(client.get("/admin/announce").status_code, 403)

    def test_user_management_flow(self):
        client = admin_client(self.app)
        make_user(self.app, name="joe", email="joe@test.dev")
        from app.extensions import db
        from app.models import User
        from sqlalchemy import select

        joe_id = db.session.scalar(select(User.id).where(User.email == "joe@test.dev"))

        body = client.get("/admin/users").get_data(as_text=True)
        self.assertIn("joe@test.dev", body)
        self.assertIn("Export CSV", body)
        self.assertEqual(client.get(f"/admin/users/{joe_id}").status_code, 200)

        # CSRF-less POST is rejected
        self.assertEqual(client.post(f"/admin/users/{joe_id}/status",
                                     data={"action": "suspend"}).status_code, 400)

        client.post(f"/admin/users/{joe_id}/status",
                    data={"action": "suspend", "csrf_token": csrf_token(client)})
        db.session.expire_all()
        self.assertEqual(db.session.get(User, joe_id).status, "suspended")

        login(client, self.app, "joe@test.dev")
        r = client.post("/login", data={"email": "joe@test.dev", "password": "pw123456",
                                        "csrf_token": csrf_token(client, "/login")})
        self.assertEqual(r.status_code, 403)  # suspended users can't sign in

    def test_csv_export_excludes_health_data(self):
        client = admin_client(self.app)
        make_user(self.app, name="joe", email="joe@test.dev")
        r = client.get("/admin/users/export.csv")
        self.assertEqual(r.mimetype, "text/csv")
        body = r.get_data(as_text=True)
        self.assertIn("id,name,email,status", body)
        self.assertIn("joe@test.dev", body)
        self.assertNotIn("weight", body.lower())

    def test_challenge_admin_publish_and_audit(self):
        from app.challenges import seed_challenge_templates
        from app.extensions import db
        from app.models import AuditLog, Challenge
        from sqlalchemy import select

        client = admin_client(self.app)
        seed_challenge_templates()
        cid = db.session.scalar(select(Challenge.id).where(
            Challenge.title == "30-Day Streak"))

        self.assertIn("30-Day Streak", client.get("/admin/challenges").get_data(as_text=True))
        client.post(f"/admin/challenges/{cid}/status",
                    data={"action": "publish", "csrf_token": csrf_token(client, "/admin/challenges")})
        db.session.expire_all()
        self.assertEqual(db.session.get(Challenge, cid).status, "active")
        actions = db.session.scalars(select(AuditLog.action).where(
            AuditLog.action.like("challenge.%"))).all()
        self.assertIn("challenge.publish", actions)

    def test_announce_tool_audits_and_targets(self):
        from app import dashboard as dash_mod
        from app.models import AuditLog

        make_user(self.app, name="sleepy", email="sleepy@test.dev", last_active_at=None)
        client = admin_client(self.app)
        sent = []
        dash_mod._send_web_push = lambda user, **kw: (sent.append(f"push:{user.email}") or True)
        dash_mod._send_via_smtp = lambda *a, **k: (sent.append(f"email:{a[0].email}"), (True, "ok"))[1]

        r = client.post("/admin/announce", data={
            "title": "Hello", "body": "World", "audience": "all",
            "channels": ["push", "email"],
            "csrf_token": csrf_token(client, "/admin/announce"),
        })
        self.assertEqual(r.status_code, 302)
        # No push subscriptions exist in the test DB -> only email delivers.
        self.assertIn("email:admin@test.dev", sent)
        self.assertIn("email:sleepy@test.dev", sent)
        self.assertIsNotNone(db.session.scalar(select(AuditLog.action).where(
            AuditLog.action == "announce.send")))

    def test_inactive_segment_only_reaches_stale_users(self):
        from app import dashboard as dash_mod

        make_user(self.app, name="sleepy", email="sleepy@test.dev", last_active_at=None)
        make_user(self.app, name="fresh", email="fresh@test.dev",
                  last_active_at=datetime.utcnow())
        client = admin_client(self.app)
        emailed = []
        dash_mod._send_via_smtp = lambda user, *a, **k: (emailed.append(user.email), (True, "ok"))[1]

        client.post("/admin/announce", data={
            "title": "Come back", "body": "We miss you.", "audience": "inactive_7d",
            "channels": ["email"], "csrf_token": csrf_token(client, "/admin/announce"),
        })
        self.assertIn("sleepy@test.dev", emailed)
        self.assertNotIn("fresh@test.dev", emailed)


if __name__ == "__main__":
    unittest.main()
