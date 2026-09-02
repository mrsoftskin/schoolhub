"""Applying a sync finding must say what it actually did.

Reported as "the apply to calendar thing doesn't seem like it's working, and
if it is it's not easy to tell". It WAS working - it wrote
"FINC 313 Lecture" into fixed.csv correctly - but the item was kind=admin
dated three weeks out, and the Today plan renders only exam/project/quiz. So
nothing visibly changed, while the toast said "Calendar updated" regardless of
whether it wrote one row or none.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import brain.web.app as webapp
from brain.connectors import PulledItem
from conftest import make_core

CAL = ('\n[calendar]\nsemester_start = 2026-08-17\n'
       'semester_end = 2026-12-15\nfixed_csv = "calendar/fixed.csv"\n')


@pytest.fixture
def client(tmp_path, monkeypatch):
    core = make_core(tmp_path, [{"name": "FINC313", "assist_level": "full"}], CAL)
    (tmp_path / "calendar").mkdir(exist_ok=True)
    (tmp_path / "calendar" / "fixed.csv").write_text(
        "course,title,date,start_time,end_time,all_day,kind\n", encoding="utf-8")
    from brain.connectors import SessionStore

    SessionStore(core.config.settings.data_dir).save("oaks", {"d2lSessionVal": "x"})

    from brain.connectors.sites import OaksConnector

    item = PulledItem(course="FINC313", title="FINC 313 Lecture (details on OAKS)",
                      date="2026-09-18", start_time="11:00", end_time="11:30",
                      kind="admin", site="oaks")
    monkeypatch.setattr(OaksConnector, "pull",
                        lambda self, s, c, window_hi=None: [item])
    monkeypatch.setattr(webapp.Core, "load", staticmethod(lambda *a, **k: core))
    with TestClient(webapp.create_app(), headers={"host": "127.0.0.1"}) as c:
        yield c


def test_apply_reports_what_it_wrote(client):
    r = client.post("/api/sync/apply")
    assert r.status_code == 200
    body = r.json()
    assert body["applied"] == 1
    assert body["items"], "the response must name what it applied"
    it = body["items"][0]
    assert it["course"] == "FINC313"
    assert it["date"] == "2026-09-18"
    # The kind is what tells the UI the row will be invisible on Today.
    assert it["kind"] == "admin"


def test_applying_twice_reports_nothing_new(client):
    client.post("/api/sync/apply")
    body = client.post("/api/sync/apply").json()
    assert body["applied"] == 0, "the row already exists; nothing should be written"


def test_the_row_actually_lands_in_the_csv(client, tmp_path):
    client.post("/api/sync/apply")
    csv = (tmp_path / "calendar" / "fixed.csv").read_text(encoding="utf-8")
    assert "FINC 313 Lecture" in csv
    assert "2026-09-18" in csv
