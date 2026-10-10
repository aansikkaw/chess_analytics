"""Load test: how the site behaves with a few hundred people using it at once.

Run it against STAGING (never production: it creates accounts and queues imports).

  pip install locust
  locust -f deploy/loadtest/locustfile.py --host https://staging.YOUR_DOMAIN \
         --users 200 --spawn-rate 10 --run-time 10m --headless --csv loadtest

On the staging server first set SIGNUPS_PER_HOUR=100000 (every simulated user comes from your one IP address)
and keep COACH_PROVIDER=offline unless you mean to spend AI tokens.

What to watch:
  * p95 response time under 500 ms for page and API reads (locust's report, and the JSON access log)
  * error rate under 1% (429s from the per-IP limits are expected for the login and preview tasks)
  * the status page: import queue wait, and whether any check turns red

Most simulated users browse their analysis and solve puzzles; a few sync, ask the coach or run a preview.
That's roughly the mix real users produce.
"""

from __future__ import annotations

import random
import uuid

from locust import HttpUser, between, task


class Player(HttpUser):
    wait_time = between(2, 8)

    def on_start(self) -> None:
        self.email = f"load-{uuid.uuid4().hex[:10]}@example.com"
        r = self.client.post("/api/auth/signup", json={"email": self.email, "password": "load-test-pass-1", "accept_terms": True},
                             headers={"Origin": self.host}, name="/api/auth/signup")
        self.ok = r.status_code == 200
        self.acct = None
        if not self.ok:
            return
        a = self.client.post("/api/accounts", json={"platform": "demo"}, headers={"Origin": self.host})
        if a.status_code == 200:
            self.acct = a.json()["id"]
            self.client.post(f"/api/accounts/{self.acct}/pgn", json={"pgn": "demo"}, headers={"Origin": self.host},
                             name="/api/accounts/[id]/pgn")

    def _get(self, path: str, name: str | None = None):
        # 404 just means this user's demo games are still being analysed: not an error.
        with self.client.get(path, name=name or path, catch_response=True) as r:
            if r.status_code == 404:
                r.success()
            return r

    @task(10)
    def dashboard(self) -> None:
        self._get("/api/me")
        if self.acct:
            self._get(f"/api/accounts/{self.acct}/profile", "/api/accounts/[id]/profile")

    @task(6)
    def puzzles(self) -> None:
        if not self.acct:
            return
        r = self._get(f"/api/accounts/{self.acct}/puzzles?due=false&limit=10", "/api/accounts/[id]/puzzles")
        rows = r.json() if r.status_code == 200 else []
        if rows and isinstance(rows, list):
            p = random.choice(rows)
            self.client.post(f"/api/puzzles/{p['id']}/hint", json={"level": 1}, headers={"Origin": self.host}, name="/api/puzzles/[id]/hint")

    @task(3)
    def plan(self) -> None:
        if self.acct:
            self._get(f"/api/accounts/{self.acct}/plan", "/api/accounts/[id]/plan")

    @task(2)
    def coach(self) -> None:
        if self.acct:
            with self.client.post(f"/api/accounts/{self.acct}/coach", json={"message": "What should I work on this week?"},
                                  headers={"Origin": self.host}, name="/api/accounts/[id]/coach", catch_response=True) as r:
                if r.status_code in (404, 429):  # still importing, or the daily question limit: expected
                    r.success()

    @task(2)
    def landing(self) -> None:
        self._get("/")
        self._get("/api/config")

    @task(1)
    def status(self) -> None:
        self._get("/api/status?external=false", "/api/status")
