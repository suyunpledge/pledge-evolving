"""Launch Qt Quick on the platform RHI; compile shaders during development only."""
import argparse
from functools import partial
import json
import os
from pathlib import Path
import sys
import tempfile

from PySide6.QtCore import QTimer, QUrl, Qt, qVersion, qInstallMessageHandler
from PySide6.QtGui import QFont, QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow, QSGRendererInterface
from .controller import ChatController
from .media_generation import tool_set as media_tool_set
from .workbench import WorkbenchController


def main(argv=None):
    parser = argparse.ArgumentParser(description="Forge Qt Quick desktop")
    parser.add_argument("--demo", action="store_true", help="isolated material preview; no model requests or writes")
    parser.add_argument("--home", type=Path, default=Path.home() / ".forge")
    parser.add_argument("--software", action="store_true", help="opaque compatibility mode without optical shaders")
    parser.add_argument("--smoke", type=Path, help="write renderer diagnostics and screenshot, then exit")
    parser.add_argument("--page", default="chat", help="initial integrated workbench page")
    parser.add_argument("--browser", default="", help="open URL in the browser side panel")
    args = parser.parse_args(argv)
    render_messages = []
    if args.smoke:
        os.environ["QSG_INFO"] = "1"
        qInstallMessageHandler(lambda _kind, _context, message: render_messages.append(str(message)))
    root = Path(__file__).resolve().parent
    if not args.software:
        missing = [p for p in (root / "shaders").glob("*.frag")
                   if not p.with_suffix(".frag.qsb").is_file()
                   or p.stat().st_mtime > p.with_suffix(".frag.qsb").stat().st_mtime]
        if missing:
            from .build_shaders import build
            build()
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    if args.software:
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Software)
    elif sys.platform == "win32" and not os.environ.get("QSG_RHI_BACKEND"):
        QQuickWindow.setGraphicsApi(QSGRendererInterface.GraphicsApi.Direct3D11)
    from PySide6.QtWebEngineQuick import QtWebEngineQuick
    QtWebEngineQuick.initialize()
    app = QGuiApplication(sys.argv[:1])
    app.setApplicationName("Forge")
    app.setOrganizationName("Forge")
    ui_font = QFont()
    ui_font.setFamilies(["Segoe UI", "Microsoft YaHei UI", "Noto Sans SC"])
    ui_font.setPointSize(10)
    ui_font.setStyleStrategy(QFont.StyleStrategy.ContextFontMerging)
    app.setFont(ui_font)
    icon = root.parent / "desktop" / "forge.ico"
    if icon.is_file():
        app.setWindowIcon(QIcon(str(icon)))
    controller = ChatController(args.home, demo=args.demo)
    if not args.demo:
        from .team_service import work_context
        controller.work_context = lambda messages, model, cancel, status: work_context(args.home, messages, model, cancel, status)
    preview_home = tempfile.TemporaryDirectory(prefix='forge-workbench-preview-', ignore_cleanup_errors=True) if args.demo else None
    workbench = WorkbenchController(Path(preview_home.name) if preview_home else args.home, demo=args.demo)
    workbench._input_busy = lambda: controller.busy
    from .permissions import authorize, tool_available
    def tool_authorizer():
        profile, workspace = workbench.permission, workbench.workspace
        return lambda name, arguments, cancel: authorize(
            profile, workspace, name, arguments, workbench.approve, cancel)
    controller.tool_authorizer = tool_authorizer
    controller.tool_filter = lambda: partial(tool_available, workbench.permission)

    def mcp_tools():
        workspace = workbench.workspace
        tools = {} if workbench.permission == 'sandbox' else workbench.mcp.chat_tools(workspace)
        if workbench.permission != 'sandbox':
            tools.update(workbench.service_tools())
        tools.update(workbench.localTools.tools())
        tools.update(media_tool_set())
        tools['forge_public_search'] = workbench.search.work_tool()
        skipped_media = {}

        def dispatch(target, arguments, cancel):
            if isinstance(target, str):
                return workbench.search.call_work_tool(target, arguments, cancel)
            if target[0] == 'media':
                kind = target[1]
                if kind in skipped_media:
                    return skipped_media[kind]
                response = workbench.mediaModels.call(kind, workspace, arguments, cancel)
                if response['meta'].get('error_code') == 'media_setup_skipped':
                    skipped_media[kind] = response
                return response
            if target[0] == 'local':
                return workbench.localTools.call(target[1], workspace, arguments, cancel)
            if target[0] == 'service':
                return workbench.call_service(target[1], arguments, cancel)
            return workbench.mcp.call_chat_tool(target, arguments, cancel)

        return tools, dispatch

    controller.mcp_tools = mcp_tools
    def chat_mcp_tools():
        tools = {} if workbench.permission == 'sandbox' else workbench.mcp.chat_tools(workbench.workspace, remote_only=True)
        return tools, workbench.mcp.call_chat_tool

    controller.chat_mcp_tools = chat_mcp_tools
    controller.navigateRequested.connect(workbench.navigate)
    controller.browseRequested.connect(workbench.openBrowser)
    workbench.gatewayChanged.connect(controller.setManagedGateway)
    workbench.gatewayChanging.connect(controller.gatewayChanging)
    engine = QQmlApplicationEngine()
    engine.rootContext().setContextProperty("controller", controller)
    engine.rootContext().setContextProperty("workbench", workbench)
    engine.rootContext().setContextProperty("smokeMode", bool(args.smoke))
    engine.rootContext().setContextProperty("uiFont", ui_font)
    engine.rootContext().setContextProperty("gpuEnabled", not args.software)
    engine.rootContext().setContextProperty("brandUrl", QUrl.fromLocalFile(str(root.parent / "assets" / "forge-logo-104.png")))
    warnings = []
    engine.warnings.connect(lambda items: warnings.extend(str(item) for item in items))
    engine.load(QUrl.fromLocalFile(str(root / "qml" / "Main.qml")))
    if not engine.rootObjects():
        print("\n".join(warnings), file=sys.stderr, flush=True)
        return 1
    window = engine.rootObjects()[0]
    if args.smoke:
        window.setFlag(Qt.WindowType.WindowDoesNotAcceptFocus, True)
        window.setPosition(-4000, -3000)
        window.show()
    app.aboutToQuit.connect(controller.shutdown)
    app.aboutToQuit.connect(workbench.shutdown)
    if not args.demo:
        QTimer.singleShot(250, workbench.start_host)
    if args.page != 'chat':
        QTimer.singleShot(100, lambda: workbench.navigate(args.page))
    if args.browser:
        QTimer.singleShot(150, lambda: workbench.openBrowser(args.browser))
    if args.smoke:
        def capture():
            args.smoke.parent.mkdir(parents=True, exist_ok=True)
            image = window.grabWindow()
            image.save(str(args.smoke.with_suffix(".png")))
            data = {"qt": qVersion(), "renderer": str(window.rendererInterface().graphicsApi()),
                    "source": str(root.parent), "title": window.title(),
                    "size": [window.width(), window.height()], "image": [image.width(), image.height()],
                    "qml_warnings": warnings, "render_messages": render_messages,
                    "workbench": {"page": workbench.page, "ready": workbench.nativePageReady,
                                  "error": workbench.error, "status": workbench.status}}
            args.smoke.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            if args.page == 'connectors':
                ready = workbench.page == 'connectors' and workbench.mcp.loaded
            else:
                ready = args.page in {'chat', 'connectors'} or workbench.nativePageReady
            app.exit(0 if not image.isNull() and not warnings and ready and not workbench.error else 2)
        QTimer.singleShot(20000 if not args.demo else 7000 if args.page != 'chat' or args.browser else 1800, capture)
    result = app.exec()
    if preview_home:
        preview_home.cleanup()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
