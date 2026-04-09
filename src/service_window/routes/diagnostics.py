"""Diagnostics page — runs diagnostics.sh and shows logs."""

import subprocess
from pathlib import Path

from flask import Blueprint, render_template

from service_window.auth import login_required

diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/diagnostics")

_DIAG_PATHS = [
    "/opt/bluesignal/scripts/diagnostics.sh",
    str(Path(__file__).parents[3] / "scripts" / "diagnostics.sh"),
]


@diagnostics_bp.route("/")
@login_required
def index():
    return render_template("diagnostics.html")


@diagnostics_bp.route("/run")
@login_required
def run_diagnostics():
    script = None
    for p in _DIAG_PATHS:
        if Path(p).exists():
            script = p
            break

    if not script:
        return render_template(
            "diagnostics.html",
            output="diagnostics.sh not found",
            logs="",
        )

    try:
        result = subprocess.run(
            ["bash", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        output = result.stdout + result.stderr
    except subprocess.TimeoutExpired:
        output = "Diagnostics timed out after 30 seconds."
    except Exception as e:
        output = f"Error running diagnostics: {e}"

    # Grab recent journal logs
    logs = _get_recent_logs()
    return render_template("diagnostics.html", output=output, logs=logs)


def _get_recent_logs(lines: int = 100) -> str:
    """Get recent bluesignal-wqm journal entries."""
    try:
        result = subprocess.run(
            ["journalctl", "-u", "bluesignal-wqm", "-n", str(lines), "--no-pager"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout
    except Exception:
        return "(Could not read journal logs)"
