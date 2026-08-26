param(
    [switch]$SkipModels
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$uv = Get-Command uv -ErrorAction SilentlyContinue
if (-not $uv) {
    throw "uv is required. Install it from https://docs.astral.sh/uv/ and run this script again."
}

$python = Get-Command python -ErrorAction SilentlyContinue
if (-not $python) {
    throw "Python 3.12+ is required."
}
$versionText = & $python.Source --version
if ($versionText -notmatch "Python (\d+)\.(\d+)") {
    throw "Could not determine Python version."
}
$major = [int]$Matches[1]
$minor = [int]$Matches[2]
if (($major -lt 3) -or (($major -eq 3) -and ($minor -lt 12))) {
    throw "Python 3.12 or newer is required; detected $versionText."
}

$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    & $uv.Source venv --python $python.Source (Join-Path $root ".venv")
}
$sitecustomize = Join-Path $root ".venv\Lib\site-packages\sitecustomize.py"
Copy-Item -LiteralPath (Join-Path $root "scripts\venv_sitecustomize.py") -Destination $sitecustomize -Force
& $uv.Source sync --locked --all-extras --python $venvPython

if (-not $SkipModels) {
    & $venvPython -m scripts.bootstrap_models --report (Join-Path $root "data\manifests\bootstrap-report.json")
}

Write-Host "Setup complete. Use scripts\run_server.ps1 to start the gateway."
