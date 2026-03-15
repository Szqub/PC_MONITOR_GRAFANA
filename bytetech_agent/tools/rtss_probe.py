"""
Raw diagnostic tool for RTSS shared memory.

Example:
    python -m bytetech_agent.tools.rtss_probe
"""
from __future__ import annotations

import argparse
import sys
from typing import Iterable, List

from bytetech_agent.providers.rtss_provider import RtssEntryDiagnostic, RtssProbeResult, RtssSharedMemoryReader


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe RTSS shared memory mappings and dump raw app entries.")
    parser.add_argument(
        "--shared-memory-name",
        default="RTSSSharedMemoryV2",
        help="Preferred RTSS shared memory name to try first.",
    )
    parser.add_argument(
        "--stale-timeout-ms",
        type=int,
        default=2000,
        help="Stale timeout used for reject classification.",
    )
    return parser


def render_probe_results(results: Iterable[RtssProbeResult]) -> str:
    lines: List[str] = []
    for result in results:
        status_label = "OK" if result.mapping_found else "NO"
        lines.append(f"[{status_label}] mapping={result.mapping_name}")
        lines.append(f"  status={result.status}")
        lines.append(f"  mapping_found={result.mapping_found}")
        lines.append(f"  mapping_size={result.mapping_size}")
        if result.header:
            lines.append(
                "  header signature=0x{0:08X} version=0x{1:08X} app_entry_size={2} "
                "app_arr_offset={3} app_arr_size={4} osd_entry_size={5} osd_arr_offset={6} "
                "osd_arr_size={7} osd_frame={8}".format(
                    result.header.signature,
                    result.header.version,
                    result.header.app_entry_size,
                    result.header.app_arr_offset,
                    result.header.app_arr_size,
                    result.header.osd_entry_size,
                    result.header.osd_arr_offset,
                    result.header.osd_arr_size,
                    result.header.osd_frame,
                )
            )
        if result.error:
            lines.append(f"  error={result.error}")

        if not result.entry_diagnostics:
            lines.append("  entries=0")
            lines.append("")
            continue

        lines.append(f"  entries={len(result.entry_diagnostics)}")
        for entry in result.entry_diagnostics:
            lines.extend(_render_entry(entry))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _render_entry(entry: RtssEntryDiagnostic) -> List[str]:
    decision = "kept" if entry.kept else "rejected"
    reason = entry.reject_reason or "none"
    lines = [
        (
            "  entry[{0}] decision={1} reason={2} pid={3} process_name={4!r} profile_name={5!r} "
            "fps={6} frametime_ms={7} source_quality={8} sample_tick_ms={9} age_ms={10}"
        ).format(
            entry.index,
            decision,
            reason,
            entry.pid,
            entry.process_name,
            entry.profile_name,
            entry.fps,
            entry.frametime_ms,
            entry.source_quality,
            entry.sample_tick_ms,
            entry.age_ms,
        )
    ]
    raw_fields = " ".join(f"{key}={value}" for key, value in entry.raw_fields.items())
    lines.append(f"    raw_fields {raw_fields}")
    return lines


def main() -> int:
    args = _build_parser().parse_args()
    reader = RtssSharedMemoryReader(
        shared_memory_name=args.shared_memory_name,
        stale_timeout_ms=args.stale_timeout_ms,
    )
    results = reader.probe_mappings()
    print(render_probe_results(results), end="")

    for result in results:
        if result.status == "ok" and result.entry_diagnostics:
            return 0
    for result in results:
        if result.mapping_found:
            return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
