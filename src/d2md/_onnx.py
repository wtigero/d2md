"""Keep optional ONNX Runtime dependencies from starting telemetry."""

from __future__ import annotations

import os
import sys


def prepare_onnx_telemetry_opt_out() -> None:
    """Set the process opt-out before ONNX Runtime can initialize."""
    os.environ["ORT_DISABLE_TELEMETRY"] = "1"


def disable_loaded_onnx_telemetry() -> None:
    """Apply the runtime API control when ONNX Runtime is already imported."""
    runtime = sys.modules.get("onnxruntime")
    disable = getattr(runtime, "disable_telemetry_events", None)
    if callable(disable):
        disable()


def disable_onnx_telemetry() -> None:
    """Apply both environment and runtime telemetry controls."""
    prepare_onnx_telemetry_opt_out()
    try:
        import onnxruntime  # noqa: F401
    except ImportError:
        return
    disable_loaded_onnx_telemetry()
