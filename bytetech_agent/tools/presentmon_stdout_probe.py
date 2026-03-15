"""
Diagnostic tool for PresentMon stdout capture.

Example:
    python -m bytetech_agent.tools.presentmon_stdout_probe --process-name dwm.exe --duration 5
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from typing import Optional

from bytetech_agent.providers.presentmon_provider import PresentMonCsvParser, PresentMonProvider


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Probe PresentMon stdout and parser output.")
    parser.add_argument("--process-name", default="dwm.exe", help="Target process name.")
    parser.add_argument("--process-id", type=int, default=0, help="Target process id.")
    parser.add_argument("--duration", type=int, default=5, help="Capture duration in seconds.")
    parser.add_argument("--executable-path", default="", help="Optional explicit PresentMon.exe path.")
    return parser


def _resolve_executable(path_override: str) -> Optional[str]:
    config = type("ProbeConfig", (), {"executable_path": path_override or None})()
    provider = PresentMonProvider(config)
    return provider._discover_presentmon_exe()


def main() -> int:
    args = _build_parser().parse_args()
    exe_path = _resolve_executable(args.executable_path)
    if not exe_path:
        print("PresentMon.exe was not found.")
        return 2

    command = [
        exe_path,
        "--output_stdout",
        "--no_console_stats",
        "--stop_existing_session",
        "--session_name",
        f"ByteTechProbe-{int(time.time())}",
    ]

    if args.process_id > 0:
        command.extend(["--process_id", str(args.process_id), "--terminate_on_proc_exit"])
    else:
        command.extend(["--process_name", args.process_name])

    print("Executable :", exe_path)
    print("Command    :", subprocess.list2cmdline(command))

    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            universal_newlines=True,
        )
    except OSError as exc:
        print(f"Failed to start PresentMon.exe: {exc}")
        if getattr(exc, "winerror", None) == 740:
            print("Hint: run the probe from an elevated terminal or use an elevated agent service.")
        return 3

    parser = PresentMonCsvParser()
    parsed_records = 0
    stdout_lines = 0
    start = time.monotonic()

    try:
        while time.monotonic() - start < args.duration:
            line = process.stdout.readline() if process.stdout else ""
            if not line:
                if process.poll() is not None:
                    break
                time.sleep(0.1)
                continue

            stdout_lines += 1
            if stdout_lines <= 5:
                print(f"stdout[{stdout_lines}] {line.rstrip()}")

            try:
                sample = parser.parse_line(line)
            except Exception as exc:
                print(f"parser_error: {exc} | line={line.rstrip()!r}")
                continue

            if sample is not None:
                parsed_records += 1
                if parsed_records <= 5:
                    print(
                        "parsed[{0}] pid={1} process={2} frametime_ms={3} cpu_busy_ms={4} gpu_busy_ms={5}".format(
                            parsed_records,
                            sample.pid,
                            sample.process_name,
                            sample.frametime_ms,
                            sample.cpu_busy_ms,
                            sample.gpu_busy_ms,
                        )
                    )
    finally:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    stderr_output = process.stderr.read() if process.stderr else ""
    if stderr_output.strip():
        print("stderr:")
        print(stderr_output.strip())

    print(f"stdout_lines={stdout_lines}")
    print(f"parsed_records={parsed_records}")
    if parsed_records > 0:
        print("Probe result: PresentMon stdout parser produced non-zero frame records.")
        return 0

    print("Probe result: no parsed frame records captured in the selected window.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
