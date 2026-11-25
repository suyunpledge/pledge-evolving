#!/usr/bin/env bash
# revon 一键配置（macOS / Linux）
# 唯一前提：能上网。缺 Python 会自动安装（系统包管理器需要 sudo 时会提示输一次密码）。
# 用法：bash setup.sh [gui|qt|web|check|selftest]
set -u
cd "$(dirname "$0")"

MODE="${1:-gui}"

echo "============================================================"
echo "  revon 一键配置（macOS / Linux）"
echo "  唯一前提：能上网。缺 Python 会自动安装。"
echo "  用法：bash setup.sh [gui/qt/web/check/selftest]"
echo "============================================================"
echo

PY=""
PY_CHECK='import sys;sys.exit(0 if sys.version_info >= (3,10) else 1)'
PY_REQUIRED='3.10+'
if [ "$MODE" = qt ]; then
  # matplotlib requires 3.11+, and the pinned PySide6 wheels require <3.15.
  PY_CHECK='import sys;sys.exit(0 if (3,11) <= sys.version_info < (3,15) else 1)'
  PY_REQUIRED='3.11–3.14'
fi

detect_python() {
  local c
  for c in python3 python3.12 python3.11 python3.13 python3.14; do
    command -v "$c" >/dev/null 2>&1 || continue
    if "$c" -c "$PY_CHECK" >/dev/null 2>&1; then
      PY="$(command -v "$c")"
      return 0
    fi
  done
  # Homebrew 常见直装路径（未进 PATH 的情况）
  local p
  for p in /opt/homebrew/bin/python3.12 /usr/local/bin/python3.12 /opt/homebrew/bin/python3.11 /usr/local/bin/python3.11; do
    if [ -x "$p" ] && "$p" -c "$PY_CHECK" >/dev/null 2>&1; then
      PY="$p"
      return 0
    fi
  done
  return 1
}

install_python() {
  local os
  local brew_packages=(python@3.12) deb_packages=(python3) rpm_packages=(python3) arch_packages=(python)
  if [ "$MODE" = qt ]; then
    deb_packages+=(python3-venv)
  else
    brew_packages+=(python-tk@3.12)
    deb_packages+=(python3-tk)
    rpm_packages+=(python3-tkinter)
    arch_packages+=(tk)
  fi
  os="$(uname -s)"
  if [ "$os" = "Darwin" ]; then
    if command -v brew >/dev/null 2>&1; then
      echo "      正在通过 Homebrew 安装 Python 3.12，可能需要几分钟..."
      brew install "${brew_packages[@]}" || return 1
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
    echo "      正在通过 apt 安装 Python（可能需要 sudo 密码）..."
    sudo apt-get update -qq && sudo apt-get install -y "${deb_packages[@]}"
    return $?
  fi
  if command -v dnf >/dev/null 2>&1; then
    echo "      正在通过 dnf 安装 Python（可能需要 sudo 密码）..."
    sudo dnf install -y "${rpm_packages[@]}"
    return $?
  fi
  if command -v yum >/dev/null 2>&1; then
    echo "      正在通过 yum 安装 Python（可能需要 sudo 密码）..."
    sudo yum install -y "${rpm_packages[@]}"
    return $?
  fi
  if command -v pacman >/dev/null 2>&1; then
    echo "      正在通过 pacman 安装 Python（可能需要 sudo 密码）..."
    sudo pacman -S --noconfirm "${arch_packages[@]}"
    return $?
  fi
  echo "[错误] 未识别的发行版（无 apt/dnf/yum/pacman）。请手动安装 Python 3.10+：https://www.python.org/downloads/"
  return 1
}

if ! detect_python; then
  echo "[1/4] 未检测到 Python $PY_REQUIRED，开始自动安装..."
  install_python || { echo "[错误] 请安装 Python $PY_REQUIRED 后重跑本脚本。"; exit 1; }
  detect_python || { echo "[错误] 请把 Python $PY_REQUIRED 加入 PATH 后重跑本脚本。"; exit 1; }
fi
echo "[1/4] Python 就绪："
"$PY" --version

if [ "$MODE" = qt ]; then
  shift
  QT_PY=$(cd revon-gui && "$PY" -c 'from pathlib import Path; from desktop.runtime_paths import qt_python; print(qt_python(Path.cwd().parent))') || exit 1
  if [ ! -x "$QT_PY" ]; then
    "$PY" -m venv "$(dirname "$(dirname "$QT_PY")")" || {
      echo "[错误] 无法创建 Qt 虚拟环境。Debian / Ubuntu 需要与 Python 版本匹配的 venv 软件包。"
      exit 1
    }
  fi
  "$QT_PY" -m pip --disable-pip-version-check install -r revon-gui/requirements-qt.txt || exit 1
  exec "$QT_PY" revon-gui/revon_qt.py "$@"
fi

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
  cat > "$APPS/revon.desktop" <<EOF 2>/dev/null || true
[Desktop Entry]
Type=Application
Name=revon
Exec=$PY $REPO/revon-gui/revon_gui_v2.py
Path=$REPO
Terminal=false
Categories=Development;
EOF
  [ -f "$APPS/revon.desktop" ] && echo "[3/4] 应用菜单已创建：revon（应用列表中可找到）" || echo "[3/4] 跳过快捷方式创建"
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

echo "[4/4] 启动 revon 桌面窗口..."
nohup "$PY" revon-gui/revon_gui_v2.py >/dev/null 2>&1 &
echo "已启动。窗口没出现就手动运行：python3 revon-gui/revon_gui_v2.py"
exit 0
