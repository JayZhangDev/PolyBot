# Shows PolyBot's health from Windows by running the check inside WSL, where the bot itself runs.
#
#   powershell -ExecutionPolicy Bypass -File scripts\health.ps1
param(
    [string]$Distro = "Ubuntu",
    [string]$WslProject = "/mnt/c/Users/$env:USERNAME/PolyBot",
    [string]$Venv = '$HOME/venvs/polybot/bin/python'
)
wsl.exe -d $Distro -- bash -lc "cd $WslProject && $Venv -m polybot health"
exit $LASTEXITCODE