"""Dependency telemetry stays disabled before ONNX Runtime initializes."""

import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import types

import pytest


def test_disable_onnx_telemetry_sets_opt_out_and_calls_runtime_api(monkeypatch):
    from d2md._onnx import disable_onnx_telemetry

    calls = []
    runtime = types.ModuleType("onnxruntime")
    runtime.disable_telemetry_events = lambda: calls.append("disabled")
    monkeypatch.setitem(sys.modules, "onnxruntime", runtime)
    monkeypatch.setenv("ORT_DISABLE_TELEMETRY", "0")

    disable_onnx_telemetry()

    assert os.environ["ORT_DISABLE_TELEMETRY"] == "1"
    assert calls == ["disabled"]


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS session-file regression")
@pytest.mark.skipif(
    importlib.util.find_spec("onnxruntime") is None,
    reason="ONNX Runtime is not installed",
)
def test_disable_onnx_telemetry_prevents_posix_session_file(tmp_path):
    environment = os.environ.copy()
    for name in (
        "CI",
        "TF_BUILD",
        "GITHUB_ACTIONS",
        "GITLAB_CI",
        "CIRCLECI",
        "TRAVIS",
        "JENKINS_URL",
        "CODEBUILD_BUILD_ID",
        "BUILDKITE",
        "TEAMCITY_VERSION",
        "APPVEYOR",
        "BITBUCKET_BUILD_NUMBER",
        "SYSTEM_TEAMFOUNDATIONCOLLECTIONURI",
    ):
        environment.pop(name, None)
    environment["ORT_DISABLE_TELEMETRY"] = "0"

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from d2md._onnx import disable_onnx_telemetry; "
                "disable_onnx_telemetry(); from markitdown import MarkItDown"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert not (Path(tmp_path) / ":memory:.ses").exists()
