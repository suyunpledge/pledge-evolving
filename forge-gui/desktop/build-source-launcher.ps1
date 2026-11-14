param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) { $PythonExe = 'python' }
$guiDirectory = Split-Path -Parent $PSScriptRoot
$repoDirectory = Split-Path -Parent $guiDirectory
$distDirectory = Join-Path $PSScriptRoot 'dist'
$launcher = Join-Path $distDirectory 'forge-desktop.exe'
$backup = Join-Path $distDirectory 'forge-desktop.legacy.exe'
if ((Test-Path -LiteralPath $launcher) -and -not (Test-Path -LiteralPath $backup)) {
    Copy-Item -LiteralPath $launcher -Destination $backup
}
& $PythonExe -m PyInstaller --noconfirm --distpath $distDirectory --workpath (Join-Path $PSScriptRoot 'build-source') (Join-Path $PSScriptRoot 'forge-source.spec')
if ($LASTEXITCODE -ne 0) { throw 'Source launcher build failed.' }
$config = @{ sourceRoot = $repoDirectory; kind = 'forge-source-launcher' } | ConvertTo-Json
[IO.File]::WriteAllText((Join-Path $distDirectory 'forge-source.json'), $config, [Text.UTF8Encoding]::new($false))
$oldDist = Join-Path $guiDirectory 'dist'
if (Test-Path -LiteralPath $oldDist) {
    $oldLauncher = Join-Path $oldDist 'forge-desktop.exe'
    $oldBackup = Join-Path $oldDist 'forge-desktop.legacy.exe'
    if ((Test-Path -LiteralPath $oldLauncher) -and -not (Test-Path -LiteralPath $oldBackup)) {
        Copy-Item -LiteralPath $oldLauncher -Destination $oldBackup
    }
    Copy-Item -LiteralPath $launcher -Destination $oldLauncher -Force
    [IO.File]::WriteAllText((Join-Path $oldDist 'forge-source.json'), $config, [Text.UTF8Encoding]::new($false))
}
Write-Output "Ready: $launcher (loads current source each time)"
