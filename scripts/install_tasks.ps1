# Registers PolyBot's seven Windows scheduled tasks. All run inside WSL.
#
#   PolyBot-Hourly     the pipeline, once an hour at five past
#   PolyBot-Agent      the paper-trading agent, every 10 minutes
#   PolyBot-Discover   the market catalog refresh, every 15 minutes
#   PolyBot-Stream     the live websocket recorder, which runs all the time
#   PolyBot-Rollup     the daily rollup: pack finished days, copy snapshots, build bars
#   PolyBot-Paper      the PAPER trading cycle and the status file, every 5 minutes
#   PolyBot-Kalshi     Polymarket and Kalshi prices for the same questions, every 10 minutes
#
# Docker and the Mac run the same timetable from one process instead (polybot/scheduler.py,
# `polybot run`). If you change a time here, change it there too; tests/test_scheduler.py checks.
#
# Install, or reload after changing this file (safe to run again; it replaces all seven):
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1
#
# Register just one or two (replacing PolyBot-Stream while it runs stops the streamer for a few
# minutes, so leave it out when only another task changed):
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_tasks.ps1 -Only PolyBot-Rollup
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
    [string]$Venv = '$HOME/venvs/polybot/bin/python',
    [string[]]$Only = @()
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

function Test-Wanted([string]$Name) {
    # With no -Only, every task is registered. With -Only, just the ones named.
    return ($Only.Count -eq 0) -or ($Only -contains $Name)
}

# The hourly task starts at five past so it does not collide with the agent's run on the hour.
# It may wake a sleeping PC once an hour; the agent may not, or the PC would never stay asleep.
if (Test-Wanted "PolyBot-Hourly") {
    Register-PolyBotTask -Name "PolyBot-Hourly" -Script "run_pipeline.sh" -EveryMinutes 60 -StartMinute 5 `
        -LimitMinutes 30 -Wake $true -Description "PolyBot hourly pipeline: snapshot, resolutions, paper trades (WSL)"
}
if (Test-Wanted "PolyBot-Agent") {
    Register-PolyBotTask -Name "PolyBot-Agent" -Script "run_agent.sh" -EveryMinutes 10 -StartMinute 0 `
        -LimitMinutes 9 -Wake $false -Description "PolyBot v2 paper-trading agent, every 10 minutes (WSL)"
}

# The catalog refresh starts at 7, 22, 37 and 52 past, between the other two jobs. Its download
# happens before it takes the database lock, so it only holds the others up for a few seconds.
if (Test-Wanted "PolyBot-Discover") {
    Register-PolyBotTask -Name "PolyBot-Discover" -Script "run_discover.sh" -EveryMinutes 15 -StartMinute 7 `
        -LimitMinutes 14 -Wake $false -Description "PolyBot market catalog refresh, every 15 minutes (WSL)"
}

# The streamer never finishes, so it has no time limit (0 means unlimited). Its trigger fires at login
# and then every 5 minutes: while the streamer is running the extra starts are ignored, and if it has
# died one of them brings it back. That makes the trigger a watchdog as well as a starter.
if (Test-Wanted "PolyBot-Stream") {
    Register-PolyBotTask -Name "PolyBot-Stream" -Script "run_stream.sh" -EveryMinutes 5 -StartMinute 1 `
        -LimitMinutes 0 -Wake $false -Description "PolyBot live order book and trade recorder (WSL websocket)"
}

# The rollup runs once a day, a couple of hours after midnight UTC, so the day it packs is over.
# Task Scheduler works in local time, so that moment is converted to this PC's clock here. A
# daylight-saving change moves it by an hour, which still leaves it after midnight UTC.
if (Test-Wanted "PolyBot-Rollup") {
    $rollupLocal = [datetime]::SpecifyKind([datetime]::UtcNow.Date.AddHours(2).AddMinutes(10), 'Utc').ToLocalTime()
    Register-PolyBotTask -Name "PolyBot-Rollup" -Script "run_rollup.sh" -EveryMinutes 1440 `
        -StartMinute ($rollupLocal.Hour * 60 + $rollupLocal.Minute) -LimitMinutes 60 -Wake $false `
        -Description "PolyBot daily rollup: Parquet packing, snapshot copy, one-minute bars (WSL)"
}

# The paper job keeps its own small database and never takes the main database lock, so it can run
# every 5 minutes whatever the other jobs are doing. It also writes the status file each time.
if (Test-Wanted "PolyBot-Paper") {
    Register-PolyBotTask -Name "PolyBot-Paper" -Script "run_paper.sh" -EveryMinutes 5 -StartMinute 3 `
        -LimitMinutes 4 -Wake $false -Description "PolyBot PAPER trading cycle and status file, every 5 minutes (WSL)"
}

# The Kalshi comparison runs at 4, 14, 24 ... past the hour, between the other jobs. Once a day it lists
# every Kalshi market (about two and a half minutes, one page a second) before pairing; other runs take
# seconds. Its downloads happen before it takes the database lock, so it never holds the others up.
if (Test-Wanted "PolyBot-Kalshi") {
    Register-PolyBotTask -Name "PolyBot-Kalshi" -Script "run_kalshi.sh" -EveryMinutes 10 -StartMinute 4 `
        -LimitMinutes 9 -Wake $false -Description "PolyBot Kalshi comparison: both sites' prices, every 10 minutes (WSL)"
}
