param([string]$DesktopDirectory = [Environment]::GetFolderPath('Desktop'))

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
$taskRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot -Parent))
$desktopRoot = [IO.Path]::GetFullPath($DesktopDirectory)
$launcher = Join-Path $taskRoot 'launch.ps1'
# Decode product labels from JSON so Windows PowerShell 5 does not depend on
# the source file's code page. Runtime launch.ps1 itself remains ASCII too.
$shortcutName = '"\u62fe\u5149\u65e5\u8bb0.lnk"' | ConvertFrom-Json
$legacyName = '"\u542f\u52a8\u62fe\u5149.vbs"' | ConvertFrom-Json
$legacyLauncher = Join-Path $taskRoot $legacyName
$shortcutPath = Join-Path $desktopRoot $shortcutName
$powershellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$launchArguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}"' -f $launcher
$identityPath = Join-Path $taskRoot 'data\desktop-entry-id.json'
$entryId = [Guid]::NewGuid().ToString('N')
if (Test-Path -LiteralPath $identityPath) {
    $identity = Get-Content -LiteralPath $identityPath -Raw | ConvertFrom-Json
    $entryId = ([Guid]::Parse($identity.id)).ToString('N')
}
$entryDescription = 'Shiguang Diary - ' + $entryId

function Test-ManagedShortcut($Shortcut) {
    return ($Shortcut.TargetPath -eq $legacyLauncher) -or
        ($Shortcut.TargetPath -eq $powershellPath -and ($Shortcut.Arguments -eq $launchArguments -or $Shortcut.Description -eq $entryDescription))
}

try {
    if (-not (Test-Path -LiteralPath $launcher -PathType Leaf)) { throw 'The Shiguang launcher was not found beside this installer.' }
    if (-not (Test-Path -LiteralPath $desktopRoot -PathType Container)) { throw 'The desktop folder does not exist.' }
    $shell = New-Object -ComObject WScript.Shell
    $existing = $null
    if (Test-Path -LiteralPath $shortcutPath) {
        $existing = $shell.CreateShortcut($shortcutPath)
        if (-not (Test-ManagedShortcut $existing)) { throw 'A shortcut with this name belongs to another application. It was not replaced.' }
    }

    $legacyPaths = @()
    foreach ($file in Get-ChildItem -LiteralPath $desktopRoot -Filter '*.lnk') {
        if ($file.FullName -eq $shortcutPath) { continue }
        if (-not $file.BaseName.StartsWith([IO.Path]::GetFileNameWithoutExtension($legacyName))) { continue }
        try { $candidate = $shell.CreateShortcut($file.FullName) } catch { continue }
        if ($candidate.TargetPath -eq $legacyLauncher) { $legacyPaths += $file.FullName }
    }
    $replaceCurrent = $existing -and ($existing.TargetPath -ne $powershellPath -or $existing.Arguments -ne $launchArguments -or $existing.WorkingDirectory -ne $taskRoot -or $existing.Description -ne $entryDescription)
    if ($replaceCurrent -or $legacyPaths.Count) {
        $backupRoot = Join-Path $taskRoot ('data\launcher-backups\' + [DateTime]::Now.ToString('yyyyMMdd-HHmmss-fff'))
        $null = New-Item -Path $backupRoot -ItemType Directory -Force
        if ($replaceCurrent) { Copy-Item -LiteralPath $shortcutPath -Destination $backupRoot }
        foreach ($legacyPath in $legacyPaths) { Copy-Item -LiteralPath $legacyPath -Destination $backupRoot }
    }

    if (-not (Test-Path -LiteralPath $identityPath)) {
        $null = New-Item -Path (Split-Path $identityPath) -ItemType Directory -Force
        @{ id = $entryId } | ConvertTo-Json -Compress | Set-Content -LiteralPath $identityPath -Encoding Ascii
    }
    if (-not $existing -or $replaceCurrent) {
        $shortcut = $shell.CreateShortcut($shortcutPath)
        $shortcut.TargetPath = $powershellPath
        $shortcut.Arguments = $launchArguments
        $shortcut.WorkingDirectory = $taskRoot
        $shortcut.WindowStyle = 7
        $shortcut.Description = $entryDescription
        $shortcut.Save()
    }
    $saved = $shell.CreateShortcut($shortcutPath)
    if ($saved.TargetPath -ne $powershellPath -or $saved.Arguments -ne $launchArguments) { throw 'The shortcut could not be verified.' }
    foreach ($legacyPath in $legacyPaths) {
        if ([IO.Path]::GetDirectoryName([IO.Path]::GetFullPath($legacyPath)) -ne $desktopRoot) { throw 'Unexpected shortcut location.' }
        Remove-Item -LiteralPath $legacyPath
    }
    @{ installed = $true; shortcut = $shortcutPath; replaced = $legacyPaths.Count } | ConvertTo-Json -Compress
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
