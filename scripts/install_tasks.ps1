# Registers PolyBot's three Windows scheduled tasks. All run inside WSL.
#
#   PolyBot-Hourly     the pipeline, once an hour at five past
#   PolyBot-Agent      the paper-trading agent, every 10 minutes
#   PolyBot-Discover   the market catalog refresh, every 15 minutes
#
# Install, or reload after changing this file (safe to run again; it replaces all three):
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#
# Stop and start without removing them:
#
#   Get-ScheduledTask PolyBot-* | Disable-ScheduledTask
#   Get-ScheduledTask PolyBot-* | Enable-ScheduledTask
#
# Remove them:
#
#   Get-ScheduledTask PolyBot-* | Unregister-ScheduledTask -Confirm:$false
#
# The tasks survive a reboot: they start again as soon as you log in, and a run that was
# missed while the PC was off runs straight away. They cannot run while the PC is asleep
# or while nobody is logged in. Paper trading only.
param(
    [string]$Distro = "Ubuntu",
    [string]$WslProject = "/mnt/c/Users/$env:USERNAME/PolyBot",
    [string]$Venv = '$HOME/venvs/polybot/bin/python'
)

function Register-PolyBotTask {
    param([string]$Name, [string]$Script, [int]$EveryMinutes, [int]$StartMinute,
          [int]$LimitMinutes, [bool]$Wake, [string]$Description)

    # Anything a run prints goes to task_output.log. Normal messages go to logs/polybot.log,
    # so this file stays empty unless a run crashes before logging starts.
    $inner = "cd $WslProject && mkdir -p logs && POLYBOT_PYTHON=$Venv bash scripts/$Script >> logs/task_output.log 2>&1"
    $action = New-ScheduledTaskAction -Execute "wsl.exe" -Argument "-d $Distro -- bash -lc `"$inner`""

    # Trigger 1: repeat forever, counted from a time today that has already passed.
    $repeat = New-ScheduledTaskTrigger -Once -At (Get-Date).Date.AddMinutes($StartMinute) `
        -RepetitionInterval (New-TimeSpan -Minutes $EveryMinutes)
    # Trigger 2: run once right after login, so a reboot does not cost a whole interval.
    $logon = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME

    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew `
        -ExecutionTimeLimit (New-TimeSpan -Minutes $LimitMinutes) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    $settings.WakeToRun = $Wake
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited

    try {
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $repeat, $logon -Settings $settings `
            -Principal $principal -Description $Description -Force -ErrorAction Stop | Out-Null
    } catch {
        # Some PCs only let an administrator create a login trigger. The repeating trigger alone
        # still survives reboots; the first run just waits for the next scheduled time.
        Write-Host "  (login trigger not allowed here; registering '$Name' with the repeating trigger only)"
        Register-ScheduledTask -TaskName $Name -Action $action -Trigger $repeat -Settings $settings `
            -Principal $principal -Description $Description -Force -ErrorAction Stop | Out-Null
    }
    Write-Host "Registered '$Name': every $EveryMinutes minutes via WSL ($Distro)."
}

# The hourly task starts at five past so it does not collide with the agent's run on the hour.
# It may wake a sleeping PC once an hour; the agent may not, or the PC would never stay asleep.
Register-PolyBotTask -Name "PolyBot-Hourly" -Script "run_pipeline.sh" -EveryMinutes 60 -StartMinute 5 `
    -LimitMinutes 30 -Wake $true -Description "PolyBot hourly pipeline: snapshot, resolutions, paper trades (WSL)"
Register-PolyBotTask -Name "PolyBot-Agent" -Script "run_agent.sh" -EveryMinutes 10 -StartMinute 0 `
    -LimitMinutes 9 -Wake $false -Description "PolyBot v2 paper-trading agent, every 10 minutes (WSL)"# The catalog refresh starts at 7, 22, 37 and 52 past, between the other two jobs. Its download
# happens before it takes the database lock, so it only holds the others up for a few seconds.
Register-PolyBotTask -Name "PolyBot-Discover" -Script "run_discover.sh" -EveryMinutes 15 -StartMinute 7 `
    -LimitMinutes 14 -Wake $false -Description "PolyBot market catalog refresh, every 15 minutes (WSL)"
