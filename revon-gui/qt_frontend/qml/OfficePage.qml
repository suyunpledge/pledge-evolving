import QtQuick
import QtQuick.Window
import QtQuick.Dialogs
import QtWebEngine
import QtWebChannel

Item {
    id: page
    property string currentWorkspace: workbench.workspace
    property bool ready: false
    property bool deckOpen: false
    property bool projecting: false
    property int priorVisibility: Window.Windowed
    function project(active) {
        const win = page.Window.window
        if (!win || active === projecting) return
        if (active) {
            if (!deckOpen || !visible) return
            priorVisibility = win.visibility
            projecting = true
            win.showFullScreen()
            view.forceActiveFocus()
        } else {
            projecting = false
            if (win.visibility === Window.FullScreen) win.visibility = priorVisibility
            view.forceActiveFocus()
        }
    }
    function endProjection() {
        if (projecting) {
            project(false)
            view.runJavaScript("if (document.getElementById('presentation').open) document.getElementById('presentation').close()")
        }
    }
    onVisibleChanged: if (!visible) endProjection()
    Shortcut {
        sequence: "F5"
        enabled: page.visible && page.deckOpen && !page.projecting
        onActivated: view.runJavaScript("job(() => startSlideShow(false))")
    }
    Shortcut {
        sequence: "Shift+F5"
        enabled: page.visible && page.deckOpen && !page.projecting
        onActivated: view.runJavaScript("job(() => startSlideShow(true))")
    }
    readonly property bool chartViewActive: ready && visible && !!Window.window
        && Window.window.visible && Window.window.visibility !== Window.Minimized
    onChartViewActiveChanged: if (workbench && workbench.office) workbench.office.setChartViewActive(chartViewActive)
    Component.onDestruction: if (workbench && workbench.office) workbench.office.setChartViewActive(false)
    readonly property url editorUrl: Qt.resolvedUrl("../office/index.html")
    onCurrentWorkspaceChanged: {
        endProjection()
        deckOpen = false
        if (view && view.url.toString()) { ready = false; view.reload() }
    }
    WebChannel { id: channel; registeredObjects: [bridge] }
    QtObject {
        id: bridge
        WebChannel.id: "office"
        signal updated()
        signal imported(string result)
        signal agentStatus(string text)
        function initialized() { page.ready = true }
        function presentationOpened(active: bool) { page.deckOpen = active; if (!active) page.endProjection() }
        function setSlideShow(active: bool) { page.project(active) }
        function request(payload: string): string { return workbench.office.request(payload) }
        function pickFile() { picker.open() }
        function ask(doc: string, selection: string, prompt: string): string {
            if (controller.busy) return "当前任务仍在运行。"
            if (controller.draft.trim()) return "请先处理对话框中已有的草稿。"
            if (!prompt.trim()) return "请输入编辑要求。"
            controller.setConversationMode("work")
            const context = doc ? "内置 Office 文件 " + JSON.stringify({id: doc}) +
                (selection ? "，当前选区 " + selection : "") : "在内置 Office 中创建文件。"
            controller.draft = context + "\n" + prompt
            controller.send()
            return controller.busy ? "" : (controller.error || "请检查当前模型和网关。")
        }
    }
    Connections {
        target: workbench.office
        function onChanged() { bridge.updated() }
        function onInspectChart(token, docId, version, field, start) {
            if (!page.chartViewActive) { workbench.office.finishChartView(token, "null"); return }
            const args = [docId, version, field, start].map(v => JSON.stringify(v)).join(",")
            view.runJavaScript("JSON.stringify(window.inspectOfficeChart(" + args + "))", function(raw) {
                const layout = page.chartViewActive && raw ? JSON.parse(raw) : null
                if (layout) {
                    const origin = view.mapToGlobal(0, 0)
                    layout.desktop_transform = {
                        unit: "Qt desktop logical pixels (DIP)",
                        origin: [origin.x, origin.y], scale: view.zoomFactor,
                        mapping: "desktop = origin + viewport_css * scale"
                    }
                }
                workbench.office.finishChartView(token, JSON.stringify(layout))
            })
        }
    }
    Connections {
        target: controller
        function onChanged() { if (page.visible) bridge.agentStatus(controller.busy ? controller.status : controller.error || "") }
    }
    FileDialog {
        id: picker
        title: "导入为本地办公副本"
        nameFilters: ["办公文件 (*.docx *.xlsx *.pptx *.md *.txt *.csv)"]
        onAccepted: bridge.imported(workbench.office.importSelected(selectedFile))
    }
    WebEngineView {
        id: view
        objectName: "officeWebView"
        parent: page.projecting ? page.Window.window.contentItem : page
        z: page.projecting ? 10000 : 0
        anchors.fill: parent
        // Transparent so the window backdrop (aurora/starfield) shows behind the glass chrome.
        backgroundColor: "transparent"
        url: page.editorUrl
        webChannel: channel
        profile: WebEngineProfile { offTheRecord: true }
        settings.localContentCanAccessRemoteUrls: false
        settings.localContentCanAccessFileUrls: true
        settings.javascriptCanOpenWindows: false
        onNavigationRequested: function(request) {
            if (request.url.toString() !== page.editorUrl.toString()) request.reject()
        }
    }
}
