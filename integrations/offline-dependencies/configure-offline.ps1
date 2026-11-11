param(
    [string]$DataDir,
    [string]$ComsolHome,
    [string]$DocRoot,
    [string]$Applications,
    [switch]$VerifyOnly
)
$ErrorActionPreference = 'Stop'
$offlineNode = Join-Path $PSScriptRoot 'node\node.exe'
if (-not (Test-Path -LiteralPath $offlineNode -PathType Leaf)) { throw 'Missing bundled node\node.exe; extract the complete offline archive first.' }
$offlineArgs = @((Join-Path $PSScriptRoot 'initialize.mjs'))
if ($DataDir) { $offlineArgs += @('--data-dir', $DataDir) }
if ($ComsolHome) { $offlineArgs += @('--comsol-home', $ComsolHome) }
if ($DocRoot) { $offlineArgs += @('--doc-root', $DocRoot) }
if ($Applications) { $offlineArgs += @('--applications', $Applications) }
if ($VerifyOnly) { $offlineArgs += '--verify-only' }
& $offlineNode @offlineArgs
if ($LASTEXITCODE -ne 0) { throw "Offline configuration failed (exit $LASTEXITCODE)." }
