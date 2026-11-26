<#
.SYNOPSIS
    Register Revon with Windows Explorer for the current user (no admin needed).

.DESCRIPTION
    - "Open with" → Revon for common code and text files
    - Folder right-click (and folder background) → "Open with Revon"
    - Desktop shortcut that uses the current Revon icon

    Explorer caches icons by file path. So the icon is copied to a versioned
    file under %LOCALAPPDATA%\Revon\icons\revon-<hash>.ico; after an icon change,
    re-running this script switches to a new path and the old icon goes away.

    Opening a file switches Coding to that file's project (its Git root, or else
    its folder). If Revon is already running, the file opens in the existing window.

.EXAMPLE
    powershell -ExecutionPolicy Bypass -File install-shell-integration.ps1
    powershell -ExecutionPolicy Bypass -File install-shell-integration.ps1 -Uninstall
#>
param([switch]$Uninstall)
$ErrorActionPreference = 'Stop'

$AppKey = 'revon-desktop.exe'
$Classes = 'HKCU:\Software\Classes'
$Extensions = @(
    '.py', '.pyi', '.ipynb', '.js', '.mjs', '.cjs', '.jsx', '.ts', '.tsx', '.vue', '.svelte',
    '.json', '.jsonc', '.md', '.markdown', '.txt', '.log', '.csv', '.tsv',
    '.html', '.htm', '.css', '.scss', '.less', '.xml', '.svg',
    '.yaml', '.yml', '.toml', '.ini', '.cfg', '.conf', '.properties',
    '.c', '.h', '.cpp', '.cc', '.hpp', '.cs', '.java', '.kt', '.kts', '.go', '.rs', '.swift',
    '.rb', '.php', '.lua', '.dart', '.scala', '.r', '.sql', '.gradle',
    '.sh', '.bash', '.ps1', '.psm1', '.bat', '.cmd', '.qml', '.patch', '.diff'
)

Add-Type -Namespace Revon -Name Shell -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("shell32.dll")]
public static extern void SHChangeNotify(int eventId, uint flags, System.IntPtr item1, System.IntPtr item2);
'@
function Send-ShellRefresh { [Revon.Shell]::SHChangeNotify(0x08000000, 0, [IntPtr]::Zero, [IntPtr]::Zero) }

function Remove-Key([string]$Path) {
    if (Test-Path -LiteralPath $Path) { Remove-Item -LiteralPath $Path -Recurse -Force }
}

$folderKeys = @("$Classes\Directory\shell\Revon", "$Classes\Directory\Background\shell\Revon")

if ($Uninstall) {
    Remove-Key "$Classes\Applications\$AppKey"
    foreach ($ext in $Extensions) { Remove-Key "$Classes\$ext\OpenWithList\$AppKey" }
    foreach ($key in $folderKeys) { Remove-Key $key }
    Send-ShellRefresh
    Write-Output 'Removed Revon from "Open with" and the folder context menu (the desktop shortcut is kept).'
    return
}

$launcher = Join-Path $PSScriptRoot "dist\$AppKey"
if (-not (Test-Path -LiteralPath $launcher)) {
    throw "Launcher not found: $launcher. Run build-source-launcher.ps1 first."
}
$sourceIcon = Join-Path $PSScriptRoot 'revon.ico'
if (-not (Test-Path -LiteralPath $sourceIcon)) { throw "Icon not found: $sourceIcon" }

# Versioned icon copy: a new path makes Explorer drop the old cached icon.
$iconDir = Join-Path $env:LOCALAPPDATA 'Revon\icons'
[IO.Directory]::CreateDirectory($iconDir) | Out-Null
$hash = (Get-FileHash -LiteralPath $sourceIcon -Algorithm SHA256).Hash.Substring(0, 12).ToLower()
$icon = Join-Path $iconDir "revon-$hash.ico"
Copy-Item -LiteralPath $sourceIcon -Destination $icon -Force
Get-ChildItem -LiteralPath $iconDir -Filter 'revon-*.ico' |
    Where-Object { $_.FullName -ne $icon } | Remove-Item -Force -ErrorAction SilentlyContinue

$openCommand = "`"$launcher`" `"%1`""
$folderCommand = "`"$launcher`" `"%V`""

# Applications\revon-desktop.exe: what "Open with" shows for this program.
$app = "$Classes\Applications\$AppKey"
New-Item -Path "$app\shell\open\command" -Force | Out-Null
Set-ItemProperty -LiteralPath $app -Name 'FriendlyAppName' -Value 'Revon'
New-Item -Path "$app\DefaultIcon" -Force | Out-Null
Set-Item -LiteralPath "$app\DefaultIcon" -Value "$icon,0"
Set-Item -LiteralPath "$app\shell\open\command" -Value $openCommand
Set-ItemProperty -LiteralPath "$app\shell\open" -Name 'FriendlyAppName' -Value 'Revon'
Remove-Key "$app\SupportedTypes"
New-Item -Path "$app\SupportedTypes" -Force | Out-Null
foreach ($ext in $Extensions) {
    New-ItemProperty -LiteralPath "$app\SupportedTypes" -Name $ext -Value '' -PropertyType String -Force | Out-Null
    New-Item -Path "$Classes\$ext\OpenWithList\$AppKey" -Force | Out-Null
}

foreach ($key in $folderKeys) {
    New-Item -Path "$key\command" -Force | Out-Null
    Set-Item -LiteralPath $key -Value '用 Revon 打开'
    Set-ItemProperty -LiteralPath $key -Name 'Icon' -Value "$icon,0"
    Set-Item -LiteralPath "$key\command" -Value $folderCommand
}

# Desktop shortcut: update the existing one if it points at this launcher, else create Revon.lnk.
$shell = New-Object -ComObject WScript.Shell
$desktop = [Environment]::GetFolderPath('Desktop')
$shortcutPath = Get-ChildItem -LiteralPath $desktop -Filter *.lnk | Where-Object {
    $shell.CreateShortcut($_.FullName).TargetPath -eq $launcher
} | Select-Object -First 1 -ExpandProperty FullName
if (-not $shortcutPath) { $shortcutPath = Join-Path $desktop 'Revon.lnk' }
$shortcut = $shell.CreateShortcut($shortcutPath)
$shortcut.TargetPath = $launcher
$shortcut.WorkingDirectory = Split-Path -Parent $PSScriptRoot
$shortcut.IconLocation = "$icon,0"
$shortcut.Description = 'Revon'
$shortcut.Save()

Send-ShellRefresh
Write-Output "Launcher: $launcher"
Write-Output "Icon: $icon"
Write-Output "Shortcut: $shortcutPath"
Write-Output "Open with: $($Extensions.Count) file types, plus the folder context menu"
