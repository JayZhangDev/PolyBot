# Start, stop or check the live streamer from Windows.
#
#   powershell -ExecutionPolicy Bypass -File scripts\stream.ps1 status
#   powershell -ExecutionPolicy Bypass -File scripts\stream.ps1 stop
#   powershell -ExecutionPolicy Bypass -File scripts\stream.ps1 start
param(
    [ValidateSet("start", "stop", "status")][string]$Action = "status",
    [string]$Distro = "Ubuntu",
    [string]$TaskName = "PolyBot-Stream"
)

switch ($Action) {
    "start" {
        Enable-ScheduledTask -TaskName $TaskName | Out-Null
        Start-ScheduledTask -TaskName $TaskName
        Write-Host "Streamer starting. It reports 'connected' in scripts\health.ps1 within a few seconds."
    }
    "stop" {
        # Disable first, or the task's 5-minute trigger would start the streamer again.
        Disable-ScheduledTask -TaskName $TaskName | Out-Null
        # A polite signal: the streamer writes its last batch, marks itself stopped and exits.
        wsl.exe -d $Distro -- pkill -TERM -f "polybot.streamer"
        Start-Sleep -Seconds 3
        Stop-ScheduledTask -TaskName $TaskName
        Write-Host "Streamer stopped and its task disabled. Run this script with 'start' to bring it back."
    }
    "status" {
        $info = Get-ScheduledTask -TaskName $TaskName | Get-ScheduledTaskInfo
        Write-Host "Task $TaskName : $((Get-ScheduledTask -TaskName $TaskName).State), last started $($info.LastRunTime)"
        $running = wsl.exe -d $Distro -- pgrep -af "polybot.streamer"
        if ($running) { Write-Host "Process in WSL  : $running" } else { Write-Host "Process in WSL  : not running" }
    }
}
