<#
.SYNOPSIS
    Build the Revon Windows installer (Revon-Setup-<version>.exe).

.DESCRIPTION
    1. PyInstaller builds the runtime: Revon.exe + revon-python.exe sharing one folder
       (PySide6, QtWebEngine and the other wheels).
    2. The app's source is copied beside them into Revon\app (it runs from source).
    3. A smoke run of the frozen runtime checks that the kernel and GUI import.
    4. Inno Setup packs everything into one per-user installer.

    Needs: the Qt environment (.venv\qt, see install-qt.ps1) with PyInstaller,
    and Inno Setup 6 (ISCC.exe).

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File revon-gui\desktop\installer\build-installer.ps1
#>
param(
    [string]$PythonExe = '',
    [string]$Iscc = '',
    [switch]$SkipPyInstaller
)
$ErrorActionPreference = 'Stop'
$here = $PSScriptRoot
$gui = Split-Path -Parent (Split-Path -Parent $here)
$root = Split-Path -Parent $gui
$work = Join-Path $here 'build'
$dist = Join-Path $here 'dist'
$out = Join-Path $here 'output'
$bundle = Join-Path $dist 'Revon'

if (-not $PythonExe) {
    $PythonExe = Join-Path $root '.venv\qt\Scripts\python.exe'
    if (-not (Test-Path -LiteralPath $PythonExe)) {
        # A worktree has no .venv of its own: use the main checkout's.
        $common = (& git -C $root rev-parse --path-format=absolute --git-common-dir).Trim()
        $PythonExe = Join-Path (Split-Path -Parent $common) '.venv\qt\Scripts\python.exe'
    }
}
if (-not (Test-Path -LiteralPath $PythonExe)) { throw "Qt Python not found: $PythonExe (run install-qt.ps1)" }
if (-not $Iscc) {
    $Iscc = @("$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
              "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
              "$env:ProgramFiles\Inno Setup 6\ISCC.exe") | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
}
if (-not $Iscc) { throw 'Inno Setup 6 (ISCC.exe) not found. winget install JRSoftware.InnoSetup' }

$version = (Select-String -LiteralPath (Join-Path $root 'pyproject.toml') -Pattern '^version\s*=\s*"([^"]+)"').Matches[0].Groups[1].Value
$commit = (& git -C $root rev-parse --short HEAD).Trim()
Write-Output "Revon $version ($commit)"

# 1. Frozen runtime
if (-not $SkipPyInstaller) {
    & $PythonExe -m PyInstaller --noconfirm --log-level WARN --distpath $dist --workpath $work (Join-Path $here 'revon.spec')
    if ($LASTEXITCODE -ne 0) { throw 'PyInstaller failed.' }
}

# 2. App source, as plain files (no tests, caches or build output)
$app = Join-Path $bundle 'app'
if (Test-Path -LiteralPath $app) { Remove-Item -LiteralPath $app -Recurse -Force }
$excludeDirs = @('__pycache__', '.pytest_cache', 'tests', 'build', 'dist', 'build-source', 'output', 'node_modules', 'installer')
$excludeFiles = @('test_*.py', '*_test.py', 'conftest.py', '*.pyc', '.openclaw-tmp*', '*.spec', '.secret_store.json', '*-desktop.json', '*-diagnose.json')
foreach ($part in @('revon', 'revon-gui', 'bundles', 'templates')) {
    $source = Join-Path $root $part
    if (-not (Test-Path -LiteralPath $source)) { continue }
    & robocopy $source (Join-Path $app $part) /E /NFL /NDL /NJH /NJS /NP /XD @excludeDirs /XF @excludeFiles | Out-Null
    if ($LASTEXITCODE -ge 8) { throw "Copying $part failed ($LASTEXITCODE)." }
}
foreach ($file in @('run.py', 'pyproject.toml', 'LICENSE', 'README.md', 'README.zh.md')) {
    Copy-Item -LiteralPath (Join-Path $root $file) -Destination $app
}
Set-Content -LiteralPath (Join-Path $app 'BUILD.txt') -Value "Revon $version`ncommit $commit`nbuilt $(Get-Date -Format s)" -Encoding UTF8

# 3. Smoke: the frozen runtime must import the kernel and the GUI from app\
# Native stderr must not abort the script; the output is checked instead.
# (Windows PowerShell strips embedded double quotes from native arguments: use single quotes.)
$ErrorActionPreference = 'Continue'
$probe = "import revon, revon.cli, qt_frontend.app, qt_frontend.service_host, PySide6.QtWebEngineQuick; print('imports-ok')"
$result = & (Join-Path $bundle 'revon-python.exe') -c $probe 2>&1 | Out-String
if (-not ($result -match 'imports-ok')) { throw "Frozen runtime smoke failed:`n$result" }
$selftest = & (Join-Path $bundle 'revon-python.exe') (Join-Path $app 'run.py') selftest 2>&1 | Select-Object -Last 1
$ErrorActionPreference = 'Stop'
Write-Output "Frozen selftest: $selftest"
if (-not ($selftest -match 'checks passed')) { throw 'Frozen selftest did not pass.' }

# 4. Installer
$integration = Get-Content -LiteralPath (Join-Path (Split-Path -Parent $here) 'install-shell-integration.ps1') -Raw
$block = [regex]::Match($integration, '\$Extensions = @\(([\s\S]*?)\)').Groups[1].Value
$extensions = [regex]::Matches($block, "'(\.[^']+)'") | ForEach-Object { $_.Groups[1].Value }
$lines = foreach ($ext in $extensions) {
    "Root: HKCU; Subkey: ""Software\Classes\Applications\Revon.exe\SupportedTypes""; ValueType: string; ValueName: ""$ext""; ValueData: """"; Tasks: openwith"
    "Root: HKCU; Subkey: ""Software\Classes\$ext\OpenWithList\Revon.exe""; Flags: uninsdeletekey; Tasks: openwith"
}
Set-Content -LiteralPath (Join-Path $here 'openwith.iss') -Value $lines -Encoding UTF8
[IO.Directory]::CreateDirectory($out) | Out-Null
& $Iscc /Q "/DAppVersion=$version" "/DDistDir=$bundle" "/DOutputDir=$out" (Join-Path $here 'revon.iss')
if ($LASTEXITCODE -ne 0) { throw 'Inno Setup failed.' }
$setup = Join-Path $out "Revon-Setup-$version.exe"
$hash = (Get-FileHash -LiteralPath $setup -Algorithm SHA256).Hash
Set-Content -LiteralPath "$setup.sha256" -Value "$hash  Revon-Setup-$version.exe" -Encoding ASCII
Write-Output ("Installer: {0} ({1:N0} MB)" -f $setup, ((Get-Item -LiteralPath $setup).Length / 1MB))
Write-Output "SHA256: $hash"
