param([string]$DataDir, [string]$WorkbenchUrl)
$ErrorActionPreference = 'Stop'
if ($DataDir) { $env:SIM_ADVISOR_DATA = [System.IO.Path]::GetFullPath($DataDir) }
elseif (-not $env:SIM_ADVISOR_DATA) { $env:SIM_ADVISOR_DATA = Join-Path $env:LOCALAPPDATA 'sim-advisor-workbench' }
$env:PYTHONUTF8 = '1'
$env:PYTHONDONTWRITEBYTECODE = '1'
$env:PYTHONNOUSERSITE = '1'
Remove-Item Env:PYTHONPATH,Env:PYTHONHOME,Env:PYTHONUSERBASE -ErrorAction SilentlyContinue
$mcpPython = Join-Path $PSScriptRoot 'python\python.exe'
$mcpArgs = @((Join-Path $PSScriptRoot 'workbench\workbench_mcp.py'))
if ($WorkbenchUrl) { $mcpArgs += @('--url', $WorkbenchUrl) }
& $mcpPython @mcpArgs
exit $LASTEXITCODE
