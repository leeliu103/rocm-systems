#!/usr/bin/env python3
# Copyright (c) Advanced Micro Devices, Inc.
# SPDX-License-Identifier:  MIT

"""Tests for per-arch template resolution in verify_against_config_template.py
and the gfx1201 section mapping in metric_description_manager.py."""

import sys
from pathlib import Path
from typing import Dict, Tuple

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from config_management import metric_description_manager as mdm  # noqa: E402
from config_management import verify_against_config_template as vact  # noqa: E402

FAMILY_LABELS = ("gfx9", "gfx11", "gfx12")


def _write_template(path: Path, title: str = "Top Stats") -> Path:
    path.write_text(
        "panels:\n"
        "  - file: top_stats.yaml\n"
        "    panel_id: 100\n"
        f"    panel_title: {title}\n"
        "    data_sources:\n"
        "      - type: metric_table\n"
        "        id: 101\n"
        "        title: Top Kernels\n"
    )
    return path


def _write_arch(configs_dir: Path, arch: str, title: str = "Top Stats") -> None:
    arch_dir = configs_dir / arch
    arch_dir.mkdir()
    (arch_dir / "0100_top_stats.yaml").write_text(
        "Panel Config:\n"
        "  id: 100\n"
        f"  title: {title}\n"
        "  data source:\n"
        "    - metric_table:\n"
        "        id: 101\n"
        "        title: Top Kernels\n"
        "  metrics_description: {}\n"
    )


def _family_templates(configs_dir: Path) -> Dict[str, Path]:
    return {
        label: configs_dir / f"{label}_config_template.yaml" for label in FAMILY_LABELS
    }


def _run_main(monkeypatch, capsys, *argv: str) -> Tuple[int, str]:
    monkeypatch.setattr(sys, "argv", ["verify_against_config_template.py", *argv])
    code = 0
    try:
        vact.main()
    except SystemExit as exc:
        code = exc.code or 0
    return code, capsys.readouterr().out


@pytest.mark.parametrize(
    ("arch", "label"),
    [
        ("gfx1201", "gfx1201"),
        ("gfx1250", "gfx12"),
        ("gfx115x", "gfx11"),
        ("gfx1151", "gfx11"),
        ("gfx942", "gfx9"),
    ],
)
def test_resolve_template(tmp_path, arch, label):
    _write_template(tmp_path / "gfx1201_config_template.yaml")
    family = _family_templates(tmp_path)
    expected = (
        tmp_path / "gfx1201_config_template.yaml"
        if label == "gfx1201"
        else family[label]
    )
    assert vact.resolve_template(arch, tmp_path, family) == (label, expected)


def test_resolve_template_known_override_without_file(tmp_path):
    """gfx1201 must not silently fall back to the gfx12 family template."""
    family = _family_templates(tmp_path)
    assert vact.resolve_template("gfx1201", tmp_path, family) == (
        "gfx1201",
        tmp_path / "gfx1201_config_template.yaml",
    )


def test_main_passes_with_all_templates(tmp_path, monkeypatch, capsys):
    for label in ("gfx9", "gfx11", "gfx12", "gfx1201"):
        _write_template(tmp_path / f"{label}_config_template.yaml")
    for arch in ("gfx942", "gfx115x", "gfx1250", "gfx1201"):
        _write_arch(tmp_path, arch)
    code, out = _run_main(monkeypatch, capsys, str(tmp_path))
    assert code == 0, out
    assert "Validation PASSED" in out
    gfx1201_template = tmp_path / "gfx1201_config_template.yaml"
    assert f"Using gfx1201 template (1 panels): {gfx1201_template}" in out


def test_main_honors_gfx12_template_override(tmp_path, monkeypatch, capsys):
    _write_template(tmp_path / "gfx12_config_template.yaml", title="Wrong Title")
    override = _write_template(tmp_path / "custom_gfx12.yaml")
    _write_arch(tmp_path, "gfx1250")

    code, out = _run_main(monkeypatch, capsys, str(tmp_path))
    assert code == 1
    assert "Panel title mismatch" in out

    code, out = _run_main(
        monkeypatch, capsys, str(tmp_path), "--gfx12-template", str(override)
    )
    assert code == 0, out
    assert f"Using gfx12 template (1 panels): {override}" in out


def test_main_missing_family_template_fails(tmp_path, monkeypatch, capsys):
    _write_arch(tmp_path, "gfx1250")
    code, out = _run_main(monkeypatch, capsys, str(tmp_path))
    assert code == 1
    assert "gfx1250 architecture present but gfx12 template missing" in out
    assert str(tmp_path / "gfx12_config_template.yaml") in out


def test_main_missing_per_arch_template_fails(tmp_path, monkeypatch, capsys):
    """A missing gfx1201 override is an error even if the gfx12 template exists."""
    _write_template(tmp_path / "gfx12_config_template.yaml")
    _write_arch(tmp_path, "gfx1201")
    code, out = _run_main(monkeypatch, capsys, str(tmp_path))
    assert code == 1
    assert "gfx1201 architecture present but gfx1201 template missing" in out
    assert str(tmp_path / "gfx1201_config_template.yaml") in out
    assert "Loading gfx12 template" not in out


def test_panel_id_to_section_gfx1201():
    assert mdm.panel_id_to_section("gfx1201", 1101) == "GL1C Interface Activity"
    # Ids gfx1201 does not retitle are inherited from the RDNA 3.5 layout.
    assert mdm.panel_id_to_section("gfx1201", 1301) == "GL2 Cache Performance"
    assert (
        mdm.panel_id_to_section("gfx1201", 1301)
        == mdm.RDNA35_PANEL_ID_TO_SECTION_BY_ARCH[1301]
    )
    assert mdm.panel_id_to_section("gfx1201", None) is None
    # The RDNA 3.5 mapping itself keeps its GL1 cache title.
    assert mdm.panel_id_to_section("gfx115x", 1101) == "GL1 Cache Utilization"
