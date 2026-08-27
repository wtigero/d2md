#!/usr/bin/env bash
set -euo pipefail

skip_integration=false
require_gpu=false
profile=base
device=auto
device_set=false

usage() {
    echo "Usage: $0 [--profile base|ocr|docling] [--skip-integration] [--require-gpu] [--device auto|cpu|cuda|mps|xpu]" >&2
}

while (($#)); do
    case "$1" in
        --profile)
            if (($# < 2)); then
                usage
                exit 2
            fi
            profile=$2
            shift
            case "$profile" in
                base|ocr|docling) ;;
                *)
                    usage
                    exit 2
                    ;;
            esac
            ;;
        --skip-integration)
            skip_integration=true
            ;;
        --require-gpu)
            require_gpu=true
            ;;
        --device)
            if (($# < 2)); then
                usage
                exit 2
            fi
            device=$2
            device_set=true
            shift
            case "$device" in
                auto|cpu|cuda|mps|xpu) ;;
                *)
                    usage
                    exit 2
                    ;;
            esac
            ;;
        *)
            usage
            exit 2
            ;;
    esac
    shift
done

if [[ "$profile" != docling && "$device_set" == true ]]; then
    echo "--device requires --profile docling" >&2
    exit 2
fi
if [[ "$profile" != docling && "$require_gpu" == true ]]; then
    echo "--require-gpu requires --profile docling" >&2
    exit 2
fi

repository_root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$repository_root"

python_command=""
for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))'
    then
        python_command=$candidate
        break
    fi
done

if [[ -z "$python_command" ]]; then
    echo "Python 3.10 through 3.13 is required." >&2
    exit 1
fi

echo "Creating the isolated full-test environment..."
"$python_command" -m venv --clear .venv-smoke
test_python="$repository_root/.venv-smoke/bin/python"

echo "Installing editable development and Docling dependencies..."
"$test_python" -m pip install -e '.[dev,docling]'

echo "Generating synthetic example documents..."
"$test_python" examples/generate.py

echo "Running pytest..."
"$test_python" -m pytest

if [[ "$skip_integration" == true ]]; then
    echo "Profile installation and integration skipped (--skip-integration)."
    exit 0
fi

echo "Creating the isolated $profile profile environment..."
"$python_command" -m venv --clear .venv-smoke-profile
profile_python="$repository_root/.venv-smoke-profile/bin/python"

case "$profile" in
    base) install_target='.' ;;
    ocr) install_target='.[ocr]' ;;
    docling) install_target='.[docling]' ;;
esac

echo "Installing profile target: $install_target"
"$profile_python" -m pip install -e "$install_target"
"$profile_python" -m pip check
"$profile_python" -c 'import d2md; print(f"Imported d2md from: {d2md.__file__}")'

if [[ "$profile" == docling ]]; then
    echo "Checking PyTorch accelerator availability..."
    "$profile_python" - "$require_gpu" "$device" <<'PY'
import sys

import torch

require_gpu = sys.argv[1] == "true"
device = sys.argv[2]
cuda = bool(torch.version.cuda and torch.cuda.is_available())
xpu_api = getattr(torch, "xpu", None)
xpu = bool(xpu_api and xpu_api.is_available())
mps_api = getattr(getattr(torch, "backends", None), "mps", None)
mps = bool(mps_api and mps_api.is_available())
available = {"cuda": cuda, "xpu": xpu, "mps": mps, "cpu": True, "auto": True}

print(f"PyTorch {torch.__version__}")
print(f"CUDA available: {cuda}")
if cuda:
    print(f"CUDA device: {torch.cuda.get_device_name(0)}")
print(f"Intel XPU available: {xpu}")
print(f"Apple MPS available: {mps}")

if require_gpu:
    selected_available = (
        cuda or xpu or mps
        if device == "auto"
        else device != "cpu" and available[device]
    )
    if not selected_available:
        raise SystemExit(f"--require-gpu was set, but {device} is unavailable")
PY
fi

echo "Running the $profile example profile..."
"$profile_python" examples/smoke.py --profile "$profile" --device "$device"

echo "Linux/macOS $profile smoke test passed."
