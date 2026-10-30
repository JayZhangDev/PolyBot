# Registers the 10-minute PolyBot v2 agent as a Windows scheduled task that runs inside WSL,
# mirroring the existing hourly "PolyBot-Hourly" pipeline task.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_agent_task.ps1
#
# Adjust $Distro, $WslProject and $Venv if your setup differs. Paper trading only.
param(
    [string]$Distro = "Ubuntu",
    [string]$WslProject = "/mnt/c/Users/$env:USERNAME/PolyBot",
    [string]$Venv = '$HOME/venvs/polybot/bin/python',
    [string]$TaskName = "PolyBot-Agent"
)

$inner = "cd $WslProject && POLYBOT_PYTHON=$Venv bash scripts/run_agent.sh >> agent.log 2>&1"
$action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -- bash -lc `"$inner`""
$trigger = New-ScheduledTaskTrigger -Once -At (Get-Date).Date -RepetitionInterval (New-TimeSpan -Minutes 10)
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 9) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
    -Principal $principal -Description "PolyBot v2 autonomous paper-trading agent, every 10 minutes (WSL)" -Force | Out-Null
Write-Host "Registered '$TaskName': every 10 minutes via WSL ($Distro)."
