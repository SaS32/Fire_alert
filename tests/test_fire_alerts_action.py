"""Tests for fire_alerts_action.py — the NASA FIRMS -> Telegram pipeline.

All tests are offline: fetching is replaced by --csv fixtures, Telegram
delivery by --dry-run or a stubbed requests.post. The module itself must
import with no secrets in the environment (regression for the old import-time
KeyError).
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import fire_alerts_action as fa

CSV_HEADER = ("latitude,longitude,acq_date,acq_time,confidence,frp\n")


def write_csv(path, rows):
    path.write_text(CSV_HEADER + "".join(
        f"{r['latitude']},{r['longitude']},{r['acq_date']},{r['acq_time']},"
        f"{r['confidence']},{r['frp']}\n" for r in rows))


def row(**kw):
    base = {"latitude": "42.68000", "longitude": "23.30000",
            "acq_date": "2026-09-24", "acq_time": "1230",
            "confidence": "nominal", "frp": "10.0"}
    base.update(kw)
    return base


@pytest.fixture
def state(tmp_path, monkeypatch):
    """Redirect every state file into the test directory."""
    for attr, name in (("SEEN_FILE", "seen.json"),
                       ("COOLDOWN_FILE", "cooldown.json"),
                       ("ZONES_FILE", "zones.json")):
        monkeypatch.setattr(fa, attr, str(tmp_path / name))
    return tmp_path


@pytest.fixture
def csvfile(tmp_path):
    def make(rows):
        p = tmp_path / "fires.csv"
        write_csv(p, rows)
        return p
    return make


class TestModuleImport:
    def test_imports_without_secrets(self, monkeypatch):
        for var in ("FIRMS_MAP_KEY", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID",
                    "RUN_MODE", "FIRE_COUNTRY", "FIRE_BBOX", "FIRE_SOURCES",
                    "FIRE_MIN_CONFIDENCE", "FIRE_CENTER_NAME", "FIRE_CENTER_LAT",
                    "FIRE_CENTER_LON", "FIRE_TIMEZONE", "FIRE_FILTER_POLYGON"):
            monkeypatch.delenv(var, raising=False)
        # Reimport under a fresh name to prove import-time safety.
        import importlib
        import sys
        sys.modules.pop("fire_alerts_action", None)
        mod = importlib.import_module("fire_alerts_action")
        assert mod.__version__ == "1.0.0"

    def test_required_env_raises_clear_error(self, monkeypatch):
        monkeypatch.delenv("FIRMS_MAP_KEY", raising=False)
        with pytest.raises(RuntimeError, match="FIRMS_MAP_KEY"):
            fa.firms_map_key()

    def test_required_env_returns_trimmed_value(self, monkeypatch):
        monkeypatch.setenv("FIRMS_MAP_KEY", "  abc123  ")
        assert fa.firms_map_key() == "abc123"


class TestStamps:
    def test_stamp_of_zero_pads_time(self):
        assert fa.stamp_of({"acq_date": "2026-09-24", "acq_time": "43"}) \
            == "2026-09-24 0043"

    def test_stamp_of_missing_time(self):
        assert fa.stamp_of({"acq_date": "2026-09-24"}) == "2026-09-24 0000"

    def test_fmt_seen_formats_local_and_utc(self):
        out = fa.fmt_seen("2026-09-24 1230")
        assert "2026-09-24" in out
        assert "(12:30 UTC)" in out
        assert "EEST" in out or "EET" in out or "UTC" in out

    def test_fmt_seen_malformed_falls_back(self):
        assert fa.fmt_seen("garbage") == "garbage UTC"

    def test_fmt_seen_utc_only_when_no_zone(self, monkeypatch):
        monkeypatch.setattr(fa, "LOCAL_ZONE", None)
        assert fa.fmt_seen("2026-09-24 1230") == "2026-09-24 12:30 UTC"


class TestFrp:
    def test_row_frp_parses(self):
        assert fa.row_frp({"frp": "12.5"}) == 12.5

    def test_row_frp_missing_is_none(self):
        assert fa.row_frp({"frp": ""}) is None
        assert fa.row_frp({}) is None

    def test_row_frp_negative_is_none(self):
        assert fa.row_frp({"frp": "-3"}) is None

    def test_fmt_power_rounding(self):
        assert fa.fmt_power(4.55) == "4.5 MW"
        assert fa.fmt_power(12.0) == "12 MW"
        assert fa.fmt_power(None) == ""
        assert fa.fmt_power(0) == ""


class TestConfidence:
    def test_min_confidence_percent_word(self):
        assert fa.min_confidence_percent() == 30  # nominal default

    def test_min_confidence_percent_digit(self, monkeypatch):
        monkeypatch.setattr(fa, "MIN_CONFIDENCE", "60")
        assert fa.min_confidence_percent() == 60

    def test_min_confidence_class_word(self):
        assert fa.min_confidence_class() == 1  # nominal default

    def test_min_confidence_class_digit(self, monkeypatch):
        monkeypatch.setattr(fa, "MIN_CONFIDENCE", "85")
        assert fa.min_confidence_class() == 2
        monkeypatch.setattr(fa, "MIN_CONFIDENCE", "45")
        assert fa.min_confidence_class() == 1
        monkeypatch.setattr(fa, "MIN_CONFIDENCE", "10")
        assert fa.min_confidence_class() == 0

    def test_confident_enough_viirs_words(self):
        assert fa.confident_enough({"confidence": "high"})
        assert fa.confident_enough({"confidence": "nominal"})
        assert not fa.confident_enough({"confidence": "low"})  # default is nominal

    def test_confident_enough_modis_numbers(self):
        assert fa.confident_enough({"confidence": "80"})
        assert fa.confident_enough({"confidence": "30"})
        assert not fa.confident_enough({"confidence": "29"})

    def test_confident_enough_empty_is_filtered(self):
        assert not fa.confident_enough({"confidence": ""})
        assert not fa.confident_enough({})

    def test_word_and_number_scales_agree_at_boundary(self, monkeypatch):
        monkeypatch.setattr(fa, "MIN_CONFIDENCE", "80")
        assert fa.confident_enough({"confidence": "high"})   # VIIRS high
        assert fa.confident_enough({"confidence": "80"})     # MODIS 80
        assert not fa.confident_enough({"confidence": "nominal"})  # below high
        assert not fa.confident_enough({"confidence": "79"})


class TestIds:
    def test_detection_id_shape(self):
        assert fa.detection_id(row()) == "42.68000_23.30000_2026-09-24_1230"

    def test_acquired_at_extracts_date_time(self):
        assert fa.acquired_at("42.7_23.3_2026-09-24_43") == ("2026-09-24", "0043")

    def test_acquired_at_junk_is_oldest(self):
        assert fa.acquired_at("junk_id_without_four_parts") == ("", "")

    def test_save_seen_keeps_newest_not_northernmost(self, tmp_path, monkeypatch):
        monkeypatch.setattr(fa, "SEEN_FILE", str(tmp_path / "s.json"))
        seen = {
            "44.0_23.0_2026-09-01_1200",   # northernmost
            "41.0_23.0_2026-09-24_0500",   # southern, newest
        }
        fa.save_seen(seen)
        saved = json.loads((tmp_path / "s.json").read_text())
        assert "41.0_23.0_2026-09-24_0500" in saved


class TestClustering:
    def test_merges_close_detections(self):
        rows = [{"latitude": "42.680", "longitude": "23.300",
                 "acq_date": "2026-09-24", "acq_time": "1000", "frp": "4"},
                {"latitude": "42.682", "longitude": "23.302",
                 "acq_date": "2026-09-24", "acq_time": "1100", "frp": "6"}]
        clusters = fa.cluster_fires(rows)
        assert len(clusters) == 1
        assert clusters[0]["count"] == 2
        assert clusters[0]["frp"] == pytest.approx(10.0)
        assert clusters[0]["last_seen"] == "2026-09-24 1100"

    def test_far_detections_stay_separate(self):
        rows = [{"latitude": "42.0", "longitude": "23.0",
                 "acq_date": "2026-09-24", "acq_time": "1000", "frp": ""},
                {"latitude": "43.5", "longitude": "25.0",
                 "acq_date": "2026-09-24", "acq_time": "1000", "frp": ""}]
        assert len(fa.cluster_fires(rows)) == 2

    def test_missing_coordinates_skipped(self):
        rows = [{"acq_date": "2026-09-24", "acq_time": "1000"}]
        assert fa.cluster_fires(rows) == []

    def test_centroid_tracks_mean(self):
        rows = [{"latitude": "42.685", "longitude": "23.300",
                 "acq_date": "2026-09-24", "acq_time": "1000", "frp": ""},
                {"latitude": "42.700", "longitude": "23.310",
                 "acq_date": "2026-09-24", "acq_time": "1000", "frp": ""}]
        c = fa.cluster_fires(rows)[0]
        assert c["lat"] == pytest.approx(42.6925)
        assert c["lon"] == pytest.approx(23.305)


class TestGeometry:
    SQUARE = [(0, 0), (0, 10), (10, 10), (10, 0)]

    def test_point_inside_square(self):
        assert fa.point_in_polygon(5, 5, self.SQUARE)

    def test_point_outside_square(self):
        assert not fa.point_in_polygon(15, 5, self.SQUARE)

    def test_sofia_inside_bulgaria(self):
        assert fa.point_in_polygon(42.6977, 23.3219, fa.BG_POLYGON)

    def test_athens_outside_bulgaria(self):
        assert not fa.point_in_polygon(37.98, 23.73, fa.BG_POLYGON)

    def test_near_border_inside_buffer(self):
        # The polygon edge south of Plovdiv runs ~41.37N at lon 24.0; a point
        # ~4 km south of it stays inside the 5 km buffer.
        assert fa.near_border(41.337, 24.0, fa.BG_POLYGON, 5.0)

    def test_far_from_border(self):
        assert not fa.near_border(40.5, 24.0, fa.BG_POLYGON, 5.0)

    def test_km_to_segment_beyond_endpoint(self):
        # Point beyond segment end: distance to the endpoint itself.
        d = fa.km_to_segment(42.0, 23.0, (42.0, 23.0), (42.0, 23.1))
        assert d == pytest.approx(0.0, abs=1e-6)

    def test_km_to_segment_perpendicular(self):
        d = fa.km_to_segment(42.01, 23.05, (42.0, 23.0), (42.0, 23.1))
        assert d == pytest.approx(1.1, abs=0.1)  # ~1.1 km north of the segment

    def test_in_area_polygon_off_accepts_all(self, monkeypatch):
        monkeypatch.setattr(fa, "FILTER_TO_POLYGON", False)
        assert fa.in_area({})
        assert fa.in_area({"latitude": "1", "longitude": "2"})

    def test_in_area_missing_coords_rejected(self):
        assert not fa.in_area({})

    def test_in_area_bulgaria_and_buffer(self):
        assert fa.in_area({"latitude": "42.7", "longitude": "23.3"})
        assert fa.in_area({"latitude": "41.337", "longitude": "24.0"})
        assert not fa.in_area({"latitude": "40.5", "longitude": "24.0"})


class TestZonesAtRuntime:
    def test_load_excluded_zones_shared(self):
        # Delegates to the shared geo loader (one parser, not two).
        import geo
        assert fa.load_zones is geo.load_zones

    def test_zone_containing_inside(self):
        zones = [{"name": "x", "lat": 42.7, "lon": 23.3, "radius_km": 1.0,
                  "max_frp": None}]
        assert fa.zone_containing({"latitude": "42.7001", "longitude": "23.3001"},
                                  zones)["name"] == "x"

    def test_zone_containing_outside(self):
        zones = [{"name": "x", "lat": 42.7, "lon": 23.3, "radius_km": 1.0,
                  "max_frp": None}]
        assert fa.zone_containing({"latitude": "41.0", "longitude": "20.0"},
                                  zones) is None

    def test_zone_containing_no_zones(self):
        assert fa.zone_containing(row(), []) is None

    def test_zone_containing_bad_coords(self):
        zones = [{"name": "x", "lat": 42.7, "lon": 23.3, "radius_km": 1.0,
                  "max_frp": None}]
        assert fa.zone_containing({"latitude": "abc"}, zones) is None

    def test_unreadable_max_frp_zone_still_suppresses(self, state, tmp_path):
        # Regression: a zone with a bad max_frp must keep working (silently)
        # instead of vanishing and leaking its factory's detections.
        (tmp_path / "zones.json").write_text(json.dumps([
            {"name": "factory", "lat": 42.7, "lon": 23.3, "radius_km": 1.0,
             "max_frp": "oops"}]))
        zones = fa.load_excluded_zones()
        assert len(zones) == 1
        assert zones[0]["max_frp"] is None
        inside = fa.zone_containing({"latitude": "42.7001", "longitude": "23.3001"},
                                    zones)
        assert inside is not None


class TestCooldown:
    def test_in_cooldown_within_window(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        cluster = {"lat": 42.7, "lon": 23.3, "dist_km": 100.0}
        entries = [{"lat": 42.7, "lon": 23.3,
                    "last_alert": now - timedelta(hours=1)}]
        assert fa.in_cooldown(cluster, entries, now)

    def test_in_cooldown_expired(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        cluster = {"lat": 42.7, "lon": 23.3, "dist_km": 100.0}
        entries = [{"lat": 42.7, "lon": 23.3,
                    "last_alert": now - timedelta(hours=7)}]
        assert not fa.in_cooldown(cluster, entries, now)

    def test_near_fire_shorter_window(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        # 4h old: outside the 2h near window, inside the 6h far window.
        cluster = {"lat": 42.7, "lon": 23.3, "dist_km": 1.0}
        entries = [{"lat": 42.7, "lon": 23.3,
                    "last_alert": now - timedelta(hours=4)}]
        assert not fa.in_cooldown(cluster, entries, now)
        cluster_far = {"lat": 42.7, "lon": 23.3, "dist_km": 100.0}
        assert fa.in_cooldown(cluster_far, entries, now)

    def test_negative_age_is_expired(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        cluster = {"lat": 42.7, "lon": 23.3, "dist_km": 100.0}
        entries = [{"lat": 42.7, "lon": 23.3,
                    "last_alert": now + timedelta(hours=2)}]  # clock skew
        assert not fa.in_cooldown(cluster, entries, now)

    def test_beyond_match_km_not_same_fire(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        cluster = {"lat": 42.7, "lon": 23.3, "dist_km": 100.0}
        entries = [{"lat": 43.0, "lon": 25.0,   # ~160 km away
                    "last_alert": now - timedelta(hours=1)}]
        assert not fa.in_cooldown(cluster, entries, now)

    def test_remember_alerts_replaces_nearby(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        old = [{"lat": 42.7, "lon": 23.3, "last_alert": now - timedelta(hours=5)}]
        clusters = [{"lat": 42.7001, "lon": 23.3001}]
        kept = fa.remember_alerts(clusters, old, now)
        assert len(kept) == 1
        assert kept[0]["last_alert"] == now

    def test_remember_alerts_prunes_expired(self):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        stale = [{"lat": 42.0, "lon": 23.0, "last_alert": now - timedelta(hours=50)}]
        kept = fa.remember_alerts([], stale, now)
        assert kept == []

    def test_cooldown_roundtrip(self, state, tmp_path):
        now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
        fa.save_cooldown([{"lat": 42.700005, "lon": 23.300004,
                           "last_alert": now}])
        loaded = fa.load_cooldown()
        assert len(loaded) == 1
        assert loaded[0]["last_alert"] == now

    def test_corrupt_cooldown_fails_open(self, state, tmp_path, capsys):
        (tmp_path / "cooldown.json").write_text("{ nope")
        assert fa.load_cooldown() == []
        assert "no cooldowns applied" in capsys.readouterr().out


class TestFormatting:
    def _cluster(self, lat=42.68, lon=23.30, count=2, frp=None, last="2026-09-24 1230"):
        return {"lat": lat, "lon": lon, "count": count, "frp": frp,
                "last_seen": last}

    def test_marker_near_vs_far(self):
        near = self._cluster(); near["dist_km"] = 5.0
        far = self._cluster(); far["dist_km"] = 50.0
        assert fa.marker(near) == "🚨"
        assert fa.marker(far) == "•"

    def test_describe_place(self):
        c = self._cluster(); c["dist_km"] = 34.0; c["direction"] = "NE"
        assert fa.describe_place(c) == "34 km NE of Sofia"

    def test_fmt_title_near_alarms(self):
        c = self._cluster(); c["dist_km"] = 5.0; c["direction"] = "SW"
        title = fa.fmt_title([c], "2 fire(s) with new activity", "🔥")
        assert title.startswith("🚨 FIRE")

    def test_fmt_title_calm_when_not_near(self):
        c = self._cluster(); c["dist_km"] = 80.0; c["direction"] = "E"
        title = fa.fmt_title([c], "Report: 1 active fire(s)", "📋")
        assert title.startswith("📋")

    def test_fmt_clusters_truncates(self):
        clusters = []
        for i in range(fa.MAX_ITEMS + 3):
            c = self._cluster(lat=42.0 + i * 0.1, lon=23.0, count=1)
            c["dist_km"] = float(i)
            c["direction"] = "N"
            clusters.append(c)
        out = fa.fmt_clusters(clusters, "title")
        assert f"...and {fa.MAX_ITEMS + 3 - fa.MAX_ITEMS} more fires" in out

    def test_fmt_size_empty(self):
        assert fa.fmt_size({"frp": None}) == ""


class TestTelegramTransport:
    def _resp(self, ok=True, **kw):
        class R:
            def json(self):
                return {"ok": ok, **kw}

            def raise_for_status(self):
                pass
        return R()

    def test_checked_accepts_ok(self):
        assert fa._checked(self._resp(True, result="x"), "message")["result"] == "x"

    def test_checked_rejects_ok_false(self):
        # Regression: Telegram returns HTTP 200 with ok:false for many failures;
        # those must raise so seen_fires.json is not committed for a lost alert.
        with pytest.raises(RuntimeError, match="Telegram rejected message"):
            fa._checked(self._resp(False, description="bot was blocked"), "message")

    def test_checked_rejects_non_json(self):
        class R:
            def json(self):
                raise ValueError("no json")

            def raise_for_status(self):
                pass
        with pytest.raises(RuntimeError):
            fa._checked(R(), "message")

    def test_send_telegram_posts_and_checks(self, monkeypatch):
        posted = {}

        class R:
            status = 200

            def json(self):
                return {"ok": True}

            def raise_for_status(self):
                pass

        def fake_post(url, data, timeout):
            posted["url"] = url
            posted["data"] = data
            return R()

        monkeypatch.setattr(fa.requests, "post", fake_post)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tok")
        monkeypatch.setenv("TELEGRAM_CHAT_ID", "cid")
        fa.send_telegram("hello")
        assert "bottok/sendMessage" in posted["url"]
        assert posted["data"]["chat_id"] == "cid"
        assert posted["data"]["text"] == "hello"

    def test_send_telegram_dry_run_no_post(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("must not post in dry-run")
        monkeypatch.setattr(fa.requests, "post", boom)
        fa.send_telegram("hello", dry_run=True)
        assert "[dry-run]" in capsys.readouterr().out

    def test_send_location_dry_run(self, capsys):
        fa.send_location(42.7, 23.3, dry_run=True)
        assert "42.70000,23.30000" in capsys.readouterr().out

    def test_send_satellite_photo_dry_run(self, capsys):
        fa.send_satellite_photo(42.7, 23.3, "caption here", dry_run=True)
        out = capsys.readouterr().out
        assert "[dry-run]" in out
        assert "caption here" in out

    def test_send_map_pins_dry_run_skips_photos(self, monkeypatch, capsys):
        def boom(*a, **k):
            raise AssertionError("dry-run must not hit the network")
        monkeypatch.setattr(fa.requests, "get", boom)
        c = {"lat": 42.68, "lon": 23.30, "count": 2, "frp": None,
             "dist_km": 5.0, "direction": "SW", "last_seen": "2026-09-24 1230"}
        fa.send_map_pins([c], dry_run=True)
        out = capsys.readouterr().out
        assert "Satellite photo" in out


class TestMainOffline:
    """End-to-end runs against locale CSV files with dry-run delivery."""

    CSV = ("latitude,longitude,acq_date,acq_time,confidence,frp\n"
           "42.67000,23.30000,2026-09-24,1230,nominal,12.5\n"
           "42.69000,23.31000,2026-09-24,1245,nominal,8.2\n")

    def _run(self, monkeypatch, state, rows, *extra, polygon=True):
        monkeypatch.setattr(fa, "FILTER_TO_POLYGON", polygon)
        p = state / "fires.csv"
        write_csv(p, rows)
        fa.main(["--csv", str(p), "--dry-run", *extra])
        return p

    def test_check_mode_alerts_new_fires(self, monkeypatch, state, capsys):
        self._run(monkeypatch, state, [row()])
        out = capsys.readouterr().out
        assert "1 fire(s) with new activity" in out
        assert "[dry-run] Telegram message" in out
        assert (state / "seen.json").exists()
        assert (state / "cooldown.json").exists()

    def test_second_run_is_quiet(self, monkeypatch, state, capsys):
        rows = [row()]
        self._run(monkeypatch, state, rows)
        capsys.readouterr().out
        self._run(monkeypatch, state, rows)
        out = capsys.readouterr().out
        assert "No new detections." in out

    def test_report_mode_ignores_cooldown(self, monkeypatch, state, capsys):
        rows = [row()]
        self._run(monkeypatch, state, rows, "--mode", "check")
        capsys.readouterr().out
        # Same fire again: check is quiet (cooldown), report shows it anyway.
        self._run(monkeypatch, state, rows, "--mode", "report")
        out = capsys.readouterr().out
        assert "Report: 1 active fire(s)" in out

    def test_zone_suppression(self, monkeypatch, state, capsys):
        rows = [row(latitude="42.70100", longitude="23.30100")]  # inside zone
        (state / "zones.json").write_text(json.dumps([
            {"name": "factory", "lat": 42.7, "lon": 23.3, "radius_km": 1.0}]))
        self._run(monkeypatch, state, rows)
        out = capsys.readouterr().out
        assert "Suppressed 1 detection(s) in excluded zones: factory (1)" in out
        assert "No new detections." in out
        # Suppressed detections are not remembered as announced.
        assert json.loads((state / "seen.json").read_text()) == []

    def test_max_frp_override_reports(self, monkeypatch, state, capsys):
        rows = [row(latitude="42.70100", longitude="23.30100", frp="300")]
        (state / "zones.json").write_text(json.dumps([
            {"name": "factory", "lat": 42.7, "lon": 23.3, "radius_km": 1.0,
             "max_frp": 150}]))
        self._run(monkeypatch, state, rows)
        out = capsys.readouterr().out
        assert "over its max_frp: factory" in out
        assert "1 fire(s) with new activity" in out
        assert len(json.loads((state / "seen.json").read_text())) == 1

    def test_polygon_filter_counts_skipped(self, monkeypatch, state, capsys):
        rows = [row(latitude="37.98", longitude="23.73")]  # Athens
        self._run(monkeypatch, state, rows)
        out = capsys.readouterr().out
        assert "Filtered out 1 detection(s) outside Bulgaria" in out
        assert "No new detections." in out

    def test_polygon_off_via_env(self, monkeypatch, state):
        monkeypatch.setenv("FIRE_FILTER_POLYGON", "false")
        import importlib
        import sys
        sys.modules.pop("fire_alerts_action", None)
        mod = importlib.import_module("fire_alerts_action")
        assert mod.FILTER_TO_POLYGON is False

    def test_outage_raises_and_warns(self, monkeypatch, state, capsys, csvfile):
        def fake_load_rows(args):
            return [], ["VIIRS_NOAA20_NRT"], {"VIIRS_NOAA20_NRT": "boom"}
        monkeypatch.setattr(fa, "load_rows", fake_load_rows)
        with pytest.raises(RuntimeError, match="All satellite sources failed"):
            fa.main(["--dry-run"])
        out = capsys.readouterr().out
        assert "[dry-run]" in out  # the outage warning was drafted

    def test_send_failure_does_not_commit_seen(self, monkeypatch, state, capsys):
        # Regression: if Telegram rejects the alert (ok:false), the fire must
        # NOT be marked as announced — otherwise it is silently lost forever.
        def boom(message, dry_run=False):
            raise RuntimeError("Telegram rejected message: blocked")
        monkeypatch.setattr(fa, "send_telegram", boom)
        p = state / "fires.csv"
        write_csv(p, [row()])
        with pytest.raises(RuntimeError):
            fa.main(["--csv", str(p)])
        assert not (state / "seen.json").exists()

    def test_version_flag(self, capsys):
        with pytest.raises(SystemExit):
            fa.main(["--version"])
        assert "1.0.0" in capsys.readouterr().out

    def test_missing_csv_file_fails(self, state):
        with pytest.raises(FileNotFoundError):
            fa.main(["--csv", str(state / "nope.csv"), "--dry-run"])