param([switch]$Demo, [switch]$Software)
$ErrorActionPreference = 'Stop'
$guiDirectory = Split-Path -Parent $PSScriptRoot
$qtPython = Join-Path (Split-Path -Parent $guiDirectory) '.venv\qt\Scripts\pythonw.exe'
if (-not (Test-Path -LiteralPath $qtPython)) {
    throw 'Run desktop/install-qt.ps1 first.'
}
$launchArgs = @('"' + (Join-Path $guiDirectory 'forge_qt.py') + '"')
if ($Demo) { $launchArgs += '--demo' }
if ($Software) { $launchArgs += '--software' }
Start-Process -FilePath $qtPython -ArgumentList $launchArgs -WorkingDirectory $guiDirectory -WindowStyle Hidden
