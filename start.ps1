param([int]$Port = 8765, [switch]$BuildIndex)
$ErrorActionPreference = 'Stop'
$env:PYTHONUTF8 = '1'
$appRoot = $PSScriptRoot
$pythonPath = Join-Path $appRoot '..\..\.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath)) {
    $pythonPath = (Get-Command python -ErrorAction Stop).Source
}
Push-Location $appRoot
try {
    $arguments = @('run.py', '--port', "$Port")
    if ($BuildIndex) { $arguments += '--build-index' }
    & $pythonPath @arguments
} finally {
    Pop-Location
}
