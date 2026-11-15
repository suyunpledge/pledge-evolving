"""One GUI application for backend and scene-graph tests in the same process."""
import os
import sys

os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")

from PySide6.QtGui import QFont, QGuiApplication
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from PySide6.QtWebEngineQuick import QtWebEngineQuick

if sys.platform == "win32":
    QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)
QtWebEngineQuick.initialize()
APP = QGuiApplication.instance() or QGuiApplication([])
APP.setQuitOnLastWindowClosed(False)
FONT = QFont(["Segoe UI", "Microsoft YaHei UI", "Noto Sans SC"], 10)
APP.setFont(FONT)
