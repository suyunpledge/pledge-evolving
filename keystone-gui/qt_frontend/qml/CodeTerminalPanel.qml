import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: panel
    required property string resultText
    required property string tab
    signal tabRequested(string name)
    signal collapseRequested()
    signal resizeRequested(real pixels)
    readonly property bool terminal: tab === "terminal"
    readonly property bool canRun: workbench.permission !== "sandbox"
    readonly property string project: workbench.workspace
    property var history: []
    property int historyIndex: -1
    property string savedDraft: ""
    property bool follow: true
    color: "#1c1e22"
    onProjectChanged: if (input) { input.clear(); history = []; historyIndex = -1; savedDraft = "" }
    function focusPanel() {
        if (!visible) return
        if (terminal && canRun && !workbench.terminalRunning) input.forceActiveFocus()
        else transcript.forceActiveFocus()
    }
    function submit() {
        const value = input.text.trim()
        if (!value || !canRun || workbench.terminalRunning || input.inputMethodComposing) return
        if (history[history.length - 1] !== value) history = history.concat([value]).slice(-50)
        historyIndex = -1
        savedDraft = ""
        input.clear()
        follow = true
        workbench.terminalAction("run", value)
    }
    function recall(delta) {
        if (!history.length) return
        if (historyIndex < 0) {
            if (delta > 0) return
            savedDraft = input.text
            historyIndex = history.length
        }
        historyIndex = Math.max(0, Math.min(history.length, historyIndex + delta))
        input.text = historyIndex === history.length ? savedDraft : history[historyIndex]
        input.cursorPosition = input.length
        if (historyIndex === history.length) historyIndex = -1
    }
    function tail() { if (visible && follow) scroll.contentY = Math.max(0, scroll.contentHeight - scroll.height) }
    onVisibleChanged: if (visible) Qt.callLater(tail)
    onTabChanged: { follow = true; Qt.callLater(tail) }
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: 35; color: "#202328"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 5; spacing: 3
                Repeater {
                    model: [{key:"terminal", label:"终端"}, {key:"output", label:"输出"}]
                    IdeButton {
                        required property var modelData
                        objectName: "codeDockTab_" + modelData.key
                        text: modelData.label; tip: text; Layout.fillHeight: true
                        Rectangle {
                            anchors.bottom: parent.bottom; anchors.horizontalCenter: parent.horizontalCenter
                            width: parent.width - 14; height: 2; color: "#82b1f1"; visible: panel.tab === modelData.key
                        }
                        onClicked: { panel.tabRequested(modelData.key); Qt.callLater(panel.focusPanel) }
                    }
                }
                Item { Layout.fillWidth: true }
                Text {
                    visible: panel.terminal && panel.width > 420
                    text: workbench.terminalRunning ? "运行中" : (Qt.platform.os === "windows" ? "PowerShell" : "sh")
                    color: workbench.terminalRunning ? "#92c3aa" : "#a3afba"; font.pixelSize: 11
                }
                IdeButton {
                    visible: panel.terminal; objectName: "terminalGitStatus"; glyph: "git-branch"; tip: "查看 Git 状态"
                    enabled: !workbench.terminalRunning; onClicked: { panel.follow = true; workbench.terminalAction("git", "") }
                }
                IdeButton {
                    visible: panel.terminal && workbench.terminalRunning; glyph: "square"; tip: "停止命令"
                    onClicked: workbench.terminalAction("stop", "")
                }
                IdeButton { visible: panel.terminal; text: "清空"; tip: "清空终端输出"; enabled: !!workbench.terminalText; onClicked: workbench.terminalAction("clear", "") }
                IdeButton { glyph: "chevron-down"; tip: "收起面板"; onClicked: panel.collapseRequested() }
            }
        }
        Flickable {
            id: scroll; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; boundsBehavior: Flickable.StopAtBounds
            contentWidth: width; contentHeight: Math.max(height, transcript.implicitHeight)
            onMovementStarted: panel.follow = false
            onMovementEnded: panel.follow = atYEnd
            onContentHeightChanged: Qt.callLater(panel.tail)
            onHeightChanged: Qt.callLater(panel.tail)
            ScrollBar.vertical: ScrollBar { onPressedChanged: panel.follow = !pressed && scroll.atYEnd }
            TextArea {
                id: transcript; objectName: "codeTerminalTranscript"; width: scroll.width
                text: panel.terminal ? workbench.terminalText : panel.resultText
                placeholderText: panel.terminal ? panel.project + "\n等待命令或项目测试输出…" : "Git 变更和文件比较结果将显示在这里"
                textFormat: TextEdit.PlainText; wrapMode: TextEdit.WrapAnywhere; readOnly: true; selectByMouse: true
                leftPadding: 12; rightPadding: 12; topPadding: 7; bottomPadding: 6
                background: null; color: "#d2d9e3"; placeholderTextColor: "#919ba9"
                selectionColor: "#345477"; font.family: "Cascadia Mono"; font.pixelSize: 12
                Accessible.name: panel.terminal ? "终端输出" : "工作区输出"
            }
        }
        Rectangle {
            visible: panel.terminal; Layout.fillWidth: true; Layout.preferredHeight: 34; color: "#202328"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: 11; anchors.rightMargin: 5; spacing: 6
                Text { text: "›"; color: "#82b1f1"; font.pixelSize: 18 }
                TextField {
                    id: input; objectName: "codeTerminalInput"; Layout.fillWidth: true; Layout.fillHeight: true
                    Accessible.name: "终端命令"; color: "#e0e6ee"; placeholderTextColor: "#99a4b3"
                    placeholderText: !panel.canRun ? "沙箱运行 · 可查看输出和 Git 状态" : "在项目根目录运行命令"
                    readOnly: !panel.canRun; selectByMouse: true; font.family: "Cascadia Mono"; font.pixelSize: 12
                    background: null; leftPadding: 2; rightPadding: 2
                    onAccepted: panel.submit()
                    Keys.onUpPressed: event => { if (!inputMethodComposing) panel.recall(-1); else event.accepted = false }
                    Keys.onDownPressed: event => { if (!inputMethodComposing) panel.recall(1); else event.accepted = false }
                }
                IdeButton { visible: !panel.canRun; glyph: "shield"; tip: "切换执行权限"; onClicked: workbench.permissionRequested() }
                IdeButton {
                    visible: panel.canRun; glyph: "play"; tip: "运行命令"; enabled: !workbench.terminalRunning && !!input.text.trim()
                    onClicked: panel.submit()
                }
            }
        }
    }
    Rectangle { anchors.top: parent.top; width: parent.width; height: resizer.containsMouse || resizer.pressed ? 2 : 1; color: resizer.containsMouse || resizer.pressed ? "#74a5e4" : "#3a4049" }
    MouseArea {
        id: resizer; height: 5; width: parent.width; y: -2
        cursorShape: Qt.SplitVCursor; hoverEnabled: true
        property real originY
        property real originHeight
        onPressed: mouse => { originY = mapToGlobal(0, mouse.y).y; originHeight = panel.height }
        onPositionChanged: mouse => { if (pressed) panel.resizeRequested(originHeight + originY - mapToGlobal(0, mouse.y).y) }
    }
}
