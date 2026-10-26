# Copyright (c) Advanced Micro Devices, Inc.
# SPDX-License-Identifier:  MIT

"""Host-only checks for the RDNA4 SoC's counter collection settings."""

from argparse import Namespace
from pathlib import Path

import common
import pytest
import yaml

from rocprof_compute_soc.soc_base import flat_counters_in_perfmon_file
from rocprof_compute_soc.soc_gfx1201 import gfx1201_soc
from utils.specs import MachineSpecsRDNA4
from utils.utils_common import parse_pmc_perf, parse_sets_yaml
from utils.utils_counter_defs import parse_counters_text


@pytest.fixture
def soc():
    args = Namespace(
        config_dir=Path(common.SRC) / "rocprof_compute_soc" / "analysis_configs",
        filter_blocks=[],
        set_selected=None,
        roof_only=False,
        membw_analysis=False,
    )
    return gfx1201_soc(args, MachineSpecsRDNA4(gpu_arch="gfx1201", gpu_series="RDNA4"))


@pytest.mark.parametrize("block, capacity", [("SQ", 8), ("GL2C", 4)])
def test_counter_capacity_splits_passes(soc, block, capacity, monkeypatch):
    """One extra event spills to another pass at the Navi48 PMC limit."""
    assert soc.get_arch() == "gfx1201"
    assert (
        soc._mspec.l2_banks,
        soc._mspec.lds_banks_per_cu,
        soc._mspec.pipes_per_gpu,
    ) == (32, 32, 2)

    monkeypatch.setattr(soc, "_same_bucket_priority_metric_ids", lambda: ())
    counters = {f"{block}_EVENT_{index}" for index in range(capacity + 1)}
    files, _, _ = soc._allocate_perfmon_counter_files(counters)

    assert sorted(len(flat_counters_in_perfmon_file(file)) for file in files) == [
        1,
        capacity,
    ]
    assert {
        ctr for file in files for ctr in flat_counters_in_perfmon_file(file)
    } == counters


def test_default_counters_resolve_to_shipped_sdk_definitions(soc):
    """Every default counter and derived dependency has an RDNA4 definition."""
    config_path = (
        Path(common.SRC) / "rocprof_compute_soc" / "profile_configs" / "sdk_config.yaml"
    )
    sdk_config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    definitions = {
        counter["name"]: definition
        for counter in sdk_config["rocprofiler-sdk"]["counters"]
        for definition in counter["definitions"]
        if "gfx1201" in definition["architectures"]
    }
    pending, _ = soc.detect_counters()
    assert pending
    resolved = set()
    while pending:
        name = pending.pop()
        assert name in definitions, f"No gfx1201 definition for {name}"
        definition = definitions[name]
        resolved.add(name)
        if "expression" in definition:
            dependencies, _ = parse_counters_text(definition["expression"])
            assert dependencies, f"No counter dependencies found for {name}"
            pending.update(dependencies - resolved)
        else:
            assert definition["block"] and "event" in definition


def test_gpu_busy_operands_share_profile_job(
    soc: gfx1201_soc, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GPU Busy uses numerator and denominator from the same kernel replay."""
    counters, _ = soc.detect_counters()
    soc.get_args().output_directory = tmp_path
    monkeypatch.setattr(soc, "get_rocprof_supported_counters", lambda: counters)

    soc.perfmon_coalesce(counters)

    jobs = [
        set(parse_pmc_perf(str(filename)))
        for filename in (tmp_path / "perfmon").glob("pmc_perf_*.yaml")
    ]
    assert any({"GRBM_GUI_ACTIVE", "GRBM_COUNT"}.issubset(job) for job in jobs)


def test_shipped_sets_fit_one_pass_without_losing_counters(soc):
    """Exercise selection and allocation together for each user-visible set."""
    sets = parse_sets_yaml("gfx1201")
    assert sets
    for set_option in sets:
        soc.get_args().set_selected = set_option
        counters, _ = soc.detect_counters()
        assert counters, set_option
        files, _, _ = soc._allocate_perfmon_counter_files(counters)
        assert len(files) == 1, set_option
        assert set(flat_counters_in_perfmon_file(files[0])) == counters, set_option
