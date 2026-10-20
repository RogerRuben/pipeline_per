param([ValidateSet('win11', 'win7_client')][string]$Profile = 'win11')
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
$taskAssets = Join-Path $taskRoot 'assets'
if (-not (Test-Path -LiteralPath (Join-Path $taskAssets 'assets_manifest.json'))) {
    $taskAssets = Join-Path $taskRoot $(if ($Profile -eq 'win11') { 'runtime\tabpfn_offline_assets' } else { 'runtime\tabpfn_win7_assets' })
}
$taskRuntime = Join-Path $taskRoot $(if ($Profile -eq 'win11') { 'runtime\tabpfn311' } else { 'runtime\tabpfn38' })
$taskPython = Join-Path $taskRuntime 'python.exe'
$taskStem = if ($Profile -eq 'win11') { 'python311' } else { 'python38' }
if (-not (Test-Path -LiteralPath $taskPython) -or -not (Test-Path -LiteralPath (Join-Path $taskRuntime ($taskStem + '.dll'))) -or -not (Test-Path -LiteralPath (Join-Path $taskRuntime ($taskStem + '.zip')))) {
    $taskArchiveName = if ($Profile -eq 'win11') { 'python-3.11.9-embed-amd64.zip' } else { 'python-3.8.10-embed-amd64.zip' }
    $taskZip = Join-Path $taskAssets $taskArchiveName
    $taskExpected = [System.IO.File]::ReadAllText((Join-Path $taskAssets 'python_embed.sha256')).Trim()
    $taskHasher = [System.Security.Cryptography.SHA256]::Create()
    $taskStream = [System.IO.File]::OpenRead($taskZip)
    try { $taskActual = ([System.BitConverter]::ToString($taskHasher.ComputeHash($taskStream))).Replace('-', '').ToLower() }
    finally { $taskStream.Dispose(); $taskHasher.Dispose() }
    if (-not $taskExpected -or $taskActual -ne $taskExpected) {
        throw 'Embedded Python archive checksum mismatch.'
    }
    New-Item -ItemType Directory -Path $taskRuntime -Force | Out-Null
    if (Get-Command Expand-Archive -ErrorAction SilentlyContinue) {
        Expand-Archive -LiteralPath $taskZip -DestinationPath $taskRuntime -Force
    } else {
        $taskShell = New-Object -ComObject Shell.Application
        $taskSource = $taskShell.NameSpace($taskZip)
        $taskDestination = $taskShell.NameSpace($taskRuntime)
        if (-not $taskSource -or -not $taskDestination) { throw 'Cannot open the local Python archive.' }
        $taskDestination.CopyHere($taskSource.Items(), 20)
        $taskDeadline = (Get-Date).AddSeconds(45)
        do {
            $taskReady = $true
            foreach ($taskItem in $taskSource.Items()) {
                $taskTarget = Join-Path $taskRuntime $taskItem.Name
                if (-not (Test-Path -LiteralPath $taskTarget)) { $taskReady = $false; break }
                if ((Get-Item -LiteralPath $taskTarget).Length -ne $taskItem.Size) { $taskReady = $false; break }
            }
            if (-not $taskReady) { Start-Sleep -Milliseconds 100 }
        } while (-not $taskReady -and (Get-Date) -lt $taskDeadline)
        if (-not $taskReady) { throw 'Python archive extraction did not finish.' }
    }
}
& $taskPython (Join-Path $taskRoot 'tools\install_tabpfn_offline.py') --assets $taskAssets --runtime $taskRuntime --profile $Profile
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
