"""The job queue: fairness, priorities, chunked imports, crash recovery, real worker threads."""

import dataclasses
import threading
import time

import pytest

from app.jobs import JobError, JobQueue, Requeue, WorkerPool, drain, run_one
from tests.conftest import needs_engine


@pytest.fixture()
def q(tmp_path):
    return JobQueue(str(tmp_path / "q.db"))


def test_one_running_job_per_user_and_priority(q):
    a1 = q.enqueue("import", {}, user_id=1)
    a2 = q.enqueue("import", {}, user_id=1)
    b1 = q.enqueue("import", {}, user_id=2)
    assert q.claim("w1")["id"] == a1
    assert q.claim("w2")["id"] == b1  # user 1 already has a job running, so user 2 goes next
    assert q.claim("w3") is None
    q.finish(a1, {"new_games": 3})
    assert q.claim("w1")["id"] == a2
    p = q.enqueue("preview", {"key": "lichess:x"})
    q.enqueue("import", {}, user_id=3)
    assert q.claim("w4")["id"] == p  # previews jump the line


def test_requeue_goes_to_the_back_and_delay_is_respected(q):
    x = q.enqueue("import", {"n": 1}, user_id=1)
    y = q.enqueue("import", {}, user_id=2)
    assert q.claim("w")["id"] == x
    q.requeue(x, {"n": 2})
    assert q.claim("w")["id"] == y  # x went behind y
    q.finish(y)
    assert q.claim("w")["payload"] == {"n": 2}
    z = q.enqueue("import", {}, user_id=5)
    assert q.claim("w")["id"] == z
    q.requeue(z, delay=60)
    assert q.claim("w") is None  # not before its delay


def test_kinds_filter_for_the_fast_lane(q):
    q.enqueue("import", {}, user_id=1)
    assert q.claim("fast", kinds=("preview",)) is None
    p = q.enqueue("preview", {})
    assert q.claim("fast", kinds=("preview",))["id"] == p


def test_crashed_jobs_are_recovered(q):
    j = q.enqueue("import", {}, user_id=1)
    q.claim("w")
    assert q.recover() == 0  # heartbeat is fresh
    with q._conn() as c:
        c.execute("UPDATE jobs SET heartbeat = ? WHERE id = ?", (time.time() - 3600, j))
    assert q.recover() == 1
    assert q.get(j)["state"] == "queued" and q.get(j)["stage"].startswith("Resuming")


def test_concurrent_claims_never_duplicate(q):
    ids = {q.enqueue("preview", {"i": i}) for i in range(60)}
    got, lock = [], threading.Lock()

    def worker(n):
        while (j := q.claim(f"w{n}")) is not None:
            with lock:
                got.append(j["id"])
    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(got) == sorted(ids)


def test_run_one_outcomes(q):
    def ok(job, ctx):
        ctx.progress(stage="Working", done=1, total=2)
        ctx.partial(first_results=True)
        return {"answer": 42}

    def bad(job, ctx):
        raise JobError("No Lichess account called 'ghost'.")

    def boom(job, ctx):
        raise ZeroDivisionError

    def again(job, ctx):
        raise Requeue({"step": 2}, stage="Next chunk")

    handlers = {"ok": ok, "bad": bad, "boom": boom, "again": again}
    for kind in handlers:
        jid = q.enqueue(kind, {})
        run_one(q, q.claim("w", (kind,)), handlers, {})
        j = q.get(jid)
        if kind == "ok":
            assert j["state"] == "done" and j["result"] == {"first_results": True, "answer": 42}
        elif kind == "bad":
            assert j["state"] == "error" and "ghost" in j["error"]
        elif kind == "boom":
            assert j["state"] == "error" and "our side" in j["error"]  # no stack traces shown to users
        else:
            assert j["state"] == "queued" and j["payload"] == {"step": 2} and j["stage"] == "Next chunk"
    # The browser view hides the payload and never shows other users' jobs
    jid = q.enqueue("ok", {"secret": "pgn"}, user_id=7)
    assert q.view(jid, 8) is None and "payload" not in q.view(jid, 7)


def test_drain_runs_chunks_to_completion(q):
    def chunked(job, ctx):
        left = job["payload"].get("left", 3)
        if left > 1:
            raise Requeue({"left": left - 1})
        return {"chunks": 3}
    jid = q.enqueue("c", {})
    drain(q, {"c": chunked}, {}, jid)
    assert q.get(jid)["state"] == "done" and q.get(jid)["result"] == {"chunks": 3}


def test_worker_pool_threads_process_jobs(q):
    seen = []

    def handler(job, ctx):
        seen.append(job["payload"]["n"])
        return {"ok": True}
    pool = WorkerPool(q, {"import": handler, "preview": handler}, lambda: {}, workers=2, poll_s=0.05)
    pool.start()
    try:
        ids = [q.enqueue("import", {"n": i}, user_id=i) for i in range(5)] + [q.enqueue("preview", {"n": 99})]
        deadline = time.time() + 10
        while time.time() < deadline and any(q.get(i)["state"] != "done" for i in ids):
            time.sleep(0.05)
        assert all(q.get(i)["state"] == "done" for i in ids) and sorted(seen) == [0, 1, 2, 3, 4, 99]
        assert pool.alive() == 3  # two general workers + the fast lane
    finally:
        pool.stop()
    s = q.stats()
    assert s["queued"] == 0 and s["finished_24h"] == 6


# ---- chunked imports through the real app -------------------------------------------------------
@needs_engine
def test_big_import_runs_in_chunks_with_early_results(monkeypatch):
    import app.main as m
    from tests.test_api import client

    monkeypatch.setattr(m.importer, "settings", dataclasses.replace(m.settings, import_chunk=1))
    c = client(m, "chunks@test.com")
    a = c.post("/api/accounts", json={"platform": "demo"}).json()
    seen_stages = []
    real_requeue = m.queue.requeue

    def spy(job_id, payload=None, delay=0.0, stage=None):
        seen_stages.append(stage)
        return real_requeue(job_id, payload, delay, stage)
    monkeypatch.setattr(m.queue, "requeue", spy)
    job = c.post(f"/api/accounts/{a['id']}/pgn", json={"pgn": "demo"}).json()
    j = c.get(f"/api/jobs/{job['job_id']}").json()
    assert j["state"] == "done" and j["new_games"] == 3 and j["first_results"] is True
    assert seen_stages == ["Analysed 1 of 3; continuing shortly", "Analysed 2 of 3; continuing shortly"]
    assert c.get(f"/api/accounts/{a['id']}/profile").json()["dna"]["games"] == 3
    assert c.get("/api/me").json()["accounts"][0]["running_job"] is None
