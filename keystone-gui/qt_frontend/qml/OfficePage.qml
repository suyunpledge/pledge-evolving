import QtQuick
import QtQuick.Dialogs
import QtWebEngine
import QtWebChannel

Item {
    id: page
    property string currentWorkspace: workbench.workspace
    property bool ready: false
    readonly property url editorUrl: Qt.resolvedUrl("../office/index.html")
    onCurrentWorkspaceChanged: {
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
        function request(payload: string): string { return workbench.office.request(payload) }
        function pickFile() { picker.open() }
        function ask(doc: string, selection: string, prompt: string): string {
            if (controller.busy) return "当前任务仍在运行。"
            if (controller.draft.trim()) return "请先处理对话框中已有的草稿。"
            if (!prompt.trim()) return "请输入编辑要求。"
            controller.setConversationMode("work")
            controller.draft = (doc ? "在内置 Office 中编辑文件 " + doc + (selection ? "，当前选区 " + selection : "") + "。" : "在内置 Office 中创建文件。") + prompt
            controller.send()
            return controller.busy ? "" : (controller.error || "请检查当前模型和网关。")
        }
    }
    Connections {
        target: workbench.office
        function onChanged() { bridge.updated() }
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
        anchors.fill: parent
        backgroundColor: "#0d1118"
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
