"""Tests for find_hotspots.py — the recurring-detection report."""

import json

import pytest

import find_hotspots as fh


def write_json(path, data):
    path.write_text(json.dumps(data))


class TestLoadDetections:
    def test_parses_four_part_ids(self, tmp_path):
        p = tmp_path / "seen.json"
        write_json(p, [
            "42.71343_24.17352_2026-07-16_43",
            "41.79461_26.70269_2026-07-17_1200",
        ])
        pts = fh.load_detections(str(p))
        assert pts == [
            (42.71343, 24.17352, "2026-07-16"),
            (41.79461, 26.70269, "2026-07-17"),
        ]

    def test_unparseable_skipped_and_counted(self, tmp_path, capsys):
        p = tmp_path / "seen.json"
        write_json(p, ["42.7_23.3_2026-07-16_1200", "junk", 42.5])
        pts = fh.load_detections(str(p))
        assert len(pts) == 1
        assert "skipped 2 unparseable" in capsys.readouterr().out

    def test_missing_file_exits(self, tmp_path):
        with pytest.raises(SystemExit) as ei:
            fh.load_detections(str(tmp_path / "nope.json"))
        assert "No" in str(ei.value)

    def test_unreadable_json_exits(self, tmp_path):
        p = tmp_path / "seen.json"
        p.write_text("{ nope")
        with pytest.raises(SystemExit):
            fh.load_detections(str(p))


class TestCluster:
    def test_nearby_points_one_group(self):
        points = [(42.7000, 23.3000, "2026-09-01"),
                  (42.7001, 23.3001, "2026-09-01"),
                  (42.7002, 23.3002, "2026-09-02")]
        groups = fh.cluster(points, 0.5)
        assert len(groups) == 1
        assert groups[0]["days"] == {"2026-09-01", "2026-09-02"}
        assert len(groups[0]["points"]) == 3

    def test_far_points_separate_groups(self):
        points = [(42.70, 23.30, "2026-09-01"),
                  (43.50, 25.00, "2026-09-01")]
        groups = fh.cluster(points, 0.5)
        assert len(groups) == 2

    def test_centroid_is_mean(self):
        points = [(42.7000, 23.3000, "2026-09-01"),
                  (42.7002, 23.3002, "2026-09-01")]
        groups = fh.cluster(points, 0.5)
        assert groups[0]["lat"] == pytest.approx(42.7001)
        assert groups[0]["lon"] == pytest.approx(23.3001)

    def test_sorted_by_days_then_points(self):
        points = [
            (42.70, 23.30, "2026-09-01"),
            (42.70, 23.30, "2026-09-02"),
            (42.70, 23.30, "2026-09-03"),          # 3 days, 3 points
            (43.00, 24.00, "2026-09-01"),
            (43.00, 24.01, "2026-09-01"),
            (43.00, 24.02, "2026-09-01"),
            (43.00, 24.03, "2026-09-01"),
            (43.00, 24.04, "2026-09-01"),
            (43.00, 24.05, "2026-09-01"),          # 1 day, 6 points
        ]
        groups = fh.cluster(points, 0.5)
        assert len(groups[0]["days"]) == 3  # days outrank raw point count

    def test_spread_computed(self):
        points = [(42.7000, 23.3000, "2026-09-01"),
                  (42.7005, 23.3005, "2026-09-01")]
        groups = fh.cluster(points, 0.5)
        assert groups[0]["spread_km"] > 0


class TestSuggestedRadius:
    def test_floor(self):
        assert fh.suggested_radius(0.0) == fh.MIN_SUGGESTED_KM

    def test_margin_rounding(self):
        # 0.455 * 1.35 = 0.614 -> round(0.614 + 0.049, 1) = 0.7
        assert fh.suggested_radius(0.455) == 0.7

    def test_larger_spread(self):
        assert fh.suggested_radius(1.0) > fh.MIN_SUGGESTED_KM


class TestDescribe:
    def test_recurring_line_and_map_link(self, capsys):
        g = {"lat": 42.7155, "lon": 24.1708, "points": [(42.7155, 24.1708)],
             "days": {"2026-09-01", "2026-09-02"}, "spread_km": 0.455}
        fh.describe(g, 10, None, ("Sofia", 42.6977, 23.3219))
        out = capsys.readouterr().out
        assert "2/10 days" in out
        assert "maps.google.com" in out
        assert '"radius_km"' in out  # a suggestion line is emitted

    def test_already_excluded_marked(self, capsys):
        g = {"lat": 42.7155, "lon": 24.1708, "points": [(42.7155, 24.1708)],
             "days": {"2026-09-01"}, "spread_km": 0.3}
        zone = {"name": "Pirdop", "lat": 42.7155, "lon": 24.1708,
                "radius_km": 1.0, "max_frp": None}
        fh.describe(g, 10, zone, ("Sofia", 42.6977, 23.3219))
        out = capsys.readouterr().out
        assert "already excluded" in out
        assert "covered by: Pirdop" in out


class TestZonesShared:
    def test_uses_shared_loader(self):
        # find_hotspots must not ship its own second copy of the parser.
        import geo
        assert fh.load_zones is geo.load_zones
        assert not hasattr(fh, "covering_zone") or fh.covering_zone is geo.covering_zone

    def test_zones_mark_covered_spots(self, tmp_path, capsys):
        seen = tmp_path / "seen.json"
        write_json(seen, ["42.7155_24.1708_2026-09-01_1200",
                          "42.7155_24.1708_2026-09-02_1200"])
        zones = tmp_path / "z.json"
        write_json(zones, [{"name": "Pirdop", "lat": 42.7155, "lon": 24.1708,
                            "radius_km": 0.6}])
        fh.main(["--seen", str(seen), "--zones", str(zones), "--min-days", "1"])
        out = capsys.readouterr().out
        assert "already excluded" in out
        assert "covered by: Pirdop" in out


class TestMain:
    def _args(self, tmp_path, *extra):
        return ["--seen", str(tmp_path / "seen.json"),
                "--zones", str(tmp_path / "zones.json")] + list(extra)

    def test_no_detections_exits(self, tmp_path):
        seen = tmp_path / "seen.json"
        write_json(seen, [])
        with pytest.raises(SystemExit) as ei:
            fh.main(self._args(tmp_path))
        assert "No detections" in str(ei.value)

    def test_all_lists_one_day_fires(self, tmp_path, capsys):
        seen = tmp_path / "seen.json"
        write_json(seen, ["42.70_23.30_2026-09-01_1200"])
        fh.main(self._args(tmp_path, "--all"))
        out = capsys.readouterr().out
        assert "1 location(s) seen on a single day" in out

    def test_min_days_filters_out_one_day_fire(self, tmp_path, capsys):
        seen = tmp_path / "seen.json"
        write_json(seen, ["42.70_23.30_2026-09-01_1200"])
        fh.main(self._args(tmp_path, "--min-days", "4"))
        out = capsys.readouterr().out
        assert "Nothing recurred" in out

    def test_recurring_reported(self, tmp_path, capsys):
        seen = tmp_path / "seen.json"
        write_json(seen, ["42.70_23.30_2026-09-01_1200",
                          "42.70_23.30_2026-09-02_1200",
                          "42.70_23.30_2026-09-03_1200",
                          "42.70_23.30_2026-09-04_1200"])
        fh.main(self._args(tmp_path))
        out = capsys.readouterr().out
        assert "=== Recurring on 4+ separate days" in out
        assert "detection(s), 4/4 days" in out

    def test_suppressed_hits_share_counted(self, tmp_path, capsys):
        seen = tmp_path / "seen.json"
        write_json(seen, ["42.7155_24.1708_2026-09-01_1200",
                          "42.7155_24.1708_2026-09-02_1200"])
        zones = tmp_path / "zones.json"
        write_json(zones, [{"name": "Pirdop", "lat": 42.7155, "lon": 24.1708,
                            "radius_km": 0.6}])
        fh.main(self._args(tmp_path, "--all"))
        out = capsys.readouterr().out
        assert "fall inside existing zones" in out