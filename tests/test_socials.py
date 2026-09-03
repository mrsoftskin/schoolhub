"""Happy hours and campus socials.

The rule this file exists to enforce: never tell someone a happy hour is on
today unless the DAYS were actually confirmed. Research established that no
aggregator publishes days - the big one had zero of 89 venues stating them -
so an unconfirmed entry is the normal case, not an edge case.
"""

from __future__ import annotations

from datetime import date, time

from brain import socials


# ---- day parsing --------------------------------------------------------

def test_day_ranges_and_lists():
    assert socials.parse_days("Tue-Fri") == [1, 2, 3, 4]
    assert socials.parse_days("Mon, Wed, Fri") == [0, 2, 4]
    assert socials.parse_days("Monday-Thursday") == [0, 1, 2, 3]
    assert socials.parse_days("daily") == [0, 1, 2, 3, 4, 5, 6]
    assert socials.parse_days("Sat & Sun") == [5, 6]


def test_a_range_can_wrap_the_weekend():
    """"Fri-Sun" and "Sun-Thu" are both things bars actually do."""
    assert socials.parse_days("Fri-Sun") == [4, 5, 6]
    assert socials.parse_days("Sun-Thu") == [0, 1, 2, 3, 6]


def test_unknown_days_parse_to_nothing_not_to_everything():
    """The dangerous failure would be defaulting to Mon-Fri."""
    for spec in ("", "unknown", "varies", "?", "whenever"):
        assert socials.parse_days(spec) == [], spec


def test_days_round_trip_to_a_readable_label():
    assert socials.format_days([1, 2, 3, 4]) == "Tue-Fri"
    assert socials.format_days([0, 2, 4]) == "Mon, Wed, Fri"
    assert socials.format_days(list(range(7))) == "Daily"
    assert socials.format_days([]) == "days not confirmed"


# ---- the confirmed/unconfirmed distinction ------------------------------

def test_unconfirmed_venue_never_appears_under_today(tmp_path, monkeypatch):
    """A venue whose days nobody established must not be presented as open
    today - that is how a student ends up walking to a closed bar."""
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text(
        '[[venue]]\nname = "Confirmed Bar"\ndays = "Mon-Fri"\nstart = "4:00 PM"\n'
        'end = "7:00 PM"\ndeals = "$5 drafts"\nchecked = "2026-08-31"\n\n'
        '[[venue]]\nname = "Mystery Bar"\ndays = "unknown"\nstart = "4:00 PM"\n'
        'deals = "reportedly cheap"\n', encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)

    everything = socials.load()
    assert {h.name for h in everything} == {"Confirmed Bar", "Mystery Bar"}
    assert not [h for h in everything if h.name == "Mystery Bar"][0].confirmed

    monday = date(2026, 8, 31)
    assert [h.name for h in socials.today(when=monday)] == ["Confirmed Bar"]


def test_a_confirmed_venue_only_shows_on_its_days(tmp_path, monkeypatch):
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text(
        '[[venue]]\nname = "Weekend Only"\ndays = "Sat, Sun"\nstart = "2:00 PM"\n',
        encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)
    assert socials.today(when=date(2026, 8, 31)) == []          # a Monday
    assert len(socials.today(when=date(2026, 9, 5))) == 1       # Saturday


def test_missing_start_time_is_not_confirmed(tmp_path, monkeypatch):
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text('[[venue]]\nname = "No Time"\ndays = "Mon-Fri"\n',
                       encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)
    assert not socials.load()[0].confirmed
    assert socials.today(when=date(2026, 8, 31)) == []


# ---- times, sources, overrides -----------------------------------------

def test_time_formats_people_actually_write(tmp_path, monkeypatch):
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text(
        '[[venue]]\nname = "A"\ndays = "Mon"\nstart = "4:00 PM"\nend = "7:00 PM"\n\n'
        '[[venue]]\nname = "B"\ndays = "Mon"\nstart = "16:00"\nend = "19:00"\n\n'
        '[[venue]]\nname = "C"\ndays = "Mon"\nstart = "4PM"\n', encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)
    by = {h.name: h for h in socials.load()}
    assert by["A"].start == time(16, 0) and by["A"].end == time(19, 0)
    assert by["B"].start == time(16, 0)
    assert by["C"].start == time(16, 0) and by["C"].end is None


def test_a_users_own_entry_overrides_the_shipped_one(tmp_path, monkeypatch):
    """The list ships with the app; a student can still fix or add a spot
    without waiting for a release."""
    bundled = tmp_path / "bundled.toml"
    bundled.write_text('[[venue]]\nname = "Uptown Social"\ndays = "Mon-Fri"\n'
                       'start = "4:00 PM"\ndeals = "old info"\n', encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)

    mine = tmp_path / "happy_hours.toml"
    mine.write_text('[[venue]]\nname = "Uptown Social"\ndays = "Tue-Sat"\n'
                    'start = "5:00 PM"\ndeals = "corrected"\n\n'
                    '[[venue]]\nname = "My Local"\ndays = "Fri"\nstart = "6PM"\n',
                    encoding="utf-8")

    class Cfg:
        class settings:
            data_dir = tmp_path
        calendar = None

    monkeypatch.setattr(socials, "user_file", lambda c: mine)
    by = {h.name: h for h in socials.load(Cfg)}
    assert by["Uptown Social"].deals == "corrected"
    assert by["Uptown Social"].start == time(17, 0)
    assert "My Local" in by


def test_a_malformed_file_is_ignored_not_fatal(tmp_path, monkeypatch):
    bad = tmp_path / "happy_hours.toml"
    bad.write_text("this is not toml [[[", encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bad)
    assert socials.load() == []


def test_record_carries_its_provenance(tmp_path, monkeypatch):
    """A happy hour that cannot say where it came from or when it was checked
    is not maintainable - staleness is the known failure mode here."""
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text(
        '[[venue]]\nname = "Cited Bar"\ndays = "Thu"\nstart = "4PM"\n'
        'source = "https://example.test/happy-hour"\nchecked = "2026-08-31"\n',
        encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)
    d = socials.load()[0].to_dict()
    assert d["source"].startswith("https://") and d["checked"] == "2026-08-31"
    assert d["days"] == "Thu" and d["confirmed"] is True


def test_the_shipped_list_is_actually_inside_the_package():
    """It must travel in the wheel or every friend gets an empty tab.

    This regressed once already: .gitignore carried a bare `data/`, which
    also matched src/brain/data/, and hatchling honors ignore files - so the
    file existed locally, the tests passed, and the built wheel silently had
    no list in it.
    """
    assert socials.BUNDLED.exists(), socials.BUNDLED
    assert socials.BUNDLED.parent.name == "data"
    assert socials.BUNDLED.parent.parent.name == "brain"
    # and it must parse into real venues, not silently to []
    assert socials._load_file(socials.BUNDLED), "the shipped list is empty"


# ---- social event feeds -------------------------------------------------

def test_the_same_event_from_two_feeds_appears_once():
    """The campus calendar and Cougar Connect both carry many of the same
    events; the raw merge showed "BNB (Bagels 'n' Bibles)" twice, once per
    feed, differing only in punctuation."""
    from datetime import datetime

    when = datetime(2026, 9, 1, 8, 0)
    evs = [
        socials.SocialEvent(title="BNB (Bagels 'n' Bibles)", starts_at=when,
                            feed="Campus events"),
        socials.SocialEvent(title="BNB (Bagels 'N' Bibles)", starts_at=when,
                            feed="Student orgs"),
        socials.SocialEvent(title="Something else", starts_at=when,
                            feed="Student orgs"),
    ]
    out = socials._dedupe(evs)
    assert len(out) == 2
    assert out[0].feed == "Campus events", "the first feed listed should win"


def test_the_same_title_at_a_different_time_is_a_different_event():
    from datetime import datetime

    evs = [
        socials.SocialEvent(title="Trivia Night",
                            starts_at=datetime(2026, 9, 1, 19, 0)),
        socials.SocialEvent(title="Trivia Night",
                            starts_at=datetime(2026, 9, 8, 19, 0)),
    ]
    assert len(socials._dedupe(evs)) == 2


def test_campus_url_uses_the_param_that_actually_filters():
    """Measured during research: the .ics honors event_types[]; passing
    type[] is SILENTLY IGNORED and returns all 265 events with HTTP 200."""
    url = socials.campus_url("Student Activities", "Athletics")
    assert "event_types%5B%5D=44684995342756" in url
    assert "event_types%5B%5D=44350169269288" in url
    assert "type%5B%5D=4468" not in url.replace("event_type", "x")
    # an unknown category must not silently produce a broken filter
    assert socials.campus_url("Nonsense") == \
        "https://calendar.charleston.edu/calendar/ics"


def test_a_dead_feed_is_reported_and_the_others_still_load(tmp_path, monkeypatch):
    """One broken feed must not empty the tab."""
    from brain import feeds as feedmod

    class Cfg:
        class settings:
            data_dir = tmp_path
        calendar = None

    # Relative to today, not a literal date: fetch_events filters out past
    # events, so a hardcoded DTSTART silently becomes a permanent failure the
    # day it slips into the past. Tomorrow is always inside the days=30 window.
    from datetime import date, timedelta

    start = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    good = tmp_path / "good.ics"
    good.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\n"
        f"SUMMARY:Cougars vs Citadel\nDTSTART:{start}T230000Z\n"
        "END:VEVENT\nEND:VCALENDAR\n", encoding="utf-8")

    def fake_fetch(url, data_dir):
        if "broken" in url:
            return feedmod.FeedResult(url=url, path=None, fetched=False,
                                      stale=False, error="404 Not Found")
        return feedmod.FeedResult(url=url, path=good, fetched=True, stale=False)

    monkeypatch.setattr(feedmod, "fetch", fake_fetch)
    evs, errs = socials.fetch_events(Cfg, days=30, feeds=[
        {"key": "a", "label": "Good feed", "url": "https://x/good.ics"},
        {"key": "b", "label": "Dead feed", "url": "https://x/broken.ics"},
    ])
    assert [e.title for e in evs] == ["Cougars vs Citadel"]
    assert any("Dead feed" in e for e in errs)


def test_venue_and_event_links_survive_to_the_api_shape(tmp_path, monkeypatch):
    """Rows link out to the venue site and the event page, so the url has to
    reach the frontend payload."""
    bundled = tmp_path / "happy_hours.toml"
    bundled.write_text(
        '[[venue]]\nname = "Linked Bar"\ndays = "Mon"\nstart = "4PM"\n'
        'url = "https://example.test/bar"\n', encoding="utf-8")
    monkeypatch.setattr(socials, "BUNDLED", bundled)
    assert socials.load()[0].to_dict()["url"] == "https://example.test/bar"


def test_event_urls_and_locations_are_parsed(tmp_path):
    """calendar.parse_ics_file drops URL and LOCATION, which is why socials
    parses its feeds itself - the links come from those fields."""
    from datetime import date, timedelta

    ics = tmp_path / "f.ics"
    start = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\n"
        "SUMMARY:Cougars vs Citadel\n"
        f"DTSTART:{start}T230000Z\n"
        "LOCATION:TD Arena\nURL:https://cofcsports.com/game/1\n"
        "END:VEVENT\nEND:VCALENDAR\n", encoding="utf-8")
    evs = socials._parse_feed(ics, "Cougars games", date.today(),
                              date.today() + timedelta(days=7))
    assert len(evs) == 1
    d = evs[0].to_dict()
    assert d["url"] == "https://cofcsports.com/game/1"
    assert d["location"] == "TD Arena"


def test_a_non_http_event_url_is_dropped(tmp_path):
    """A feed can carry javascript: or mailto: in URL; those must never
    become an href."""
    from datetime import date, timedelta

    ics = tmp_path / "f.ics"
    start = (date.today() + timedelta(days=1)).strftime("%Y%m%d")
    ics.write_text(
        "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:1\nSUMMARY:Sketchy\n"
        f"DTSTART:{start}T230000Z\nURL:javascript:alert(1)\n"
        "END:VEVENT\nEND:VCALENDAR\n", encoding="utf-8")
    evs = socials._parse_feed(ics, "x", date.today(),
                              date.today() + timedelta(days=7))
    assert evs[0].url == ""


def test_an_unreadable_token_poisons_the_whole_spec():
    """Silently skipping a token it could not read returned a confident
    SUBSET: "Mon, Thu (call ahead)" parsed to Monday only, which is exactly
    the confident-but-wrong answer that [] exists to prevent."""
    for spec in ("Mon, Thu (call ahead)", "Tue, whenever", "Mon, Fri-ish",
                 "Mon-Funday"):
        assert socials.parse_days(spec) == [], spec
    # the good specs still parse
    assert socials.parse_days("Mon, Thu") == [0, 3]
    assert socials.parse_days("Mon-Fri") == [0, 1, 2, 3, 4]


def test_the_shipped_list_has_no_unreadable_day_specs():
    """A typo in the bundled file would silently drop a venue off 'today'."""
    import tomllib

    raw = tomllib.loads(socials.BUNDLED.read_bytes().decode("utf-8-sig"))
    for v in raw["venue"]:
        spec = str(v.get("days") or "")
        if spec and spec.lower() != "unknown":
            assert socials.parse_days(spec), f"{v['name']}: unreadable days {spec!r}"
