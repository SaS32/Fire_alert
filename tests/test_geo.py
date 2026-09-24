"""Tests for geo.py — the shared distance/direction/zone helpers."""

import json

import pytest

import geo
from geo import (compass_from, covering_zone, describe_offset, env_float,
                 fmt_distance, great_circle_km, km_between, load_center,
                 load_zones)


class TestDistance:
    def test_km_between_one_degree_latitude(self):
        # 1 degree of latitude is ~110.57 km everywhere.
        d = km_between(42.0, 23.0, 43.0, 23.0)
        assert d == pytest.approx(110.57, abs=0.01)

    def test_km_between_zero(self):
        assert km_between(42.7, 23.3, 42.7, 23.3) == 0.0

    def test_km_between_symmetric(self):
        a = km_between(41.0, 22.0, 43.0, 25.0)
        b = km_between(43.0, 25.0, 41.0, 22.0)
        assert a == pytest.approx(b)

    def test_km_between_lon_scale_shrinks_northward(self):
        # One degree of longitude is ~111 km at the equator but ~70 km at 51N.
        south = km_between(0.0, 20.0, 0.0, 21.0)
        north = km_between(51.0, 20.0, 51.0, 21.0)
        assert south > 100
        assert 60 < north < 80

    def test_great_circle_one_degree_at_equator(self):
        d = great_circle_km(0.0, 0.0, 0.0, 1.0)
        assert d == pytest.approx(111.195, abs=0.01)

    def test_great_circle_half_earth(self):
        d = great_circle_km(0.0, 0.0, 0.0, 180.0)
        assert d == pytest.approx(20015.0, abs=1.0)

    def test_great_circle_symmetric(self):
        a = great_circle_km(41.0, 22.0, 43.0, 25.0)
        b = great_circle_km(43.0, 25.0, 41.0, 22.0)
        assert a == pytest.approx(b)

    def test_flat_and_great_circle_agree_locally(self):
        # Over a few km the two approximations should be within a few percent.
        flat = km_between(42.69, 23.32, 42.72, 23.35)
        hav = great_circle_km(42.69, 23.32, 42.72, 23.35)
        assert abs(flat - hav) / hav < 0.05


class TestCompass:
    def test_all_four_cardinals(self):
        assert compass_from(42.7, 23.3, 42.8, 23.3) == "N"    # north
        assert compass_from(42.7, 23.3, 42.6, 23.3) == "S"    # south
        assert compass_from(42.7, 23.3, 42.7, 23.4) == "E"    # east
        assert compass_from(42.7, 23.3, 42.7, 23.2) == "W"    # west

    def test_intercardinal(self):
        assert compass_from(42.7, 23.3, 42.8, 23.4) == "NE"

    def test_every_direction_covered(self):
        # A point circling the centre at each 16-wind centre should hit every
        # direction exactly once.
        seen = set()
        for i in range(16):
            deg = i * 22.5
            lat = 42.7 + 0.5 * math_cos(deg)
            lon = 23.3 + 0.5 * math_sin(deg)
            seen.add(compass_from(42.7, 23.3, lat, lon))
        assert len(seen) == 16

    def test_tiny_offset_stays_north(self):
        assert compass_from(42.7, 23.3, 42.7001, 23.3) == "N"


def math_sin(deg):
    import math
    return math.sin(math.radians(deg))


def math_cos(deg):
    import math
    return math.cos(math.radians(deg))


class TestFmtDistance:
    def test_metres_below_1km(self):
        assert fmt_distance(0.45) == "450 m"

    def test_one_decimal_under_10km(self):
        assert fmt_distance(5.55) == "5.5 km"

    def test_round_kilometres_above_10(self):
        assert fmt_distance(42.6) == "43 km"

    def test_zero(self):
        assert fmt_distance(0) == "0 m"

    def test_exactly_one_kilometre(self):
        assert fmt_distance(1.0) == "1.0 km"


class TestDescribeOffset:
    def test_format(self):
        center = ("Sofia", 42.6977, 23.3219)
        # Plovdiv is roughly 130 km east-southeast of Sofia.
        out = describe_offset(center, 42.1433, 24.7491)
        assert "of Sofia" in out
        assert "E" in out

    def test_uses_given_name(self):
        out = describe_offset(("home", 42.7, 23.3), 42.7, 23.3)
        assert out.endswith("of home")


class TestEnvFloat:
    def test_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("GX_TEST", raising=False)
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 5.0

    def test_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("GX_TEST", "  ")
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 5.0

    def test_parses_valid(self, monkeypatch):
        monkeypatch.setenv("GX_TEST", "7.5")
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 7.5

    def test_non_numeric_warns_and_defaults(self, monkeypatch, capsys):
        monkeypatch.setenv("GX_TEST", "abc")
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 5.0
        assert "not a number" in capsys.readouterr().out

    def test_out_of_range_warns_and_defaults(self, monkeypatch, capsys):
        monkeypatch.setenv("GX_TEST", "999")
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 5.0
        assert "outside" in capsys.readouterr().out

    def test_boundary_values_accepted(self, monkeypatch):
        monkeypatch.setenv("GX_TEST", "10")
        assert env_float("GX_TEST", 5.0, 0.0, 10.0) == 10.0


class TestLoadCenter:
    def test_defaults(self, monkeypatch):
        monkeypatch.delenv("FIRE_CENTER_NAME", raising=False)
        monkeypatch.delenv("FIRE_CENTER_LAT", raising=False)
        monkeypatch.delenv("FIRE_CENTER_LON", raising=False)
        assert load_center() == ("Sofia", 42.6977, 23.3219)

    def test_env_overrides(self, monkeypatch):
        monkeypatch.setenv("FIRE_CENTER_NAME", "Plovdiv")
        monkeypatch.setenv("FIRE_CENTER_LAT", "42.1433")
        monkeypatch.setenv("FIRE_CENTER_LON", "24.7491")
        assert load_center() == ("Plovdiv", 42.1433, 24.7491)

    def test_bad_coordinate_falls_back(self, monkeypatch, capsys):
        monkeypatch.setenv("FIRE_CENTER_LAT", "north-ish")
        name, lat, lon = load_center()
        assert name == "Sofia"
        assert lat == geo.DEFAULT_CENTER_LAT
        assert "Warning" in capsys.readouterr().out


class TestLoadZones:
    def test_missing_file_returns_empty(self, tmp_path):
        assert load_zones(str(tmp_path / "missing.json")) == []

    def test_bad_json_returns_empty_with_warning(self, tmp_path, capsys):
        p = tmp_path / "z.json"
        p.write_text("{ not json")
        assert load_zones(str(p)) == []
        assert "could not read" in capsys.readouterr().out

    def test_not_a_list_returns_empty(self, tmp_path, capsys):
        p = tmp_path / "z.json"
        p.write_text('{"lat": 1}')
        assert load_zones(str(p)) == []
        assert "not a list" in capsys.readouterr().out

    def test_valid_entry_with_default_radius(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "factory", "lat": 42.7, "lon": 23.3}]))
        zones = load_zones(str(p), default_radius_km=0.5)
        assert zones == [{"name": "factory", "lat": 42.7, "lon": 23.3,
                          "radius_km": 0.5, "max_frp": None}]

    def test_radius_override(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "x", "lat": 1.0, "lon": 1.0,
                                  "radius_km": 2.5}]))
        zones = load_zones(str(p), default_radius_km=0.2)
        assert zones[0]["radius_km"] == 2.5

    def test_non_numeric_radius_uses_default(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "x", "lat": 1.0, "lon": 1.0,
                                  "radius_km": "big"}]))
        zones = load_zones(str(p), default_radius_km=0.3)
        assert zones[0]["radius_km"] == 0.3

    def test_max_frp_parsed(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "x", "lat": 1.0, "lon": 1.0,
                                  "max_frp": 150}]))
        assert load_zones(str(p))[0]["max_frp"] == 150.0

    def test_max_frp_string_parsed(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "x", "lat": 1.0, "lon": 1.0,
                                  "max_frp": "150"}]))
        assert load_zones(str(p))[0]["max_frp"] == 150.0

    def test_unreadable_max_frp_keeps_silent_zone(self, tmp_path, capsys):
        # Regression: a bad max_frp used to drop the whole zone, letting the
        # factory's daily detections leak back into the alerts.
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "factory", "lat": 42.7, "lon": 23.3,
                                  "radius_km": 1.0, "max_frp": "oops"}]))
        zones = load_zones(str(p))
        assert len(zones) == 1
        assert zones[0]["name"] == "factory"
        assert zones[0]["max_frp"] is None
        assert "unreadable max_frp" in capsys.readouterr().out

    def test_negative_max_frp_becomes_none(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"name": "x", "lat": 1.0, "lon": 1.0,
                                  "max_frp": -5}]))
        assert load_zones(str(p))[0]["max_frp"] is None

    def test_malformed_entry_skipped_with_warning(self, tmp_path, capsys):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([
            {"name": "good", "lat": 42.0, "lon": 23.0},
            {"name": "no coords"},
        ]))
        zones = load_zones(str(p))
        assert [z["name"] for z in zones] == ["good"]
        assert "malformed entry #2" in capsys.readouterr().out

    def test_sibling_entry_survives_bad_one(self, tmp_path):
        # A second bad zone must not take a good one down with it.
        p = tmp_path / "z.json"
        p.write_text(json.dumps([
            {"name": "bad", "lat": "not-a-number", "lon": 1.0},
            {"name": "good", "lat": 1.0, "lon": 1.0},
        ]))
        zones = load_zones(str(p))
        assert [z["name"] for z in zones] == ["good"]

    def test_name_fallback(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([{"lat": 1.0, "lon": 1.0}]))
        assert load_zones(str(p))[0]["name"] == "zone 1"


class TestCoveringZone:
    @pytest.fixture
    def zones(self, tmp_path):
        p = tmp_path / "z.json"
        p.write_text(json.dumps([
            {"name": "a", "lat": 42.7, "lon": 23.3, "radius_km": 1.0},
            {"name": "b", "lat": 43.0, "lon": 25.0, "radius_km": 2.0},
        ]))
        return load_zones(str(p))

    def test_inside_returns_zone(self, zones):
        z = covering_zone(42.7001, 23.3001, zones)
        assert z is not None and z["name"] == "a"

    def test_outside_returns_none(self, zones):
        assert covering_zone(41.0, 20.0, zones) is None

    def test_empty_zones(self):
        assert covering_zone(42.7, 23.3, []) is None

    def test_boundary_inside(self, zones):
        # Exactly radius_km away: km_between <= radius, inclusive.
        z = covering_zone(42.7 + 1.0 / 110.57, 23.3, zones)
        assert z is not None