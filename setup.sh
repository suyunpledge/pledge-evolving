#!/usr/bin/env bash
# Forge 一键配置（macOS / Linux）
# 唯一前提：能上网。缺 Python 会自动安装（系统包管理器需要 sudo 时会提示输一次密码）。
# 用法：./setup.sh [gui|web|check|selftest]
set -u
cd "$(dirname "$0")"

MODE="${1:-gui}"

echo "============================================================"
echo "  Forge 一键配置（macOS / Linux）"
echo "  唯一前提：能上网。缺 Python 会自动安装。"
echo "  用法：./setup.sh [gui/web/check/selftest]"
echo "============================================================"
echo

PY=""

detect_python() {
  local c
  for c in python3 python3.12 python3.11 python3.13; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' >/dev/null 2>&1; then
      PY="$(command -v "$c")"
      return 0
    fi
  done
  # Homebrew 常见直装路径（未进 PATH 的情况）
  local p
  for p in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11; do
    if [ -x "$p" ] && "$p" -c 'import sys;sys.exit(0 if sys.version_info>=(3,10) else 1)' >/dev/null 2>&1; then
      PY="$p"
      return 0
    fi
  done
  return 1
}

install_python() {
  local os
  os="$(uname -s)"
  if [ "$os" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      echo "      正在通过 Homebrew 安装 Python 3.12（含 tkinter），可能需要几分钟..."
      brew install python@3.12 python-tk@3.12 || return 1
      return 0
    fi
    echo "[提示] 此 Mac 没有 Homebrew。两条路："
    echo "  A) 装 Homebrew 后重跑本脚本："
    echo '     /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo "  B) 直接用 python.org 官方安装器：https://www.python.org/downloads/"
    return 1
  fi
  # Linux 发行版
  if command -v apt-get >/dev/null 2>&1; then
    echo "      正在通过 apt 安装 python3 + tkinter（需要输一次 sudo 密码）..."
    sudo apt-get update -qq && sudo apt-get install -y python3 python3-tk
    return $?
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "      正在通过 dnf 安装 python3 + tkinter（需要输一次 sudo 密码）..."
    sudo dnf install -y python3 python3-tkinter
    return $?
  fi
  if command -v yum >/dev/null 2>&1; then
    echo "      正在通过 yum 安装 python3 + tkinter（需要输一次 sudo 密码）..."
    sudo yum install -y python3 python3-tkinter
    return $?
  fi
  if command -v pacman >/dev/null 2>&1; then
    echo "      正在通过 pacman 安装 python + tk（需要输一次 sudo 密码）..."
    sudo pacman -S --noconfirm python tk
    return $?
  fi
  echo "[错误] 未识别的发行版（无 apt/dnf/yum/pacman）。请手动安装 Python 3.10+：https://www.python.org/downloads/"
  return 1
}

if ! detect_python; then
  echo "[1/4] 未检测到 Python 3.10+，开始自动安装..."
  install_python || { echo; echo "[错误] Python 3.10+ 安装失败。请手动安装后重跑本脚本。"; exit 1; }
  detect_python || { echo; echo "[错误] 安装后仍未探测到 Python 3.10+（可能需要重开终端刷新 PATH）。"; exit 1; }
fi
echo "[1/4] Python 就绪："
"$PY" --version

HAS_TK=0
"$PY" -c "import tkinter" >/dev/null 2>&1 && HAS_TK=1
if [ "$HAS_TK" = "1" ]; then
  echo "[2/4] tkinter 就绪，桌面 GUI 可用"
else
  echo "[2/4] tkinter 缺失：GUI 不可用，将自动转 Web 模式"
fi

case "$MODE" in
  check)
    echo "[4/4] 环境检查完成，未做安装以外的任何改动。"
    exit 0
    ;;
  selftest)
    echo "[3/4] 运行离线自检（无需网络与密钥）..."
    exec "$PY" run.py selftest
    ;;
esac

# 桌面快捷方式（Linux XDG；macOS 无此概念，跳过）
if [ "$(uname -s)" = "Linux" ] && command -v mkdir >/dev/null 2>&1; then
  APPS="$HOME/.local/share/applications"
  mkdir -p "$APPS" 2>/dev/null
  REPO="$(pwd)"
  cat > "$APPS/forge.desktop" <<EOF 2>/dev/null || true
[Desktop Entry]
Type=Application
Name=Forge
Exec=$PY $REPO/forge-gui/forge_gui_v2.py
Path=$REPO
Terminal=false
Categories=Development;
EOF
  [ -f "$APPS/forge.desktop" ] && echo "[3/4] 应用菜单已创建：Forge（应用列表中可找到）" || echo "[3/4] 跳过快捷方式创建"
else
  echo "[3/4] 跳过快捷方式创建（macOS 无需）"
fi

open_url() {
  if command -v xdg-open >/dev/null 2>&1; then xdg-open "$1" >/dev/null 2>&1
  elif command -v open >/dev/null 2>&1; then open "$1"
  fi
}

if [ "$MODE" = "web" ] || [ "$HAS_TK" = "0" ]; then
  echo "[4/4] 启动 Web 客户端，浏览器将自动打开 http://127.0.0.1:7712 ..."
  nohup "$PY" client/server.py >/dev/null 2>&1 &
  sleep 2
  open_url "http://127.0.0.1:7712"
  echo "已启动（后台进程，日志静默）。浏览器没弹就手动访问 http://127.0.0.1:7712"
  exit 0
fi

echo "[4/4] 启动 Forge 桌面窗口..."
nohup "$PY" forge-gui/forge_gui_v2.py >/dev/null 2>&1 &
echo "已启动。窗口没出现就手动运行：python3 forge-gui/forge_gui_v2.py"
exit 0
