import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: picker
    required property string rootPath
    signal picked(var entry)
    property int ticket: 0
    property var rows: []
    property bool loading: false
    property bool acceptFirst: false
    property string failure: ""
    parent: Overlay.overlay
    x: Math.round((parent.width - width) / 2); y: 10
    width: Math.min(580, parent.width - 48)
    padding: 8; modal: true; dim: false; focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: Rectangle { color: "#181f2b"; border.color: "#35455e"; radius: 10 }
    function schedule() {
        ++ticket
        rows = []
        failure = ""
        acceptFirst = false
        loading = true
        debounce.restart()
    }
    function accept(index) {
        if (loading) { acceptFirst = true; return }
        if (index < 0 || index >= rows.length) return
        const row = rows[index]
        close()
        picked(row)
    }
    onOpened: { input.text = ""; input.forceActiveFocus(); schedule() }
    onClosed: { debounce.stop(); workbench.queryWorkspace("quick", "", ++ticket, true) }
    onRootPathChanged: if (opened) close()
    Timer { id: debounce; interval: 70; onTriggered: workbench.queryWorkspace("quick", input.text, picker.ticket, false) }
    Connections {
        target: workbench
        function onWorkspaceQueryResult(reply) {
            if (!picker.opened || reply.kind !== "quick" || reply.ticket !== picker.ticket || reply.root !== picker.rootPath) return
            picker.rows = reply.rows
            picker.failure = reply.error
            picker.loading = false
            choices.currentIndex = picker.rows.length ? 0 : -1
            if (picker.acceptFirst) picker.accept(choices.currentIndex)
        }
    }
    contentItem: ColumnLayout {
        spacing: 5
        TextField {
            id: input; objectName: "quickOpenInput"; Accessible.name: "快速打开文件"
            Layout.fillWidth: true; Layout.preferredHeight: 33
            placeholderText: "按名称或路径搜索文件"; color: "#e1e6ee"; placeholderTextColor: "#939eac"
            leftPadding: 10; selectByMouse: true
            background: Rectangle { radius: 6; color: "#111620"; border.color: "#5b9bf8" }
            onTextChanged: if (picker.opened) picker.schedule()
            Keys.onDownPressed: choices.currentIndex = Math.min(choices.count - 1, choices.currentIndex + 1)
            Keys.onUpPressed: choices.currentIndex = Math.max(0, choices.currentIndex - 1)
            onAccepted: picker.accept(choices.currentIndex)
        }
        Label {
            visible: !choices.count; Layout.fillWidth: true; leftPadding: 8; topPadding: 4; bottomPadding: 5
            text: picker.failure || (picker.loading ? "正在查找文件…" : "没有匹配的文件"); color: "#b4becb"
        }
        AppListView {
            id: choices; objectName: "quickOpenResults"; Layout.fillWidth: true
            Layout.preferredHeight: Math.min(count * 30, 300, picker.parent.height * 0.5)
            model: picker.rows; clip: true; highlightMoveDuration: 0
            delegate: ItemDelegate {
                id: option; required property int index; required property var modelData
                width: choices.rowWidth; height: 30; leftPadding: 8; rightPadding: 12; topPadding: 0; bottomPadding: 0
                Accessible.name: modelData.relative
                background: Rectangle { radius: 5; color: option.ListView.isCurrentItem ? "#2b4364" : option.hovered ? "#253042" : "transparent" }
                contentItem: RowLayout {
                    Glyph { name: "file-code"; ink: "#8db4db"; Layout.preferredWidth: 15; Layout.preferredHeight: 15 }
                    Text { text: option.modelData.name; color: "#e1e6ec"; font.pixelSize: 13; elide: Text.ElideRight; Layout.maximumWidth: choices.width * 0.55 }
                    Text { text: option.modelData.relative.replace(/[^/]+$/, ""); color: "#a2afbf"; font.pixelSize: 12; elide: Text.ElideMiddle; Layout.fillWidth: true }
                }
                onClicked: picker.accept(index)
            }
        }
    }
}
