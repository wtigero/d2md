"""Importing the CLI must not reconfigure the whole process.

The noise worth suppressing comes from docling, markitdown and transformers,
all of which are imported lazily inside a conversion — so doing it when the
command runs is early enough, and doing it at import time silences warnings
and logging for anyone who merely imports `d2md`.
"""

import subprocess
import sys


def _probe(body: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", body], capture_output=True, text=True
    )


def test_importing_the_cli_leaves_logging_and_warnings_alone():
    completed = _probe(
        "import logging, warnings\n"
        "filters = warnings.filters[:]\n"
        "import d2md.cli\n"
        "assert logging.root.manager.disable == 0, 'logging was disabled on import'\n"
        "assert warnings.filters == filters, 'warning filters changed on import'\n"
    )

    assert completed.returncode == 0, completed.stderr


def test_running_the_command_still_suppresses_backend_noise(tmp_path):
    source = tmp_path / "note.txt"
    source.write_text("plain content long enough to pass output validation")
    completed = _probe(
        "import logging, sys\n"
        "from d2md import cli\n"
        f"cli.main([{str(source)!r}, '-o', {str(tmp_path / 'out')!r}, '-q'])\n"
        "assert logging.root.manager.disable == logging.WARNING\n"
    )

    assert completed.returncode == 0, completed.stderr
