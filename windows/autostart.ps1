param([switch]$Remove)
$ErrorActionPreference = 'Stop'
$remoteRoot = $PSScriptRoot
$startupFolder = [Environment]::GetFolderPath('Startup')
$shortcutPath = Join-Path $startupFolder 'Orbit Bridge.lnk'
$pythonPath = Join-Path $remoteRoot '.venv\Scripts\pythonw.exe'
if ($Remove) {
    if (Test-Path -LiteralPath $shortcutPath) {
        $shellObject = New-Object -ComObject WScript.Shell
        $existingShortcut = $shellObject.CreateShortcut($shortcutPath)
        if ($existingShortcut.Arguments -ne ('"' + (Join-Path $remoteRoot 'background.py') + '" --run')) {
            throw 'Startup belongs to another folder. Run REMOVE_AUTO_START.bat in that folder.'
        }
        Remove-Item -LiteralPath $shortcutPath
    }
    Write-Host 'Automatic startup removed. Use STOP.bat to stop the running remote.'
    exit 0
}
if (!(Test-Path -LiteralPath $pythonPath)) { throw 'Run START.bat first.' }
$shellObject = New-Object -ComObject WScript.Shell
$shortcut = $shellObject.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $pythonPath
$shortcut.Arguments = '"' + (Join-Path $remoteRoot 'background.py') + '" --run'
$shortcut.WorkingDirectory = $remoteRoot
$shortcut.WindowStyle = 7
$shortcut.Description = 'Orbit bridge background runner (current user)'
$shortcut.Save()
Write-Host 'Enabled. Remote starts silently after you sign in to Windows.'
Write-Host 'Keep this folder in its current location.'
