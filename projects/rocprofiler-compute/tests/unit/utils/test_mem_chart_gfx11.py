# Copyright (c) Advanced Micro Devices, Inc.
# SPDX-License-Identifier:  MIT

"""Unit tests for the gfx11 memory-chart renderer."""

import json
import sys
from pathlib import Path
from xml.etree import ElementTree

import common
import pytest
import yaml

from utils import mem_chart_gfx11
from utils.mem_chart_common import strip_ansi

DEFAULT_TITLE = "3. Memory Chart (Normalization: per_kernel)"

ANALYSIS_CONFIG_DIR = Path(common.SRC) / "rocprof_compute_soc" / "analysis_configs"


def mem_chart_yaml_metric_names(arch: str = "gfx115x") -> list[str]:
    path = ANALYSIS_CONFIG_DIR / arch / "0300_memory_chart.yaml"
    panel = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        name
        for entry in panel["Panel Config"]["data source"]
        for name in entry["metric_table"]["metric"]
    ]


@pytest.mark.parametrize("entry", ["plot", "txt", "svg"])
@pytest.mark.parametrize(
    "metric, value, expected",
    [
        ("ICache-GL1 Request Rate", 1234567890123, "1.23e+12 req/s"),
        ("DCache-GL1 Request Rate", 1234567890123, "1.23e+12 req/s"),
        ("GL0 Request Rate (TCP Cache)", 1234567890123, "Req/s 1.23e+12"),
        ("TCP-GL1 Write Request Rate", 1234567890123, "1.23e+12 req/s"),
        ("LDS Indexed-active Cycles", 1234567890123, "1.23e+12"),
        ("LDS Bank Conflict Cycles", 1234567890123, "1.23e+12"),
        ("LDS Instructions", 1234567890123, "1.23e+12"),
        ("LDS Bank Conflict Cycle Ratio", 12.5, "12.5%"),
        ("GL1C Interface Busy", 12.5, "12.5%"),
        ("GL1C Request Credit Stall", 12.5, "12.5%"),
    ],
)
def test_gfx1201_yaml_values_reach_plot_and_exports(
    entry, metric, value, expected, monkeypatch, tmp_path
):
    """New RDNA4 fields and aliases survive both shared normalization paths."""
    metrics = dict.fromkeys(mem_chart_yaml_metric_names("gfx1201"))
    assert metric in metrics
    metrics[metric] = value
    if entry == "plot":
        output = mem_chart_gfx11.plot_mem_chart(
            metrics, chart_title=DEFAULT_TITLE, gpu_arch="gfx1201"
        )
    else:
        data_path = tmp_path / "metrics.json"
        output_path = tmp_path / f"chart.{entry}"
        data_path.write_text(json.dumps(metrics), encoding="utf-8")
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "mem_chart_gfx11",
                "--data",
                str(data_path),
                "--arch",
                "gfx1201",
                f"--{entry}",
                str(output_path),
            ],
        )
        mem_chart_gfx11.main()
        output = output_path.read_text(encoding="utf-8")
        if entry == "svg":
            output = "".join(ElementTree.fromstring(output).itertext()).replace(
                "\xa0", " "
            )
    assert expected in strip_ansi(output)


@pytest.mark.parametrize("value, expected", [(0, "0 req/s"), (None, "N/A req/s")])
def test_gfx1201_request_rates_preserve_zero_and_missing(value, expected):
    metrics = dict.fromkeys(mem_chart_yaml_metric_names("gfx1201"), value)
    output = strip_ansi(
        mem_chart_gfx11.plot_mem_chart(
            metrics, chart_title=DEFAULT_TITLE, gpu_arch="gfx1201"
        )
    )
    assert output.count(expected) == 2  # TCP writes and combined SQC reads
    expected_gl0 = "Req/s 0" if value == 0 else "Req/s N/A"
    assert expected_gl0 in output
    assert "Indexed cycles" in output and "Conflict cycles" in output


# =============================================================================
# gfx11 get_sample_metrics
# =============================================================================


class TestGetSampleMetrics:
    def test_returns_copy(self):
        first = mem_chart_gfx11.get_sample_metrics()
        second = mem_chart_gfx11.get_sample_metrics()
        first["TCP-GL1 Read Bandwidth"] = 0
        assert second["TCP-GL1 Read Bandwidth"] != 0


# =============================================================================
# gfx11 plot_mem_chart
# =============================================================================


class TestPlotMemChartGfx11:
    def test_full_chart(self):
        result = mem_chart_gfx11.plot_mem_chart(
            mem_chart_gfx11.get_sample_metrics(), chart_title=DEFAULT_TITLE
        )
        clean = strip_ansi(result)
        assert len(result) > 100
        assert "3. Memory Chart" in clean
        assert "GPU" in clean and "System Memory" in clean

    @pytest.mark.parametrize("block", ["TCP", "GL1 Cache", "GL2 Cache", "GCEA", "DRAM"])
    def test_contains_arch_element(self, block):
        result = mem_chart_gfx11.plot_mem_chart(
            mem_chart_gfx11.get_sample_metrics(), chart_title=DEFAULT_TITLE
        )
        assert block in result

    def test_normalize_drops_unknown_fills_missing(self):
        raw = {"GL0 Cache Hit Rate (TCP Cache)": 1.0, "noise": 99}
        norm = mem_chart_gfx11.normalize_mem_chart_metrics(raw)
        assert norm["GL0 Cache Hit Rate (TCP Cache)"] == 1.0
        assert "noise" not in norm
        assert norm["ICache Requests"] is None

    def test_gfx1201_interface_labels_and_values(self) -> None:
        metrics = {
            "LDS Instructions": 1234,
            "LDS Bank Conflict Cycle Ratio": 12.5,
            "GL1C Interface Busy": 37.5,
            "GL1C Request Credit Stall": 6.25,
        }
        output = strip_ansi(
            mem_chart_gfx11.plot_mem_chart(
                metrics, chart_title=DEFAULT_TITLE, gpu_arch="gfx1201"
            )
        )
        assert "GL1C" in output
        assert "GL1 Cache" not in output
        assert "Device Memory" in output
        assert "GDDR6" in output
        assert "DDR5" not in output
        assert "Instructions" in output and "1.23e+03" in output
        assert "12.5%" in output
        assert "37.5%" in output
        assert "Req Credits" in output and "6.2%" in output
        assert "N/A" in output


# =============================================================================
# gfx11 zero vs missing metrics
# =============================================================================


class TestZeroVersusMissingMetrics:
    """A measured 0 must render; an absent counter must never read as 0."""

    GUARDED_LINES = [
        ("LDS Utilization", "Util 0.0%"),
        ("LDS Estimated Bandwidth", "BW 0.0 B/s"),
        ("LDS Bank Conflict Rate", "Bank Conflict"),
        ("GL0 Cache BW (TCP Cache)", "BW 0.0 B/s"),
    ]

    @pytest.mark.parametrize(("metric", "expected"), GUARDED_LINES)
    def test_zero_valued_metric_is_displayed(self, metric, expected):
        output = strip_ansi(
            mem_chart_gfx11.plot_mem_chart({metric: 0}, chart_title=DEFAULT_TITLE)
        )
        assert expected in output

    def test_all_zero_metrics_render_no_placeholders(self):
        metrics = dict.fromkeys(mem_chart_gfx11.MEM_CHART_PANEL_METRIC_KEYS, 0)
        output = strip_ansi(
            mem_chart_gfx11.plot_mem_chart(metrics, chart_title=DEFAULT_TITLE)
        )
        assert "N/A" not in output

    def test_missing_counters_are_not_reported_as_zero(self):
        output = strip_ansi(
            mem_chart_gfx11.plot_mem_chart({}, chart_title=DEFAULT_TITLE)
        )
        assert "0.0" not in output


# =============================================================================
# gfx11 panel YAML sync
# =============================================================================


class TestMemChartPanelYamlSync:
    """gfx115x chart metric names must track the YAML on disk."""

    def test_panel_metric_keys_match_yaml_in_order(self):
        assert (
            list(mem_chart_gfx11.MEM_CHART_PANEL_METRIC_KEYS)
            == mem_chart_yaml_metric_names()
        )

    def test_every_chart_lookup_resolves_against_yaml(self):
        metrics = dict.fromkeys(mem_chart_yaml_metric_names(), 1.0)
        extracted = mem_chart_gfx11._extract_metrics(
            mem_chart_gfx11.normalize_mem_chart_metrics(metrics)
        )
        unresolved = sorted(key for key, value in extracted.items() if value is None)
        assert not unresolved, f"metric names drifted from the YAML: {unresolved}"


# =============================================================================
# gfx11 DEFAULT_SAMPLE_METRICS
# =============================================================================


class TestDefaultSampleMetrics:
    def test_keys_match_panel_keys(self):
        assert set(mem_chart_gfx11.DEFAULT_SAMPLE_METRICS) == set(
            mem_chart_gfx11.MEM_CHART_PANEL_METRIC_KEYS
        )

    def test_has_all_memory_hierarchy_levels(self):
        metrics = mem_chart_gfx11.DEFAULT_SAMPLE_METRICS
        for prefix in ("TCP", "LDS", "GL1", "GL2", "DRAM"):
            assert any(prefix in k for k in metrics), f"Missing {prefix}"


def test_chart_title_appears_as_first_line():
    chart_title = "7. Memory Chart (Normalization: per_kernel)"
    output = strip_ansi(
        mem_chart_gfx11.plot_mem_chart(
            mem_chart_gfx11.get_sample_metrics(),
            chart_title=chart_title,
        )
    )
    assert output.strip().splitlines()[0] == chart_title
    assert "3. Memory Chart" not in output
