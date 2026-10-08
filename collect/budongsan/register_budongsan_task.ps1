param(
    [string]$TaskName = 'nl2sql-budongsan-daily',
    [string]$PythonPath = '',
    [switch]$AllowIncompleteConfig
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent (Split-Path -Parent $PSScriptRoot)
if (-not $PythonPath) {
    $PythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$collector = Join-Path $projectRoot 'collect\budongsan\budongsan.py'
if ((Get-TimeZone).Id -ne 'Korea Standard Time') {
    throw 'This task uses local 06:00. Set the host timezone to Korea Standard Time before registration.'
}
if (-not $AllowIncompleteConfig) {
    & $PythonPath $collector --check-config
    if ($LASTEXITCODE -ne 0) { throw 'Complete backend/.env before registering the task.' }
}
# pythonw avoids opening a console; the collector writes logs/budongsan.log.
$windowlessPython = Join-Path (Split-Path -Parent $PythonPath) 'pythonw.exe'
if (-not (Test-Path -LiteralPath $windowlessPython)) {
    throw 'pythonw.exe is required next to python.exe for hidden scheduled execution.'
}
$action = New-ScheduledTaskAction -Execute $windowlessPython -Argument ('"{0}"' -f $collector) -WorkingDirectory $projectRoot
$trigger = New-ScheduledTaskTrigger -Daily -At '06:00'
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -WakeToRun -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Hours 12) `
    -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 15)
# Uses the current interactive session, without storing a Windows password.
$principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
    -LogonType Interactive -RunLevel Limited
$task = New-ScheduledTask -Action $action -Trigger $trigger -Settings $settings -Principal $principal `
    -Description 'Collect MOLIT apartment sales into PostgreSQL budongsan every day at 06:00 KST. Requires user login and backend/.env.'
Register-ScheduledTask -TaskName $TaskName -InputObject $task | Select-Object TaskName, State
