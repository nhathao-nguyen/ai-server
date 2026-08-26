param(
    [string]$HostAddress = $(if ($env:HOST) { $env:HOST } else { "127.0.0.1" }),
    [int]$PortNumber = $(if ($env:PORT) { [int]$env:PORT } else { 8000 }),
    [string]$TlsCertFile = "",
    [string]$TlsKeyFile = "",
    [switch]$AllowInsecureLan
)

$ErrorActionPreference = "Stop"
$parsedAddress = $null
if (-not [System.Net.IPAddress]::TryParse($HostAddress, [ref]$parsedAddress)) {
    throw "HOST must be a literal loopback or private IPv4 address"
}
$bytes = $parsedAddress.GetAddressBytes()
$isLoopback = [System.Net.IPAddress]::IsLoopback($parsedAddress)
$isPrivateIpv4 = $parsedAddress.AddressFamily -eq [System.Net.Sockets.AddressFamily]::InterNetwork -and (
    ($bytes[0] -eq 10) -or
    ($bytes[0] -eq 172 -and $bytes[1] -ge 16 -and $bytes[1] -le 31) -or
    ($bytes[0] -eq 192 -and $bytes[1] -eq 168)
)
if (-not ($isLoopback -or $isPrivateIpv4 -or $HostAddress -eq "0.0.0.0")) {
    throw "HOST must be loopback, 0.0.0.0, or an RFC1918 private IPv4 address; public binds are rejected"
}
$tlsArgs = @()
$resolvedTlsCertFile = $null
$resolvedTlsKeyFile = $null
if (-not $isLoopback) {
    if ([string]::IsNullOrWhiteSpace($TlsCertFile) -or [string]::IsNullOrWhiteSpace($TlsKeyFile)) {
        if (-not $AllowInsecureLan) {
            throw "Insecure LAN bind requires -AllowInsecureLan; otherwise provide -TlsCertFile and -TlsKeyFile"
        }
        $env:LAN_ONLY = "true"
    } else {
        $env:LAN_ONLY = "false"
    }
    if (-not [string]::IsNullOrWhiteSpace($TlsCertFile) -and -not [string]::IsNullOrWhiteSpace($TlsKeyFile)) {
        if (-not (Test-Path -LiteralPath $TlsCertFile -PathType Leaf) -or -not (Test-Path -LiteralPath $TlsKeyFile -PathType Leaf)) {
            throw "LAN/private bind TLS certificate and key files must exist"
        }
        $resolvedTlsCertFile = (Resolve-Path -LiteralPath $TlsCertFile).Path
        $resolvedTlsKeyFile = (Resolve-Path -LiteralPath $TlsKeyFile).Path
        $certificatePem = Get-Content -LiteralPath $resolvedTlsCertFile -Raw
        $privateKeyPem = Get-Content -LiteralPath $resolvedTlsKeyFile -Raw
        if (-not $certificatePem.Contains("BEGIN CERTIFICATE") -or -not $privateKeyPem.Contains("PRIVATE KEY")) {
            throw "LAN/private bind requires PEM certificate and private key material"
        }
        $tlsArgs = @("--ssl-certfile", $resolvedTlsCertFile, "--ssl-keyfile", $resolvedTlsKeyFile)
    }
} else {
    $env:LAN_ONLY = "false"
}
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root
$venvPython = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    throw "Project environment is missing. Run scripts\setup_windows.ps1 first."
}

$env:HOST = $HostAddress
$env:PORT = [string]$PortNumber
if (-not $isLoopback) {
    $env:TLS_CERT_FILE = $resolvedTlsCertFile
    $env:TLS_KEY_FILE = $resolvedTlsKeyFile
}
$uvicornArgs = @("-m", "uvicorn", "app.main:app", "--host", $HostAddress, "--port", [string]$PortNumber) + $tlsArgs
& $venvPython @uvicornArgs
