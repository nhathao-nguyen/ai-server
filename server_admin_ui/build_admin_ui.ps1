param(
    [string]$OutputDir = ""
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Không tìm thấy .venv. Chạy scripts\setup_windows.ps1 trước." }
& $python -c "import PySide6, PyInstaller" 2>$null
if ($LASTEXITCODE -ne 0) {
    throw "Thiếu PySide6 hoặc PyInstaller. Cài bằng: uv pip install --python .venv\Scripts\python.exe -r server_admin_ui\requirements.txt"
}

$dist = if ($OutputDir) { $OutputDir } else { Join-Path $root "dist\TtsServerAdmin" }
$work = Join-Path $root "build\TtsServerAdmin"
& $python -m PyInstaller --noconfirm --clean `
    --distpath $dist `
    --workpath $work `
    (Join-Path $PSScriptRoot "server_admin_ui.spec")
if ($LASTEXITCODE -ne 0) { throw "PyInstaller build failed with exit code $LASTEXITCODE" }
Write-Host "Desktop package: $(Join-Path $dist 'TtsServerAdmin')"
