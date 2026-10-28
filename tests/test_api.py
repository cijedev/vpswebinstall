"""API smoke tests for cije-vpsinstallerweb. No live apt/systemctl."""

from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from cije_vpsinstallerweb.app import create_app
from cije_vpsinstallerweb.settings import Settings

VALID = {
    "domain": "example.com",
    "hostname": "server.example.com",
    "admin_email": "admin@example.com",
    "admin_password": "AdminPass123456",
    "timezone": "UTC",
    "control_panel": "virtualmin",
    "web_bundle": "LAMP",
    "webmail": "roundcube",
    "mailbaby": {
        "host": "relay.mailbaby.net",
        "port": 25,
        "username": "mb_user",
        "password": "mb_pass",
    },
    "vnc": {"enabled": True, "password": "vncpass1", "display": 1},
    "ssl": {"enabled": True, "staging": True},
}


class WebAppTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            token="CHANGE_ME",
            host="127.0.0.1",
            port=18765,
            data_dir=root / "data",
            install_root=root / "fake-root",
            dry_run=True,
            allow_live=False,
        )
        self.settings.ensure_dirs()
        self.app = create_app(self.settings)
        self.client = TestClient(self.app)
        self.auth = {"X-API-Token": self.settings.token}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def wait_job(self, job_id: str, timeout: float = 30.0) -> dict:
        deadline = time.time() + timeout
        last = {}
        while time.time() < deadline:
            res = self.client.get(f"/api/jobs/{job_id}", headers=self.auth)
            self.assertEqual(res.status_code, 200)
            last = res.json()
            if last["status"] not in {"queued", "running"}:
                return last
            time.sleep(0.1)
        self.fail(f"job {job_id} did not finish: {last}")

    def test_health_public(self):
        res = self.client.get("/api/health")
        self.assertEqual(res.status_code, 200)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["app"], "cije-vpsinstallerweb")
        self.assertTrue(body["dry_run"])
        self.assertFalse(body["live_mode"])

    def test_api_requires_token(self):
        res = self.client.get("/api/config")
        self.assertEqual(res.status_code, 401)

    def test_pages_redirect_without_token(self):
        res = self.client.get("/", follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/login", res.headers["location"])

    def test_login_and_dashboard(self):
        bad = self.client.post("/login", data={"token": "nope", "next": "/"})
        self.assertEqual(bad.status_code, 401)
        res = self.client.post("/login", data={"token": self.settings.token, "next": "/"}, follow_redirects=False)
        self.assertEqual(res.status_code, 303)
        dash = self.client.get("/")
        self.assertEqual(dash.status_code, 200)
        self.assertIn("Setup wizard", dash.text)
        self.assertIn("cije-vpsinstallerweb", dash.text)

    def test_wizard_and_related_pages(self):
        pages = ("/wizard", "/status", "/dns", "/troubleshoot", "/backup", "/logs")
        for path in pages:
            res = self.client.get(path, headers=self.auth)
            self.assertEqual(res.status_code, 200, path)

    def test_config_validation_rejects_bad_domain(self):
        res = self.client.put(
            "/api/config",
            headers=self.auth,
            json={"domain": "not a domain", "admin_email": "x"},
        )
        self.assertEqual(res.status_code, 400)
        problems = res.json()["problems"]
        self.assertTrue(any("domain" in p for p in problems))

    def test_config_save_and_load(self):
        res = self.client.put("/api/config", headers=self.auth, json=VALID)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["config"]["domain"], "example.com")
        self.assertEqual(body["config"]["webmail"], "roundcube")
        loaded = self.client.get("/api/config", headers=self.auth)
        self.assertEqual(loaded.status_code, 200)
        self.assertEqual(loaded.json()["config"]["hostname"], "server.example.com")
        self.assertTrue(self.settings.config_path.exists())

    def test_config_webmail_choices(self):
        for webmail in ("roundcube", "snappymail", "cypht", "usermin"):
            payload = dict(VALID)
            payload["webmail"] = webmail
            res = self.client.put("/api/config", headers=self.auth, json=payload)
            self.assertEqual(res.status_code, 200, webmail)
            self.assertEqual(res.json()["config"]["webmail"], webmail)

    def test_meta_and_dns(self):
        self.client.put("/api/config", headers=self.auth, json=VALID)
        meta = self.client.get("/api/meta", headers=self.auth).json()
        self.assertEqual(meta["app"], "cije-vpsinstallerweb")
        self.assertIn("mail", meta["steps"])
        self.assertIn("snappymail", meta["webmail"])
        dns = self.client.get("/api/dns", headers=self.auth).json()
        self.assertTrue(dns["text"])
        types = {row["type"] for row in dns["records"]}
        self.assertIn("MX", types)
        self.assertIn("TXT", types)

    def test_dry_run_install_streams_logs(self):
        self.client.put("/api/config", headers=self.auth, json=VALID)
        res = self.client.post("/api/install", headers=self.auth, json={"no_backup": True})
        self.assertEqual(res.status_code, 200, res.text)
        job_id = res.json()["job"]["id"]
        done = self.wait_job(job_id)
        self.assertIn(done["status"], {"ok", "error"})
        self.assertTrue(any("dry-run" in line.lower() or "started" in line.lower() for line in done["lines"]))
        status = self.client.get("/api/status", headers=self.auth).json()
        self.assertTrue(status["started_at"])

    def test_troubleshoot_and_backup_helpers(self):
        self.client.put("/api/config", headers=self.auth, json=VALID)
        ts = self.client.post("/api/troubleshoot", headers=self.auth, json={"components": ["mail"]})
        self.assertEqual(ts.status_code, 200, ts.text)
        done = self.wait_job(ts.json()["job"]["id"])
        self.assertIn(done["status"], {"ok", "error"})
        self.assertIn("reports", done["result"])

        backup = self.client.post("/api/backup", headers=self.auth, json={})
        self.assertEqual(backup.status_code, 200, backup.text)
        bdone = self.wait_job(backup.json()["job"]["id"])
        self.assertEqual(bdone["status"], "ok")
        listed = self.client.get("/api/backup", headers=self.auth).json()
        self.assertIn("snapshots", listed)
        self.assertTrue(listed["snapshots"])
        name = listed["snapshots"][0]["name"]
        restore = self.client.post(f"/api/backup/{name}/restore", headers=self.auth, json={})
        self.assertEqual(restore.status_code, 200)
        rdone = self.wait_job(restore.json()["job"]["id"])
        self.assertEqual(rdone["status"], "ok")

    def test_second_job_conflict(self):
        self.client.put("/api/config", headers=self.auth, json=VALID)
        first = self.client.post("/api/install", headers=self.auth, json={"no_backup": True})
        self.assertEqual(first.status_code, 200)
        second = self.client.post("/api/test", headers=self.auth, json={})
        # Either 409 if still running, or 200 if the dry-run already finished.
        self.assertIn(second.status_code, (200, 409))
        self.wait_job(first.json()["job"]["id"])


if __name__ == "__main__":
    unittest.main()
