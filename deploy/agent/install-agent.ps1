# Installs the LabForge agent on Windows (x64 or ARM64) as a startup scheduled task.
# Run from an elevated PowerShell next to agent.py:
#   .\install-agent.ps1 -Server http://labforge.lab:8000 -Env dev -Name win-test-01 -Token <token>
param(
    [Parameter(Mandatory = $true)][string]$Server,
    [Parameter(Mandatory = $true)][string]$Env,
    [Parameter(Mandatory = $true)][string]$Name,
    [string]$Token = "",
    [string]$InstallDir = "$env:ProgramData\LabForge",
    [int]$Interval = 30
)
$ErrorActionPreference = "Stop"

$python = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $python) { throw "Python 3 not found on PATH. Install it from python.org (ARM64 builds are available)." }

New-Item -ItemType Directory -Force -Path $InstallDir | Out-Null
Copy-Item -Force (Join-Path $PSScriptRoot "..\..\labforge\agent.py") (Join-Path $InstallDir "agent.py") -ErrorAction SilentlyContinue
if (-not (Test-Path (Join-Path $InstallDir "agent.py"))) { Copy-Item -Force (Join-Path $PSScriptRoot "agent.py") $InstallDir }

& $python -m pip install --quiet --upgrade psutil

$agentArgs = "`"$InstallDir\agent.py`" --server $Server --env $Env --name $Name --interval $Interval"
if ($Token) { $agentArgs += " --token $Token" }

$action = New-ScheduledTaskAction -Execute $python -Argument $agentArgs -WorkingDirectory $InstallDir
$trigger = New-ScheduledTaskTrigger -AtStartup
$settings = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -ExecutionTimeLimit ([TimeSpan]::Zero)
Register-ScheduledTask -TaskName "LabForgeAgent" -Action $action -Trigger $trigger -Settings $settings -User "SYSTEM" -RunLevel Highest -Force | Out-Null
Start-ScheduledTask -TaskName "LabForgeAgent"

$once = @("$InstallDir\agent.py", "--server", $Server, "--env", $Env, "--name", $Name, "--once")
if ($Token) { $once += @("--token", $Token) }
& $python @once
Write-Host "LabForge agent installed and reporting as $Env.host.$Name"
