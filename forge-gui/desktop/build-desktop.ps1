<#
forge-gui 桌面应用一键构建

用法（在 forge-gui/ 目录下）：
    powershell -NoProfile -ExecutionPolicy Bypass -File desktop\build-desktop.ps1
可选参数：
    -PythonExe <路径>   指定构建用的 Python（默认自动探测，需带 tkinter）
    -SkipIcon           跳过图标生成（沿用已有 desktop\forge.ico）
    -Clean              打包前清掉 desktop\build 与 desktop\dist
#>
param(
    [string]$PythonExe = "",
    [switch]$SkipIcon,
    [switch]$Clean
)

$ErrorActionPreference = "Stop"
$DesktopDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$GuiDir = Split-Path -Parent $DesktopDir

function Find-BuildPython {
    param([string]$Explicit)
    if ($Explicit -and (Test-Path $Explicit)) { return $Explicit }
    $candidates = @()
    if ($env:FORGE_BUILD_PYTHON) { $candidates += $env:FORGE_BUILD_PYTHON }
    $bases = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python"),
        "C:\"
    )
    foreach ($base in $bases) {
        if (-not (Test-Path $base)) { continue }
        Get-ChildItem $base -Directory -Filter "Python3*" -ErrorAction SilentlyContinue |
            Sort-Object Name -Descending | ForEach-Object {
                $candidates += (Join-Path $_.FullName "python.exe")
            }
    }
    $candidates += @("python", "python3")
    foreach ($cand in $candidates) {
        if (-not $cand) { continue }
        $exe = $cand
        if (-not (Test-Path $exe)) {
            $cmd = Get-Command $cand -ErrorAction SilentlyContinue
            if (-not $cmd) { continue }
            $exe = $cmd.Source
        }
        # 必须能用 tkinter 且装了 PyInstaller
        $ok = & $exe -c "import tkinter, PIL; import PyInstaller" 2>$null
        if ($LASTEXITCODE -eq 0) { return $exe }
    }
    throw "找不到可用的构建 Python（需要带 tkinter 的官方 Python + pillow + pyinstaller）"
}

$py = Find-BuildPython -Explicit $PythonExe
Write-Host "构建 Python: $py" -ForegroundColor Cyan
& $py -c "import sys; print('  version:', sys.version.split()[0])"

if (-not $SkipIcon) {
    Write-Host "`n[1/3] 生成图标 ..." -ForegroundColor Cyan
    & $py (Join-Path $DesktopDir "make_icon.py") (Join-Path $DesktopDir "forge.ico")
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }
} else {
    Write-Host "`n[1/3] 跳过图标生成" -ForegroundColor DarkGray
}

if ($Clean) {
    Remove-Item (Join-Path $DesktopDir "build") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DesktopDir "dist") -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n[2/3] PyInstaller 打包 ..." -ForegroundColor Cyan
Push-Location $GuiDir
try {
    & $py -m PyInstaller --noconfirm --clean `
        --distpath (Join-Path $DesktopDir "dist") `
        --workpath (Join-Path $DesktopDir "build") `
        (Join-Path $DesktopDir "forge-desktop.spec")
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller 失败（退出码 $LASTEXITCODE）" }
} finally {
    Pop-Location
}

Write-Host "`n[3/3] 校验产物 ..." -ForegroundColor Cyan
$exe = Join-Path $DesktopDir "dist\forge-desktop.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host "  产物: $exe"
Write-Host "  大小: $size MB"
Write-Host "  SHA256: $hash"

# 冒烟：启动 → 等窗口 → 关掉
Write-Host "`n  冒烟启动 ..." -ForegroundColor DarkGray
$proc = Start-Process -FilePath $exe -PassThru
$title = ""
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 2
    $main = Get-Process -Name forge-desktop -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowTitle } | Select-Object -First 1
    if ($main) { $title = $main.MainWindowTitle; break }
    if ($proc.HasExited) { break }
}
Get-Process -Name forge-desktop -ErrorAction SilentlyContinue | Stop-Process -Force
if ($title) {
    Write-Host "  冒烟通过：窗口标题 = $title" -ForegroundColor Green
} else {
    throw "冒烟失败：等不到窗口标题"
}
Write-Host "`n构建完成。" -ForegroundColor Green
