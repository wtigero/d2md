[CmdletBinding()]
param(
    [switch]$SkipIntegration,
    [switch]$RequireGpu,
    [ValidateSet("Base", "Ocr", "Docling")]
    [string]$Profile = "Base",
    [ValidateSet("auto", "cpu", "cuda", "mps", "xpu")]
    [string]$Device = "auto"
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
$env:PYTHONUTF8 = '1'

$DeviceWasSpecified = $PSBoundParameters.ContainsKey('Device')
if ($Profile -ne 'Docling' -and $DeviceWasSpecified) {
    throw '-Device requires the Docling profile.'
}
if ($Profile -ne 'Docling' -and $RequireGpu) {
    throw '-RequireGpu requires the Docling profile.'
}

$RepositoryRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepositoryRoot

$PythonCommand = $null
$PythonPrefix = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    foreach ($Version in @('3.13', '3.12', '3.11', '3.10')) {
        & py "-$Version" -c 'raise SystemExit(0)' 2>$null
        if ($LASTEXITCODE -eq 0) {
            $PythonCommand = 'py'
            $PythonPrefix = @("-$Version")
            break
        }
    }
}
if (-not $PythonCommand -and (Get-Command python -ErrorAction SilentlyContinue)) {
    & python -c 'import sys; raise SystemExit(not ((3, 10) <= sys.version_info[:2] <= (3, 13)))'
    if ($LASTEXITCODE -eq 0) {
        $PythonCommand = 'python'
    }
}
if (-not $PythonCommand) {
    throw 'Python 3.10 through 3.13 is required. Install Python, then run this script again.'
}

Write-Host 'Creating the isolated full-test environment...'
& $PythonCommand @PythonPrefix -m venv --clear .venv-smoke
if ($LASTEXITCODE -ne 0) { throw 'Full-test environment creation failed.' }
$TestPython = Join-Path $RepositoryRoot '.venv-smoke\Scripts\python.exe'

Write-Host 'Installing editable development and Docling dependencies...'
& $TestPython -m pip install -e '.[dev,docling]'
if ($LASTEXITCODE -ne 0) { throw 'Development dependency installation failed.' }

Write-Host 'Generating synthetic example documents...'
& $TestPython examples/generate.py
if ($LASTEXITCODE -ne 0) { throw 'Example generation failed.' }

Write-Host 'Running pytest...'
& $TestPython -m pytest
if ($LASTEXITCODE -ne 0) { throw 'pytest failed.' }

if ($SkipIntegration) {
    Write-Host 'Profile installation and integration skipped (-SkipIntegration).'
    exit 0
}

$ProfileName = $Profile.ToLowerInvariant()
Write-Host "Creating the isolated $ProfileName profile environment..."
& $PythonCommand @PythonPrefix -m venv --clear .venv-smoke-profile
if ($LASTEXITCODE -ne 0) { throw 'Profile environment creation failed.' }
$ProfilePython = Join-Path $RepositoryRoot '.venv-smoke-profile\Scripts\python.exe'

$InstallTarget = switch ($ProfileName) {
    'base' { '.' }
    'ocr' { '.[ocr]' }
    'docling' { '.[docling]' }
}
Write-Host "Installing profile target: $InstallTarget"
& $ProfilePython -m pip install -e $InstallTarget
if ($LASTEXITCODE -ne 0) { throw 'Profile installation failed.' }
& $ProfilePython -m pip check
if ($LASTEXITCODE -ne 0) { throw 'Profile dependency check failed.' }
Write-Host 'Imported d2md from:'
& $ProfilePython -c 'import d2md; print(d2md.__file__)'
if ($LASTEXITCODE -ne 0) { throw 'Profile import check failed.' }

if ($ProfileName -eq 'docling') {
    Write-Host 'Checking PyTorch accelerator availability...'
    $RequireGpuValue = if ($RequireGpu) { 'true' } else { 'false' }
    $Probe = @'
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
    selected_available = (cuda or xpu or mps) if device == "auto" else device != "cpu" and available[device]
    if not selected_available:
        raise SystemExit(f"-RequireGpu was set, but {device} is unavailable")
'@
    & $ProfilePython -c $Probe $RequireGpuValue $Device
    if ($LASTEXITCODE -ne 0) { throw 'Requested accelerator is unavailable.' }
}

Write-Host "Running the $ProfileName example profile..."
& $ProfilePython examples/smoke.py --profile $ProfileName --device $Device
if ($LASTEXITCODE -ne 0) { throw 'Example profile failed.' }

Write-Host "Windows $ProfileName smoke test passed."
