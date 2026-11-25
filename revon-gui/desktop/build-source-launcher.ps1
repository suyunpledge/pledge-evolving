param([string]$PythonExe = 'python')
$ErrorActionPreference = 'Stop'
if (-not $PythonExe) { $PythonExe = 'python' }
$guiDirectory = Split-Path -Parent $PSScriptRoot
$repoDirectory = Split-Path -Parent $guiDirectory
$distDirectory = Join-Path $PSScriptRoot 'dist'
$buildDirectory = Join-Path $PSScriptRoot 'build-source'
$buildId = [guid]::NewGuid().ToString('N')
$staging = Join-Path $buildDirectory "publish-$buildId"
& $PythonExe -m PyInstaller --noconfirm --distpath $staging --workpath $buildDirectory (Join-Path $PSScriptRoot 'revon-source.spec')
if ($LASTEXITCODE -ne 0) { throw 'Source launcher build failed.' }
$builtLauncher = Join-Path $staging 'revon-desktop.exe'
$config = @{ sourceRoot = $repoDirectory; kind = 'revon-source-launcher' } | ConvertTo-Json

function Publish-Launcher([string]$Directory) {
    [IO.Directory]::CreateDirectory($Directory) | Out-Null
    $launcher = Join-Path $Directory 'revon-desktop.exe'
    $legacy = Join-Path $Directory 'revon-desktop.legacy.exe'
    $pending = Join-Path $Directory "revon-desktop.$buildId.pending"
    $previous = Join-Path $Directory "revon-desktop.$buildId.previous"
    $manifest = Join-Path $Directory 'revon-source.json'
    $sourceFile = Join-Path $Directory "revon-source.$buildId.pending"
    if ((Test-Path -LiteralPath $launcher) -and -not (Test-Path -LiteralPath $legacy)) {
        Copy-Item -LiteralPath $launcher -Destination $legacy
    }
    Copy-Item -LiteralPath $builtLauncher -Destination $pending
    [IO.File]::WriteAllText($sourceFile, $config, [Text.UTF8Encoding]::new($false))
    $published = $false
    try {
        # Build first, then rename the existing image so a running launcher
        # can keep its mapped binary while the next launch picks up the update.
        if (Test-Path -LiteralPath $launcher) { [IO.File]::Move($launcher, $previous) }
        try {
            [IO.File]::Move($pending, $launcher)
            $published = $true
            if (Test-Path -LiteralPath $manifest) {
                [IO.File]::Replace($sourceFile, $manifest, [NullString]::Value)
            } else {
                [IO.File]::Move($sourceFile, $manifest)
            }
        } catch {
            if ($published) { [IO.File]::Move($launcher, $pending) }
            if (Test-Path -LiteralPath $previous) { [IO.File]::Move($previous, $launcher) }
            throw
        }
    } finally {
        [IO.File]::Delete($pending)
        [IO.File]::Delete($sourceFile)
    }
    if (Test-Path -LiteralPath $previous) {
        try { [IO.File]::Delete($previous) } catch [IO.IOException] {
            Write-Output "Previous launcher is still in use: $previous"
        } catch [UnauthorizedAccessException] {
            Write-Output "Previous launcher retained: $previous"
        }
    }
    Write-Output "Ready: $launcher (loads current source each time)"
}

Publish-Launcher $distDirectory
$oldDist = Join-Path $guiDirectory 'dist'
if (Test-Path -LiteralPath $oldDist) { Publish-Launcher $oldDist }
[IO.File]::Delete($builtLauncher)
[IO.Directory]::Delete($staging)
