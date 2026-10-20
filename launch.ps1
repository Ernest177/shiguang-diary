param([switch]$NoBrowser, [ValidateRange(1, 65535)][int]$PreferredPort = 8765)

$ErrorActionPreference = 'Stop'
$startupMutex = $null
$ownsMutex = $false

function Get-DiaryUrl([int]$Port, [string]$Directory) {
    if ($Port -lt 1 -or $Port -gt 65535) { return $null }
    $url = "http://127.0.0.1:$Port"
    try {
        $health = Invoke-RestMethod -Uri "$url/api/health" -TimeoutSec 2
        if ($health.app -ne 'shiguang-diary') { return $null }
        $running = Invoke-RestMethod -Uri "$url/api/bootstrap" -TimeoutSec 2
        if ([IO.Path]::GetFullPath($running.settings.data_dir).TrimEnd('\') -eq $Directory) { return $url }
    } catch { }
    return $null
}

function Get-DiaryRuntime([string]$Path) {
    try {
        $value = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
        if ($value.app -eq 'shiguang-diary' -and $value.port -ge 1 -and $value.port -le 65535) { return $value }
    } catch { }
    return $null
}

try {
    $taskRoot = $PSScriptRoot
    $dataDirectory = [IO.Path]::GetFullPath((Join-Path $taskRoot 'data')).TrimEnd('\')
    $runtimeFile = Join-Path $dataDirectory 'runtime.json'
    # Serialize double-clicks for this data folder; unrelated projects keep running.
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $folderHash = [BitConverter]::ToString($hasher.ComputeHash([Text.Encoding]::UTF8.GetBytes($dataDirectory.ToLowerInvariant()))).Replace('-', '') }
    finally { $hasher.Dispose() }
    $startupMutex = New-Object Threading.Mutex($false, "Local\ShiguangStartup-$folderHash")
    try { $ownsMutex = $startupMutex.WaitOne(20000) }
    catch [Threading.AbandonedMutexException] { $ownsMutex = $true }
    if (-not $ownsMutex) { throw 'Shiguang is still starting. Please try again shortly.' }

    $previous = Get-DiaryRuntime $runtimeFile
    $appUrl = $null
    if ($previous) { $appUrl = Get-DiaryUrl $previous.port $dataDirectory }
    if (-not $appUrl) { $appUrl = Get-DiaryUrl $PreferredPort $dataDirectory }
    if (-not $appUrl) {
        $startPort = $PreferredPort
        if ($previous) { $startPort = [int]$previous.port }
        $runtime = Get-Command pythonw.exe -ErrorAction SilentlyContinue
        if (-not $runtime) { $runtime = Get-Command python.exe -ErrorAction SilentlyContinue }
        if (-not $runtime) { throw 'Python 3.10 or newer is required. Please install Python and try again.' }
        $serverScript = Join-Path $taskRoot 'app\server.py'
        $arguments = @(('"{0}"' -f $serverScript), '--port', "$startPort", '--fallback-port', '--data-dir', ('"{0}"' -f $dataDirectory))
        $serviceProcess = Start-Process -FilePath $runtime.Source -ArgumentList $arguments -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru
        for ($attempt = 0; $attempt -lt 50; $attempt++) {
            $current = Get-DiaryRuntime $runtimeFile
            if ($current -and $current.pid -eq $serviceProcess.Id) {
                $appUrl = Get-DiaryUrl $current.port $dataDirectory
                if ($appUrl) { break }
            }
            if ($serviceProcess.HasExited) { break }
            Start-Sleep -Milliseconds 200
        }
        if (-not $appUrl) { throw 'Shiguang could not start. Other applications were left running. Check Python and write access to the data folder.' }
    }
    if ($NoBrowser) { @{ url = $appUrl } | ConvertTo-Json -Compress }
    else { Start-Process $appUrl }
} catch {
    if ($NoBrowser) { [Console]::Error.WriteLine($_.Exception.Message) }
    else {
        Add-Type -AssemblyName PresentationFramework
        [System.Windows.MessageBox]::Show($_.Exception.Message, 'Shiguang - Startup', 'OK', 'Error') | Out-Null
    }
    exit 1
} finally {
    if ($ownsMutex) { $startupMutex.ReleaseMutex() }
    if ($startupMutex) { $startupMutex.Dispose() }
}
