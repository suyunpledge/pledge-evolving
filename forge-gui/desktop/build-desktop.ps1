<#
forge-gui 桌面应用一键构建

用法（在 forge-gui/ 目录下）：
    powershell -NoProfile -ExecutionPolicy Bypass -File desktop\build-desktop.ps1
可选参数：
    -PythonExe <路径>   指定构建用的 Python（默认自动探测，需带 tkinter）
    -SkipIcon           跳过图标生成（沿用已有 desktop\forge.ico）
    -SkipBrandMarks     跳过模型品牌标志生成（沿用已有 assets\brands）
    -Clean              打包前清掉 desktop\build 与 desktop\dist
#>
param(
    [string]$PythonExe = "",
    [switch]$SkipIcon,
    [switch]$SkipBrandMarks,
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
    Write-Host "`n[1/4] 生成图标 ..." -ForegroundColor Cyan
    # 注意：不要传 forge.ico 作参数——那是生成产物，会被当成输入图导致降质
    & $py (Join-Path $DesktopDir "make_icon.py")
    if ($LASTEXITCODE -ne 0) { throw "图标生成失败" }
} else {
    Write-Host "`n[1/4] 跳过图标生成" -ForegroundColor DarkGray
}

if (-not $SkipBrandMarks) {
    Write-Host "`n[2/4] 生成模型品牌标志 ..." -ForegroundColor Cyan
    # 源图在 assets\brands-src（随仓库提交），产物在 assets\brands
    & $py (Join-Path $DesktopDir "make_brand_marks.py")
    if ($LASTEXITCODE -ne 0) { throw "品牌标志生成失败" }
} else {
    Write-Host "`n[2/4] 跳过品牌标志生成" -ForegroundColor DarkGray
}

Write-Host "`n生成功能图标与表情资源 ..." -ForegroundColor Cyan
& $py (Join-Path $DesktopDir "make_ui_assets.py")
if ($LASTEXITCODE -ne 0) { throw "功能图标与表情资源生成失败" }

if ($Clean) {
    Remove-Item (Join-Path $DesktopDir "build") -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item (Join-Path $DesktopDir "dist") -Recurse -Force -ErrorAction SilentlyContinue
}

Write-Host "`n[3/4] PyInstaller 打包 ..." -ForegroundColor Cyan
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

Write-Host "`n[4/4] 校验产物 ..." -ForegroundColor Cyan
$exe = Join-Path $DesktopDir "dist\forge-desktop.exe"
if (-not (Test-Path $exe)) { throw "没有生成 $exe" }
$size = [math]::Round((Get-Item $exe).Length / 1MB, 2)
$hash = (Get-FileHash $exe -Algorithm SHA256).Hash
Write-Host "  产物: $exe"
Write-Host "  大小: $size MB"
Write-Host "  SHA256: $hash"

# 冒烟：启动 → 等窗口 → 关掉。
# FORGE_NO_AUTOSTART=1：冒烟只需要窗口标题，别拉起真 gateway——
# 后面是 Stop-Process -Force，强杀不会走 _on_close，留下的孤儿 gateway
# 会占着 8799，下次启动时表现成「gateway 起不来」。
Write-Host "`n  冒烟启动（不拉 gateway）..." -ForegroundColor DarkGray

# 记录冒烟前已经存在的 gateway 进程，收尾时只清掉「本次冒烟新增的」。
# 旧写法把所有命令行含 run.py+gateway 的 python 进程全杀掉，
# 会误伤其他项目（或其他场景）正在跑的 gateway。
function Get-GatewayPids {
    @(Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*run.py*gateway*" } |
        Select-Object -ExpandProperty ProcessId)
}
$gwBefore = Get-GatewayPids
$desktopBefore = @(
    Get-Process -Name forge-desktop -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exe } |
        Select-Object -ExpandProperty Id
)

$env:FORGE_NO_AUTOSTART = '1'
$proc = Start-Process -FilePath $exe -PassThru -WindowStyle Hidden
$title = ""
for ($i = 0; $i -lt 10; $i++) {
    Start-Sleep -Seconds 2
    $main = Get-Process -Name forge-desktop -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exe -and $desktopBefore -notcontains $_.Id -and
                       $_.MainWindowTitle -like "forge *" } | Select-Object -First 1
    if ($main) { $title = $main.MainWindowTitle; break }
    if ($proc.HasExited) { break }
}
# PyInstaller one-file 会先启动一个很快退出的引导 PID，再派生真正的 GUI PID。
# 因此按「构建前后新增 PID + 精确 exe 路径」回收；不能按进程名清场，避免
# 把用户从桌面或其它构建目录启动的 Forge 一并强制退出。
$smokePids = @(
    Get-Process -Name forge-desktop -ErrorAction SilentlyContinue |
        Where-Object { $_.Path -eq $exe -and $desktopBefore -notcontains $_.Id } |
        Select-Object -ExpandProperty Id
)
foreach ($smokePid in $smokePids) {
    Stop-Process -Id $smokePid -Force -ErrorAction SilentlyContinue
    Wait-Process -Id $smokePid -ErrorAction SilentlyContinue
}
Remove-Item Env:\FORGE_NO_AUTOSTART -ErrorAction SilentlyContinue
# 只收掉本次冒烟新增的 gateway；之前就在跑的一律不动
# 注意：不能用 $pid 作循环变量——PowerShell 里它是只读自动变量（当前进程 ID），
# 赋值会报错并让整个 taskkill 默默不执行。
$stray = @(Get-GatewayPids | Where-Object { $gwBefore -notcontains $_ })
foreach ($gatewayPid in $stray) {
    & taskkill /T /F /PID $gatewayPid 2>$null | Out-Null
}
if ($stray.Count -gt 0) {
    Write-Host "  已清理本次冒烟产生的 gateway：$($stray -join ', ')" -ForegroundColor DarkGray
}
if ($title) {
    Write-Host "  冒烟通过：窗口标题 = $title" -ForegroundColor Green
} else {
    throw "冒烟失败：等不到窗口标题"
}
Write-Host "`n构建完成。" -ForegroundColor Green
