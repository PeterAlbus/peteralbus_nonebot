from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any


def _command(args: list[str], timeout: float = 3.0) -> str | None:
    try:
        result = subprocess.run(
            args,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _sysctl(name: str) -> str | None:
    return _command(["sysctl", "-n", name])


def _memory_status() -> dict[str, Any]:
    total_raw = _sysctl("hw.memsize")
    vm_stat = _command(["vm_stat"])
    if total_raw is None or vm_stat is None:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None}
    try:
        total = int(total_raw)
    except ValueError:
        return {"total_bytes": None, "used_bytes": None, "free_bytes": None}

    page_match = re.search(r"page size of (\d+) bytes", vm_stat)
    page_size = int(page_match.group(1)) if page_match else 4096
    pages: dict[str, int] = {}
    for line in vm_stat.splitlines():
        match = re.match(r"([^:]+):\s+([\d.]+)", line)
        if match:
            pages[match.group(1).strip().lower()] = int(
                match.group(2).replace(".", "")
            )

    free_pages = pages.get("pages free", 0)
    inactive_pages = pages.get("pages inactive", 0)
    speculative_pages = pages.get("pages speculative", 0)
    available = min(total, (free_pages + inactive_pages + speculative_pages) * page_size)
    return {
        "total_bytes": total,
        "used_bytes": max(0, total - available),
        "free_bytes": free_pages * page_size,
        "inactive_bytes": inactive_pages * page_size,
        "wired_bytes": pages.get("pages wired down", 0) * page_size,
        "compressed_bytes": pages.get("pages occupied by compressor", 0) * page_size,
        "page_size_bytes": page_size,
        "available_is_estimate": True,
    }


def _uptime_seconds() -> int | None:
    raw = _sysctl("kern.boottime")
    match = re.search(r"sec\s*=\s*(\d+)", raw or "")
    if not match:
        return None
    return max(0, int(time.time()) - int(match.group(1)))


def get_system_status(disk_path: Path) -> dict[str, Any]:
    version = _command(["sw_vers", "-productVersion"])
    try:
        load_average = list(os.getloadavg())
    except (AttributeError, OSError):
        load_average = None
    usage = shutil.disk_usage(disk_path if disk_path.exists() else Path("/"))
    return {
        "platform": platform.platform(),
        "macos_version": version,
        "architecture": platform.machine(),
        "uptime_seconds": _uptime_seconds(),
        "cpu": {
            "logical_cores": os.cpu_count(),
            "load_average_1_5_15": load_average,
        },
        "memory": _memory_status(),
        "disk": {
            "path": str(disk_path),
            "total_bytes": usage.total,
            "used_bytes": usage.used,
            "free_bytes": usage.free,
        },
    }
