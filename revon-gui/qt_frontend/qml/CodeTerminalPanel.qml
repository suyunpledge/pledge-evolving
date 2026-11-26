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
    signal problemPicked(var entry)
    readonly property bool terminal: tab === "terminal"
    readonly property bool listing: tab === "problems" || tab === "toolchains"
    readonly property var problems: workbench.runState.problems || []
    readonly property bool canRun: workbench.permission !== "sandbox"
    readonly property string project: workbench.workspace
    property var drafts: ({})
    property int displayedId: workbench.activeTerminal
    property var history: []
    property int historyIndex: -1
    property string savedDraft: ""
    property bool follow: true
    color: "#121721"
    function switchInput() {
        const selected = workbench.activeTerminal
        if (displayedId === selected) return
        drafts[displayedId] = {text: input.text, history: history, index: historyIndex, saved: savedDraft}
        for (const key of Object.keys(drafts))
            if (workbench.terminalTabs.indexOf(Number(key)) < 0) delete drafts[key]
        const state = drafts[selected] || {text: "", history: [], index: -1, saved: ""}
        displayedId = selected
        input.text = state.text
        history = state.history
        historyIndex = state.index
        savedDraft = state.saved
        follow = true
        Qt.callLater(tail)
        if (visible) Qt.callLater(focusPanel)
    }
    Connections { target: workbench; function onTerminalTabsChanged() { panel.switchInput() } }
    Component.onCompleted: displayedId = workbench.activeTerminal
    FontMetrics { id: metrics; font: transcript.font }
    Timer {
        id: resizeTimer; interval: 90
        onTriggered: if (panel.visible && panel.terminal)
            workbench.resizeTerminal(Math.max(20, Math.floor((scroll.width - 27) / metrics.advanceWidth("M"))),
                                     Math.max(3, Math.floor((scroll.height - 13) / metrics.height)))
    }
    function focusPanel() {
        if (!visible) return
        if (terminal && canRun) input.forceActiveFocus()
        else if (tab === "problems") problemList.forceActiveFocus()
        else if (tab === "toolchains") chains.forceActiveFocus()
        else transcript.forceActiveFocus()
    }
    function submit() {
        const value = input.text
        if (!canRun || (!value && !workbench.activeTerminalRunning) || input.inputMethodComposing) return
        if (value && history[history.length - 1] !== value) history = history.concat([value]).slice(-50)
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
    function pickProblem(row) {
        if (row) panel.problemPicked({path: row.path, line: row.line, column: row.column, length: 0})
    }
    function tail() { if (visible && follow) scroll.contentY = Math.max(0, scroll.contentHeight - scroll.height) }
    onVisibleChanged: if (visible) { resizeTimer.restart(); Qt.callLater(tail) }
    onTabChanged: { follow = true; resizeTimer.restart(); Qt.callLater(tail) }
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        Rectangle {
            Layout.fillWidth: true; Layout.preferredHeight: 35; color: "#151b26"
            RowLayout {
                anchors.fill: parent; anchors.leftMargin: 9; anchors.rightMargin: 5; spacing: 3
                Repeater {
                    model: [{key:"terminal", label:"终端"}, {key:"output", label:"输出"},
                            {key:"problems", label:"问题" + (panel.problems.length ? " " + panel.problems.length : "")},
                            {key:"toolchains", label:"运行环境"}]
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
                IdeButton { visible: panel.tab === "problems" && panel.problems.length > 0; text: "清除"; tip: "清除问题列表"; onClicked: workbench.runAction("clearProblems", "") }
                IdeButton { visible: panel.tab === "toolchains"; glyph: "refresh-cw"; tip: "重新检测已安装的工具链"; onClicked: workbench.runAction("refresh", "") }
                IdeButton {
                    visible: panel.terminal; objectName: "terminalNew"; glyph: "plus"; tip: "新建终端"
                    enabled: workbench.terminalTabs.length < 16
                    onClicked: workbench.terminalAction("new", "")
                }
                IdeButton {
                    visible: panel.terminal; objectName: "terminalGitStatus"; glyph: "git-branch"; tip: "查看项目 Git 状态"
                    enabled: !workbench.activeTerminalRunning
                    onClicked: { panel.follow = true; workbench.terminalAction("git", "") }
                }
                IdeButton {
                    visible: panel.terminal && workbench.activeTerminalRunning; glyph: "square"; tip: "中断前台程序 · Ctrl+C"
                    onClicked: workbench.terminalAction("interrupt", "")
                }
                IdeButton { visible: panel.terminal; text: "清空"; tip: "清空当前终端输出"; enabled: !!workbench.terminalText; onClicked: workbench.terminalAction("clear", "") }
                IdeButton { visible: panel.terminal; objectName: "terminalClose"; glyph: "x"; tip: "关闭当前终端"; onClicked: workbench.terminalAction("close", "") }
                IdeButton { glyph: "chevron-down"; tip: "收起面板"; onClicked: panel.collapseRequested() }
            }
        }
        ListView {
            id: sessions
            ScrollBar.horizontal: AppScrollBar {}
            visible: panel.terminal; Layout.fillWidth: true; Layout.preferredHeight: 28 + (contentWidth > width ? ScrollBar.horizontal.implicitHeight : 0)
            orientation: ListView.Horizontal; clip: true; spacing: 2
            model: workbench.terminalTabs
            currentIndex: workbench.terminalTabs.indexOf(workbench.activeTerminal)
            highlightRangeMode: ListView.ApplyRange
            delegate: IdeButton {
                required property int modelData
                objectName: "terminalSession_" + modelData
                height: 28
                glyph: "square-terminal"
                text: (Qt.platform.os === "windows" ? "PowerShell " : "sh ") + modelData
                selected: workbench.activeTerminal === modelData
                Accessible.role: Accessible.PageTab
                Accessible.selected: selected
                onClicked: workbench.selectTerminal(modelData)
            }
        }
        AppListView {
            id: problemList; objectName: "codeProblemList"; visible: panel.tab === "problems"
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; model: panel.problems
            keyNavigationEnabled: true; highlightMoveDuration: 0; boundsBehavior: Flickable.StopAtBounds
            Keys.onReturnPressed: if (currentIndex >= 0) panel.pickProblem(model[currentIndex])
            Text {
                anchors.fill: parent; anchors.margins: 14; visible: !problemList.count; wrapMode: Text.Wrap
                color: "#919ba9"; font.pixelSize: 12
                text: workbench.runState.last ? "上次任务“" + workbench.runState.last.label + "”没有报告问题。"
                    : "运行、构建或测试后，编译器与运行时报告的错误会按 文件:行 显示在这里（F5 运行当前文件，Ctrl+Shift+B 构建）。"
            }
            delegate: ItemDelegate {
                id: problem; required property int index; required property var modelData
                width: problemList.rowWidth; height: 26; leftPadding: 12; rightPadding: 10; topPadding: 0; bottomPadding: 0
                Accessible.name: modelData.severity + " " + modelData.relative + " " + modelData.line + " " + modelData.message
                ToolTip.text: modelData.path + ":" + modelData.line + ":" + modelData.column + "\n" + modelData.message
                ToolTip.visible: hovered; ToolTip.delay: 650
                background: Rectangle { color: problem.ListView.isCurrentItem ? "#283a52" : problem.hovered ? "#222a36" : "transparent" }
                contentItem: RowLayout {
                    spacing: 7
                    Glyph {
                        Layout.preferredWidth: 14; Layout.preferredHeight: 14
                        name: problem.modelData.severity === "error" ? "circle-x" : problem.modelData.severity === "warning" ? "triangle-alert" : "info"
                        ink: problem.modelData.severity === "error" ? "#f0787a" : problem.modelData.severity === "warning" ? "#e9c46a" : "#8db4db"
                    }
                    Text { Layout.fillWidth: true; text: problem.modelData.message; elide: Text.ElideRight; color: "#d6dce5"; font.pixelSize: 12; textFormat: Text.PlainText }
                    Text {
                        Layout.maximumWidth: problemList.width * 0.45
                        text: problem.modelData.relative + ":" + problem.modelData.line + ":" + problem.modelData.column
                        elide: Text.ElideMiddle; color: problem.modelData.inWorkspace ? "#94a8c1" : "#6f7c8d"; font.pixelSize: 11; textFormat: Text.PlainText
                    }
                }
                onClicked: { problemList.currentIndex = index; panel.pickProblem(modelData) }
            }
        }
        Flickable {
            id: chains; objectName: "codeToolchains"; visible: panel.tab === "toolchains"
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; boundsBehavior: Flickable.StopAtBounds
            contentWidth: width; contentHeight: chainColumn.implicitHeight + 16
            ScrollBar.vertical: AppScrollBar {}
            Column {
                id: chainColumn; x: 12; y: 8; width: chains.width - 24; spacing: 8
                Text {
                    width: parent.width; wrapMode: Text.Wrap; color: "#a5afbc"; font.pixelSize: 11; textFormat: Text.PlainText
                    text: (workbench.runState.canRun ? "运行、构建和测试按当前权限策略执行；需要批准时会先弹出确认。"
                                                     : "当前为沙箱运行：可以查看检测结果，但不会执行项目代码。切换到“手动批准”或“完全访问”后可运行。")
                        + ((workbench.runState.notes || []).length ? "\n" + workbench.runState.notes.join("\n") : "")
                }
                Repeater {
                    model: workbench.runState.toolchains || []
                    Rectangle {
                        id: chainCard; required property var modelData
                        width: chainColumn.width; radius: 6; color: "#182030"; border.color: modelData.ready ? "#2c4a3a" : "#4a3a2c"
                        implicitHeight: chainRow.implicitHeight + 16; height: implicitHeight
                        Column {
                            id: chainRow; x: 10; y: 8; width: parent.width - 20; spacing: 3
                            RowLayout {
                                spacing: 7
                                Glyph { Layout.preferredWidth: 14; Layout.preferredHeight: 14; name: chainCard.modelData.ready ? "circle-check" : "triangle-alert"; ink: chainCard.modelData.ready ? "#7fd19b" : "#e9c46a" }
                                Text { text: chainCard.modelData.label + (chainCard.modelData.ready ? "" : " · 缺少 " + chainCard.modelData.missing.join("、")); color: "#e2e7ee"; font.pixelSize: 12; font.weight: Font.DemiBold }
                            }
                            Repeater {
                                model: chainCard.modelData.tools
                                Text {
                                    required property var modelData
                                    width: chainRow.width; elide: Text.ElideMiddle; font.pixelSize: 11; textFormat: Text.PlainText
                                    color: modelData.path ? "#aeb9c7" : "#6f7c8d"
                                    text: modelData.name + "：" + (modelData.path || "未找到") + (modelData.origin ? "（" + modelData.origin + "）" : "")
                                }
                            }
                            Text { visible: !!chainCard.modelData.guidance; width: chainRow.width; wrapMode: Text.Wrap; color: "#e6cf9a"; font.pixelSize: 11; text: chainCard.modelData.guidance; textFormat: Text.PlainText }
                        }
                    }
                }
            }
        }
        Flickable {
            id: scroll; visible: !panel.listing; Layout.fillWidth: true; Layout.fillHeight: true; clip: true; boundsBehavior: Flickable.StopAtBounds
            contentWidth: width; contentHeight: Math.max(height, transcript.implicitHeight)
            onMovementStarted: panel.follow = false
            onMovementEnded: panel.follow = atYEnd
            onContentHeightChanged: Qt.callLater(panel.tail)
            onHeightChanged: { resizeTimer.restart(); Qt.callLater(panel.tail) }
            onWidthChanged: resizeTimer.restart()
            ScrollBar.vertical: AppScrollBar { onPressedChanged: panel.follow = !pressed && scroll.atYEnd }
            TextArea {
                id: transcript; objectName: "codeTerminalTranscript"; width: scroll.width
                text: panel.terminal ? workbench.terminalText : [panel.resultText, workbench.terminalOutput].filter(Boolean).join("\n")
                placeholderText: panel.terminal ? panel.project + "\n等待输入命令…" : "Git 变更、文件比较和项目测试输出将显示在这里"
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
                    placeholderText: !panel.canRun ? "沙箱运行 · 可查看输出和 Git 状态" : (workbench.activeTerminalRunning ? "输入命令或回复正在运行的程序" : "输入第一条命令以启动终端")
                    readOnly: !panel.canRun; selectByMouse: true; font.family: "Cascadia Mono"; font.pixelSize: 12
                    background: null; leftPadding: 2; rightPadding: 2
                    onAccepted: panel.submit()
                    Keys.onPressed: event => {
                        if (event.key === Qt.Key_C && (event.modifiers & Qt.ControlModifier)
                            && !selectedText && workbench.activeTerminalRunning) {
                            workbench.terminalAction("interrupt", "")
                            event.accepted = true
                        }
                    }
                    Keys.onUpPressed: event => { if (!inputMethodComposing) panel.recall(-1); else event.accepted = false }
                    Keys.onDownPressed: event => { if (!inputMethodComposing) panel.recall(1); else event.accepted = false }
                }
                IdeButton { visible: !panel.canRun; glyph: "shield"; tip: "切换执行权限"; onClicked: workbench.permissionRequested() }
                IdeButton {
                    visible: panel.canRun; glyph: "play"; tip: "发送到当前终端"; enabled: !!input.text || workbench.activeTerminalRunning
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
