param(
    [switch]$NoServerControls
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$python = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) { throw "Không tìm thấy .venv. Chạy scripts\setup_windows.ps1 trước." }
& $python -c "import PySide6" 2>$null
if ($LASTEXITCODE -ne 0) { throw "Thiếu PySide6 trong .venv. Chạy: uv pip install --python .venv\Scripts\python.exe PySide6" }
$args = @((Join-Path $PSScriptRoot "server.py"))
if ($NoServerControls) { $args += "--no-server-controls" }
& $python @args
