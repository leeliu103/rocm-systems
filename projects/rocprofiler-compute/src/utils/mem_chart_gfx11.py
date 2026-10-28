# Copyright (c) Advanced Micro Devices, Inc.
# SPDX-License-Identifier:  MIT

"""RDNA 3.5 (gfx115x) and RDNA 4 (gfx1201) memory chart renderer.

Hierarchy (GCEA = Graphics Core Efficiency Arbiter):
  Kernel -> GL0 (TCP) / SQC -> GL1 -> GL2 -> GCEA -> System Memory
         -> LDS (on-CU, no GL1 connection)

Metric names must match gfx115x/0300_memory_chart.yaml and
gfx1201/0300_memory_chart.yaml. gfx1201 has no GL1 cache (GL1C is only a
request interface), reports request rates and raw LDS cycle counters where
gfx115x reports bytes, and has no GCEA/SysArb or DRAM byte-bandwidth metrics.
Its chart ends with the GL2-fabric edges (GL2C/EA interface request bytes)
into a Fabric panel.
"""

import argparse
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple, Union

from rich.align import VerticalAlignMethod
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from utils.mem_chart_common import (
    COLORS,
    build_arch_notes,
    build_bw_edges,
    build_ip_block,
    build_kernel_panel,
    build_legend,
    colored,
    format_edge,
    format_scientific,
    format_value,
    make_arrows,
    mem_chart_cli_main,
    metric_line,
    pad_to,
    progress_bar,
    render_chart_to_string,
    safe_float_sum,
    stack_metrics,
)
from utils.utils_common import is_gfx1201

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Keys = metric: names under each metric_table in
# analysis_configs/gfx115x/0300_memory_chart.yaml (tables 301-309), in panel order.
# Commented-out YAML metrics (e.g. TCP Atomic) are omitted. gfx1201 YAML names
# outside this list are passed through by normalize_mem_chart_metrics.
_MEM_CHART_DEFAULT_ROWS: tuple[tuple[str, Union[int, float]], ...] = (
    # Table 301: Instruction Cache
    ("ICache Requests", 450),
    ("ICache Hit Rate", 98.5),
    ("ICache-GL1 Read Bandwidth", 57.6e9),
    # Table 302: Scalar Data Cache
    ("DCache Requests", 225),
    ("DCache Hit Rate", 95.3),
    ("DCache-GL1 Read Bandwidth", 28.8e9),
    # Table 303: TCP Cache (GL0 Vector Cache)
    ("TCP Read Requests", 875_000),
    ("TCP Write Requests", 375_000),
    ("GL0 Cache Hit Rate (TCP Cache)", 88.0),
    ("GL0 Cache BW (TCP Cache)", 80e9),
    # Table 304: LDS
    ("LDS Req", 125_000),
    ("LDS Utilization", 62.0),
    ("LDS Bank Conflict Rate", 4.0),
    ("LDS Estimated Bandwidth", 256e9),
    # Table 305: TCP-GL1 Interface
    ("TCP-GL1 Read Bandwidth", 96e9),
    ("TCP-GL1 Write Bandwidth", 32e9),
    # Table 306: GL1 Cache (L1)
    ("GL1 Cache Utilization", 65.2),
    ("GL1 Cache Hit Rate", 85.0),
    ("GL1 Cache Stall GL2 Backpressure", 8.5),
    # Table 307: GL1-GL2 Interface
    ("GL1-GL2 Read Bandwidth", 48e9),
    ("GL1-GL2 Write Bandwidth", 16e9),
    # Table 308: GL2 Cache (L2)
    ("GL2 Cache Utilization", 74.2),
    ("GL2 Cache Hit Rate", 82.5),
    ("GL2-Fabric Read BW", 64e9),
    ("GL2-Fabric Write BW", 24e9),
    # Table 309: Graphics Core Efficiency Arbiter (GCEA) to System Memory
    ("SysArb Utilization", 52.3),
    ("SysArb Stall Rate", 12.4),
    ("DRAM Read Bandwidth", 100e9),
    ("DRAM Write Bandwidth", 60e9),
)

MEM_CHART_PANEL_METRIC_KEYS: tuple[str, ...] = tuple(
    k for k, _ in _MEM_CHART_DEFAULT_ROWS
)

DEFAULT_SAMPLE_METRICS: dict[str, Union[int, float]] = dict(_MEM_CHART_DEFAULT_ROWS)

# Short internal key -> YAML metric name, per architecture.
_SHARED_METRIC_NAMES: Dict[str, str] = {
    "icache_req": "ICache Requests",
    "icache_hit": "ICache Hit Rate",
    "dcache_req": "DCache Requests",
    "dcache_hit": "DCache Hit Rate",
    "tcp_read_req": "TCP Read Requests",
    "tcp_write_req": "TCP Write Requests",
    "tcp_hit": "GL0 Cache Hit Rate (TCP Cache)",
    "tcp_gl1_read_bw": "TCP-GL1 Read Bandwidth",
    "gl1_gl2_read_bw": "GL1-GL2 Read Bandwidth",
    "gl1_gl2_write_bw": "GL1-GL2 Write Bandwidth",
    "gl2c_util": "GL2 Cache Utilization",
    "gl2c_hit": "GL2 Cache Hit Rate",
    "gl2_fabric_read_bw": "GL2-Fabric Read BW",
    "gl2_fabric_write_bw": "GL2-Fabric Write BW",
}

_GFX115X_METRIC_NAMES: Dict[str, str] = {
    **_SHARED_METRIC_NAMES,
    "icache_gl1_bw": "ICache-GL1 Read Bandwidth",
    "dcache_gl1_bw": "DCache-GL1 Read Bandwidth",
    "tcp_bw": "GL0 Cache BW (TCP Cache)",
    "lds_req": "LDS Req",
    "lds_util": "LDS Utilization",
    "lds_bw": "LDS Estimated Bandwidth",
    "lds_bank_conflict": "LDS Bank Conflict Rate",
    "tcp_gl1_write_bw": "TCP-GL1 Write Bandwidth",
    "gl1c_util": "GL1 Cache Utilization",
    "gl1c_hit": "GL1 Cache Hit Rate",
    "gl1c_stall_gl2": "GL1 Cache Stall GL2 Backpressure",
    "sysarb_util": "SysArb Utilization",
    "sysarb_stall": "SysArb Stall Rate",
    "dram_read_bw": "DRAM Read Bandwidth",
    "dram_write_bw": "DRAM Write Bandwidth",
}

_GFX1201_METRIC_NAMES: Dict[str, str] = {
    **_SHARED_METRIC_NAMES,
    "icache_gl1_req": "ICache-GL1 Request Rate",
    "dcache_gl1_req": "DCache-GL1 Request Rate",
    "tcp_req_rate": "GL0 Request Rate (TCP Cache)",
    "lds_req": "LDS Instructions",
    "lds_idx_cycles": "LDS Indexed-active Cycles",
    "lds_conflict_cycles": "LDS Bank Conflict Cycles",
    "lds_bank_conflict": "LDS Bank Conflict Cycle Ratio",
    "tcp_gl1_write_req": "TCP-GL1 Write Request Rate",
    "gl1c_util": "GL1C Interface Busy",
    "gl1c_stall_gl2": "GL1C Request Credit Stall",
}

# gfx1201 CLI preview sample: shared names reuse the gfx115x values; keys are
# metric: names in analysis_configs/gfx1201/0300_memory_chart.yaml.
DEFAULT_SAMPLE_METRICS_GFX1201: Dict[str, Union[int, float]] = {
    **{name: DEFAULT_SAMPLE_METRICS[name] for name in _SHARED_METRIC_NAMES.values()},
    "ICache-GL1 Request Rate": 9.0e8,
    "DCache-GL1 Request Rate": 4.5e8,
    "GL0 Request Rate (TCP Cache)": 1.25e9,
    "LDS Instructions": 125_000,
    "LDS Indexed-active Cycles": 480_000,
    "LDS Bank Conflict Cycles": 19_200,
    "LDS Bank Conflict Cycle Ratio": 4.0,
    "TCP-GL1 Write Request Rate": 2.5e8,
    "GL1C Interface Busy": 65.2,
    "GL1C Request Credit Stall": 8.5,
}


@dataclass(frozen=True)
class _ChartLayout:
    """Per-architecture metric names, labels, and panel choices."""

    metric_names: Dict[str, str]
    lds_edge_label: str
    lds_conflict_label: str
    gl1_title: str
    gl1_stall_label: str
    show_gl1_hit: bool
    # Request rates and raw LDS cycle counters instead of byte bandwidths
    request_rates: bool
    # GCEA and DRAM panels; without them the chart ends at a Fabric panel
    show_gcea: bool
    memory_scope_label: str


_GFX115X_LAYOUT = _ChartLayout(
    metric_names=_GFX115X_METRIC_NAMES,
    lds_edge_label="Request",
    lds_conflict_label="Bank Conflict",
    gl1_title="GL1 Cache",
    gl1_stall_label="GL2 Stall",
    show_gl1_hit=True,
    request_rates=False,
    show_gcea=True,
    memory_scope_label="System Memory",
)

_GFX1201_LAYOUT = _ChartLayout(
    metric_names=_GFX1201_METRIC_NAMES,
    lds_edge_label="Instructions",
    lds_conflict_label="Cycle ratio",
    gl1_title="GL1C",
    gl1_stall_label="Req Credits",
    show_gl1_hit=False,
    request_rates=True,
    show_gcea=False,
    memory_scope_label="Fabric",
)

_EDGE_LABEL_W = 11  # fits longest kernel-edge label ("ICache Read")

# Panel dimensions
_L0_PANEL_W = 20  # LDS/TCP/SQC: fits hit% + bar + BW line
_L0_PANEL_H = 10  # each L0 panel height
_CACHE_PANEL_W = 16  # GL1/GL2/GCEA/DRAM/Fabric: fewer metrics, narrower
_TOTAL_H = 3 * _L0_PANEL_H  # all columns align to this height

# Arrow lengths
_STD_ARROW_LEN = 8  # edge arrows between cache panels
_KERNEL_EDGE_W = 20  # wider Kernel->L0 arrows (longer labels)
_EDGE_COLS = (5, 7, 9)  # grid column indices that use vertical="middle"

# Console dimensions
_CONSOLE_WIDTH = 200  # RDNA narrower than CDNA's 240


# ---------------------------------------------------------------------------
# Public API: heading, normalization, sample data
# ---------------------------------------------------------------------------


def normalize_mem_chart_metrics(metric_dict: dict[str, Any]) -> dict[str, Any]:
    """Flat map of chart metric names; unknown keys dropped.

    Every gfx115x panel name is present in panel order (None when missing).
    gfx1201 YAML names are kept when supplied, so either architecture's
    metrics survive normalization.
    """
    metrics = {k: metric_dict.get(k) for k in MEM_CHART_PANEL_METRIC_KEYS}
    metrics.update({
        name: metric_dict[name]
        for name in _GFX1201_METRIC_NAMES.values()
        if name in metric_dict
    })
    return metrics


def get_sample_metrics(gpu_arch: Optional[str] = None) -> dict[str, Any]:
    """Return sample metrics (flat panel order) for testing or demos."""
    sample = (
        DEFAULT_SAMPLE_METRICS_GFX1201
        if is_gfx1201(gpu_arch)
        else DEFAULT_SAMPLE_METRICS
    )
    return normalize_mem_chart_metrics(dict(sample))


# ---------------------------------------------------------------------------
# Diagram construction
# ---------------------------------------------------------------------------


def _scope_bar_section(label: str, span: int) -> str:
    """One labeled section of the scope rule."""
    markup_label = f" [dim]{label}[/dim] "
    visible_len = Text.from_markup(markup_label).cell_len
    inner = max(visible_len, span - 1)
    remaining = inner - visible_len
    left = remaining // 2
    return "|" + "-" * left + markup_label + "-" * (remaining - left)


def _scope_bar(gpu_span: int, memory_span: int, memory_label: str) -> str:
    """Horizontal scope rule sized from the assembled layout."""
    return (
        _scope_bar_section("GPU", gpu_span)
        + _scope_bar_section(memory_label, memory_span - 1)
        + "|"
    )


def _extract_metrics(
    metric_dict: dict[str, Any], layout: _ChartLayout
) -> dict[str, Any]:
    """Map the layout's YAML metric names to short internal keys."""
    return {key: metric_dict.get(name) for key, name in layout.metric_names.items()}


def _build_kernel_and_l0(
    metrics: dict[str, Any],
    kernel_arrows: dict[str, str],
    std_arrows: dict[str, str],
    layout: _ChartLayout,
) -> tuple[Panel, Text, Group, Text]:
    """Kernel panel, kernel edges, L0 stack, and GL1 edges."""

    color_read = COLORS["read"]
    color_write = COLORS["write"]

    kernel_panel = build_kernel_panel(height=_TOTAL_H, padding_lines=11)

    kernel_arrow_left = kernel_arrows["left"]
    kernel_arrow_right = kernel_arrows["right"]

    # LDS edge: instruction count only (not data movement)
    lds_edge_zone = [
        f"     [white]{layout.lds_edge_label}[/white]",
        format_edge("LDS", metrics["lds_req"], _EDGE_LABEL_W),
        kernel_arrows["plain"],
    ]
    tcp_edge_zone = [
        colored(
            format_edge("Read", metrics["tcp_read_req"], _EDGE_LABEL_W),
            color_read,
        ),
        colored(kernel_arrow_left, color_read),
        colored(
            format_edge("Write", metrics["tcp_write_req"], _EDGE_LABEL_W),
            color_write,
        ),
        colored(kernel_arrow_right, color_write),
    ]
    sqc_edge_zone = [
        colored(
            format_edge("ICache Read", metrics["icache_req"], _EDGE_LABEL_W),
            color_read,
        ),
        colored(kernel_arrow_left, color_read),
        colored(
            format_edge("DCache Read", metrics["dcache_req"], _EDGE_LABEL_W),
            color_read,
        ),
        colored(kernel_arrow_left, color_read),
    ]
    kernel_edges_lines = (
        pad_to(lds_edge_zone, _L0_PANEL_H)
        + pad_to(tcp_edge_zone, _L0_PANEL_H)
        + pad_to(sqc_edge_zone, _L0_PANEL_H)
    )
    kernel_edges_text = Text.from_markup("\n".join(kernel_edges_lines))

    # LDS panel
    if layout.request_rates:
        lds_util_line = "Indexed cycles\n" + colored(
            format_scientific(metrics["lds_idx_cycles"]), COLORS["util"]
        )
        lds_bw_line = "Conflict cycles\n" + colored(
            format_scientific(metrics["lds_conflict_cycles"]), COLORS["stall"]
        )
    else:
        lds_util_line = (
            f"{metric_line('Util', metrics['lds_util'], '%', COLORS['util'])}\n"
            f"[dim]{progress_bar(metrics['lds_util'])}[/dim]"
            if metrics["lds_util"] is not None
            else ""
        )
        lds_bw_line = (
            metric_line("BW", metrics["lds_bw"], "Bytes/s", COLORS["bw"])
            if metrics["lds_bw"] is not None
            else ""
        )
    lds_conflict_line = (
        metric_line(
            layout.lds_conflict_label,
            metrics["lds_bank_conflict"],
            "%",
            COLORS["stall"],
        )
        if metrics["lds_bank_conflict"] is not None
        else ""
    )
    lds_panel = build_ip_block(
        "LDS",
        _L0_PANEL_W,
        _L0_PANEL_H,
        stack_metrics(lds_util_line, lds_bw_line, lds_conflict_line),
    )

    # GL0 (TCP Cache) panel
    if layout.request_rates:
        tcp_bw_line = "Req/s " + colored(
            format_scientific(metrics["tcp_req_rate"]), COLORS["bw"]
        )
    else:
        tcp_bw_line = (
            metric_line("BW", metrics["tcp_bw"], "Bytes/s", COLORS["bw"])
            if metrics["tcp_bw"] is not None
            else ""
        )
    tcp_panel = build_ip_block(
        "GL0 (TCP Cache)",
        _L0_PANEL_W,
        _L0_PANEL_H,
        stack_metrics(
            f"{metric_line('Hit Rate', metrics['tcp_hit'], '%', COLORS['hit'])}\n"
            f"[dim]{progress_bar(metrics['tcp_hit'])}[/dim]",
            tcp_bw_line,
        ),
    )

    # SQC panel
    sqc_panel = build_ip_block(
        "SQC",
        _L0_PANEL_W,
        _L0_PANEL_H,
        stack_metrics(
            metric_line("ICache Hit Rate", metrics["icache_hit"], "%", COLORS["hit"])
            + f"\n[dim]{progress_bar(metrics['icache_hit'])}[/dim]",
            metric_line("DCache Hit Rate", metrics["dcache_hit"], "%", COLORS["hit"])
            + f"\n[dim]{progress_bar(metrics['dcache_hit'])}[/dim]",
        ),
    )

    # Stack LDS, TCP, SQC vertically
    l0_stack = Group(lds_panel, tcp_panel, sqc_panel)

    # GL1 edges: TCP and SQC connect; LDS does NOT
    if layout.request_rates:
        write_label = "Write Rate"
        write_value = f"{format_scientific(metrics['tcp_gl1_write_req'])} req/s"
        sqc_label = "Read Rate"
        sqc_requests = safe_float_sum(
            metrics["icache_gl1_req"], metrics["dcache_gl1_req"]
        )
        sqc_value = f"{format_scientific(sqc_requests)} req/s"
    else:
        write_label = "Write BW"
        write_value = format_value(metrics["tcp_gl1_write_bw"], "Bytes/s", 1)
        sqc_label = "Read BW"
        sqc_read_bw = safe_float_sum(metrics["icache_gl1_bw"], metrics["dcache_gl1_bw"])
        sqc_value = format_value(sqc_read_bw, "Bytes/s", 1)
    tcp_gl1_content = stack_metrics(
        "\n".join([
            colored("Read BW", color_read),
            colored(format_value(metrics["tcp_gl1_read_bw"], "Bytes/s", 1), color_read),
            colored(std_arrows["left"], color_read),
        ]),
        "\n".join([
            colored(write_label, color_write),
            colored(write_value, color_write),
            colored(std_arrows["right"], color_write),
        ]),
    )
    tcp_gl1_zone = tcp_gl1_content.split("\n")
    sqc_gl1_zone = [
        colored(sqc_label, color_read),
        colored(sqc_value, color_read),
        colored(std_arrows["left"], color_read),
    ]
    gl1_edges_lines = (
        pad_to([], _L0_PANEL_H)
        + pad_to(tcp_gl1_zone, _L0_PANEL_H)
        + pad_to(sqc_gl1_zone, _L0_PANEL_H)
    )
    gl1_edges_text = Text.from_markup("\n".join(gl1_edges_lines))

    return kernel_panel, kernel_edges_text, l0_stack, gl1_edges_text


def _build_cache_columns(
    metrics: dict[str, Any],
    std_arrows: dict[str, str],
    layout: _ChartLayout,
) -> tuple[Panel, Text, Panel]:
    """GL1 panel, GL1-GL2 edges, GL2 panel."""

    gl1_hit = (
        f"{metric_line('Hit Rate', metrics['gl1c_hit'], '%', COLORS['hit'])}\n"
        f"[dim]{progress_bar(metrics['gl1c_hit'])}[/dim]"
        if layout.show_gl1_hit
        else ""
    )
    gl1_content = stack_metrics(
        f"{metric_line('Util', metrics['gl1c_util'], '%', COLORS['util'])}\n"
        f"[dim]{progress_bar(metrics['gl1c_util'])}[/dim]",
        gl1_hit,
        metric_line(
            layout.gl1_stall_label, metrics["gl1c_stall_gl2"], "%", COLORS["stall"]
        ),
    )
    gl1_panel = build_ip_block(layout.gl1_title, _CACHE_PANEL_W, _TOTAL_H, gl1_content)

    gl1_gl2_edges_text = build_bw_edges(
        [
            ("Read BW", metrics["gl1_gl2_read_bw"], "left", COLORS["read"]),
            ("Write BW", metrics["gl1_gl2_write_bw"], "right", COLORS["write"]),
        ],
        std_arrows,
    )

    gl2_content = stack_metrics(
        f"{metric_line('Util', metrics['gl2c_util'], '%', COLORS['util'])}\n"
        f"[dim]{progress_bar(metrics['gl2c_util'])}[/dim]",
        f"{metric_line('Hit Rate', metrics['gl2c_hit'], '%', COLORS['hit'])}\n"
        f"[dim]{progress_bar(metrics['gl2c_hit'])}[/dim]",
    )
    gl2_panel = build_ip_block("GL2 Cache", _CACHE_PANEL_W, _TOTAL_H, gl2_content)

    return gl1_panel, gl1_gl2_edges_text, gl2_panel


def _build_memory_columns(
    metrics: dict[str, Any],
    std_arrows: dict[str, str],
    layout: _ChartLayout,
) -> Tuple[List[RenderableType], List[RenderableType]]:
    """Columns right of GL2, split into GPU-side and memory-side columns."""
    gl2_fabric_edges_text = build_bw_edges(
        [
            ("Read BW", metrics["gl2_fabric_read_bw"], "left", COLORS["read"]),
            ("Write BW", metrics["gl2_fabric_write_bw"], "right", COLORS["write"]),
        ],
        std_arrows,
    )
    if not layout.show_gcea:
        return [gl2_fabric_edges_text], [_build_fabric_panel()]

    gcea_content = stack_metrics(
        f"{metric_line('SysArb Util', metrics['sysarb_util'], '%', COLORS['util'])}\n"
        f"[dim]{progress_bar(metrics['sysarb_util'])}[/dim]",
        metric_line("SysArb Stall", metrics["sysarb_stall"], "%", COLORS["stall"]),
    )
    gcea_panel = build_ip_block("GCEA", _CACHE_PANEL_W, _TOTAL_H, gcea_content)

    dram_edges_text = build_bw_edges(
        [
            ("Read BW", metrics["dram_read_bw"], "left", COLORS["read"]),
            ("Write BW", metrics["dram_write_bw"], "right", COLORS["write"]),
        ],
        std_arrows,
    )

    total_bw = safe_float_sum(metrics["dram_read_bw"], metrics["dram_write_bw"])
    total = format_value(total_bw, "Bytes/s", 1)
    dram_content = stack_metrics(
        "[dim]DDR5/LPDDR5[/dim]",
        f"Total: [bold bright_green]{total}[/bold bright_green]",
    )
    dram_panel = build_ip_block("DRAM", _CACHE_PANEL_W, _TOTAL_H, dram_content)

    return [gl2_fabric_edges_text, gcea_panel], [dram_edges_text, dram_panel]


def _build_fabric_panel() -> Panel:
    """Terminal panel for GL2 traffic when no GCEA or DRAM BW metrics exist.

    gfx1201 has no GCEA/SysArb metrics and no DRAM byte-bandwidth metrics; its
    GL2-fabric edges are GL2C/EA interface request bytes.
    """
    return build_ip_block(
        "Fabric",
        _CACHE_PANEL_W,
        _TOTAL_H,
        "[dim]DRAM traffic not measured[/dim]",
    )


# ---------------------------------------------------------------------------
# Main diagram assembly
# ---------------------------------------------------------------------------


def create_mem_chart_diagram(
    metric_dict: dict[str, Any],
    console: Console,
    show_debug: bool = False,
    chart_title: str = "",
    gpu_arch: Optional[str] = None,
) -> None:
    """Render the RDNA memory diagram to the console."""
    layout = _GFX1201_LAYOUT if is_gfx1201(gpu_arch) else _GFX115X_LAYOUT
    metrics = _extract_metrics(metric_dict, layout)

    std_arrows = make_arrows(_STD_ARROW_LEN)
    kernel_arrows = make_arrows(_KERNEL_EDGE_W)

    # Build layout columns
    kernel_panel, kernel_edges, l0_stack, gl1_edges = _build_kernel_and_l0(
        metrics, kernel_arrows, std_arrows, layout
    )
    gl1_panel, gl1_gl2_edges, gl2_panel = _build_cache_columns(
        metrics, std_arrows, layout
    )
    gpu_columns, memory_columns = _build_memory_columns(metrics, std_arrows, layout)
    columns = [
        kernel_panel,
        kernel_edges,
        l0_stack,
        gl1_edges,
        gl1_panel,
        gl1_gl2_edges,
        gl2_panel,
        *gpu_columns,
        *memory_columns,
    ]

    # Assemble the grid (edge columns use vertical middle alignment)
    main_layout = Table.grid(padding=0)
    for i in range(len(columns)):
        vert: VerticalAlignMethod = "middle" if i in _EDGE_COLS else "top"
        main_layout.add_column(vertical=vert)

    main_layout.add_row(*columns)

    total = console.measure(main_layout).maximum
    memory_span = sum(console.measure(column).maximum for column in memory_columns)

    sections: list[Union[str, RenderableType]] = []
    if chart_title:
        sections.append(f"[bold]{chart_title}[/bold]")
    sections.append(
        _scope_bar(total - memory_span, memory_span, layout.memory_scope_label)
    )
    sections.append("")
    sections.append(main_layout)
    sections.append("")
    sections.append(build_legend(include_atomic=False, include_stall=True))

    if show_debug:
        sections.append("")
        sections.append(
            build_arch_notes([
                ("TCP (Texture Cache Pipe)", "L0 vector cache for VMEM operations"),
                ("LDS (Local Data Share)", "On-CU scratchpad, NO GL1 Cache connection"),
                ("SQC (Sequencer Cache)", "ICache + DCache for scalar operations"),
            ])
        )

    console.print(Group(*sections))


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def plot_mem_chart(
    metric_dict: dict[str, Any],
    *,
    chart_title: str,
    gpu_arch: Optional[str] = None,
) -> str:
    """Render the RDNA memory chart and return as a string."""
    return render_chart_to_string(
        create_mem_chart_diagram,
        metric_dict,
        normalize_mem_chart_metrics,
        console_width=_CONSOLE_WIDTH,
        chart_title=chart_title,
        gpu_arch=gpu_arch,
    )


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the RDNA 3.5 / RDNA 4 memory chart."""
    # Peek at --arch so the preview sample (no --data) matches the arch layout
    arch_parser = argparse.ArgumentParser(add_help=False)
    arch_parser.add_argument("--arch", default=None)
    arch_args, _ = arch_parser.parse_known_args()
    mem_chart_cli_main(
        "RDNA 3.5 / RDNA 4 Memory Chart - CLI Visualization",
        create_mem_chart_diagram,
        normalize_mem_chart_metrics,
        get_sample_metrics(arch_args.arch),
        console_width=_CONSOLE_WIDTH,
    )


if __name__ == "__main__":
    main()
