$ErrorActionPreference = 'Stop'
try {
    $taskRoot = $PSScriptRoot
    $dataDirectory = Join-Path $taskRoot 'data'
    $appPort = 8765
    $appUrl = "http://127.0.0.1:$appPort"
    try {
        $existing = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 2
        if ($existing.app -eq 'shiguang-diary') {
            $running = Invoke-RestMethod -Uri "$appUrl/api/bootstrap" -TimeoutSec 2
            if ([IO.Path]::GetFullPath($running.settings.data_dir).TrimEnd('\') -ne [IO.Path]::GetFullPath($dataDirectory).TrimEnd('\')) {
                throw 'Shiguang is using another data folder. Exit its local service in Settings, then start again.'
            }
            Start-Process $appUrl
            exit 0
        }
        throw 'Port 8765 is being used by another application.'
    } catch {
        if ($_.Exception.Message -like '*Port 8765*' -or $_.Exception.Message -like '*another data folder*') { throw }
    }
    $runtime = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if (-not $runtime) {
        $runtime = Get-Command python.exe -ErrorAction SilentlyContinue
    }
    if (-not $runtime) {
        throw 'Python 3.10 or newer is required. Please install Python and try again.'
    }
    $serverScript = Join-Path $taskRoot 'app\server.py'
    $arguments = @(('"{0}"' -f $serverScript), '--port', "$appPort", '--data-dir', ('"{0}"' -f $dataDirectory))
    $serviceProcess = Start-Process -FilePath $runtime.Source -ArgumentList $arguments -WorkingDirectory $taskRoot -WindowStyle Hidden -PassThru
    $ready = $false
    for ($attempt = 0; $attempt -lt 35; $attempt++) {
        try {
            $check = Invoke-RestMethod -Uri "$appUrl/api/health" -TimeoutSec 1
            if ($check.app -eq 'shiguang-diary') { $ready = $true; break }
        } catch { }
        if ($serviceProcess.HasExited) { break }
        Start-Sleep -Milliseconds 200
    }
    if (-not $ready) { throw 'Shiguang could not start. Check that port 8765 is free and the data folder is writable.' }
    Start-Process $appUrl
} catch {
    Add-Type -AssemblyName PresentationFramework
    [System.Windows.MessageBox]::Show($_.Exception.Message, 'Shiguang - Startup', 'OK', 'Error') | Out-Null
    exit 1
}
