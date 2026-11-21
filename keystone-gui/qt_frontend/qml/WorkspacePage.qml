import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

Item {
    id: page
    required property var material
    required property string pageKey
    readonly property var viewData: workbench.pageData[pageKey] || ({})
    readonly property bool files: pageKey === "files" || pageKey === "development"
    readonly property bool ide: pageKey === "development"
    property bool sidebarOpen: true
    property bool dockOpen: false
    property bool dockVisited: false
    property string dockTab: "terminal"
    property real dockHeight: 226
    property string lastResult: ""
    property var pendingSelection: null
    property var cursorPositions: ({})
    component PageButton: GlassButton { flat: page.ide }
    component PageSurface: GlassSurface { flat: page.ide }
    property var schema: []
    property var values: ({})
    property bool initialized: false
    property string editorPath: ""
    property int editorVersion: 0
    property bool syncing: false
    property bool outputOpen: false
    property bool findOpen: false
    property bool jumpOpen: false
    property bool referenceOpen: false
    readonly property bool hasDocument: editorPath !== ""
    readonly property bool bufferDirty: hasDocument && code.text !== (viewData.document || {}).original
    readonly property var lineStarts: offsets(code.text, "\n", true)
    readonly property int cursorLine: indexAt(lineStarts, code.editor.cursorPosition)
    readonly property int cursorColumn: code.editor.cursorPosition - lineStarts[cursorLine] + 1
    readonly property var matches: findOpen && hasDocument ? offsets(code.text, findText.text, false) : []
    readonly property int selectedMatch: {
        const start = code.editor.selectionStart
        const index = indexAt(matches, start)
        return matches[index] === start && code.editor.selectionEnd - start === findText.text.length ? index : -1
    }
    objectName: "page_" + pageKey

    function sync() {
        if (!initialized && viewData.fields) {
            const next = {}
            for (const f of viewData.fields) next[f.key] = f.value
            values = next
            schema = viewData.fields
            initialized = true
        }
        const doc = viewData.document || {}
        if (doc.path && (doc.path !== editorPath || Number(doc.version || 0) >= editorVersion)) {
            syncing = true
            const switched = doc.path !== editorPath
            if (switched) {
                jumpOpen = false
                if (ide && editorPath) cursorPositions[editorPath] = code.editor.cursorPosition
            }
            editorPath = doc.path
            editorVersion = Number(doc.version || 0)
            if (code.text !== doc.text) code.text = doc.text
            if (ide && switched) {
                if (!pendingSelection || pendingSelection.path !== doc.path) pendingSelection = {path: doc.path}
            }
            syncing = false
        } else if (!doc.path && files) {
            syncing = true; editorPath = ""; code.text = ""; syncing = false
            findOpen = false; jumpOpen = false
        }
        if (pendingSelection && doc.path === pendingSelection.path) Qt.callLater(applySelection)
        const result = viewData.result || ""
        if (result && result !== lastResult) {
            if (ide) {
                if (!dockOpen) showDock("output", false)
            } else outputOpen = true
        }
        lastResult = result
    }
    onViewDataChanged: sync()
    Component.onCompleted: sync()
    function showDock(tab, focus) {
        dockTab = tab
        dockVisited = true
        dockOpen = true
        if (focus && dock.item) Qt.callLater(dock.item.focusPanel)
    }
    function toggleDock(tab) {
        if (dockOpen && dockTab === tab) {
            dockOpen = false
            code.editor.forceActiveFocus()
        } else showDock(tab, true)
    }
    function invoke(action, row) {
        if (ide && (action === "git" || action === "compare")) showDock("output", false)
        if (action === "chooseProject") { projectPicker.open(); return }
        if (action === "find") { openFind(); return }
        if (action === "reference") { referenceOpen = !referenceOpen; return }
        if (action === "saveAs") { savePicker.open(); return }
        const payload = Object.assign({}, values, row || {})
        workbench.pageAction(pageKey, action, JSON.stringify(payload))
    }
    Connections {
        target: workbench
        function onTerminalRevealRequested() { if (page.ide && page.visible) page.showDock("terminal", false) }
        function onChanged() {
            if (page.pendingSelection && !workbench.busy && page.pendingSelection.path === page.editorPath)
                Qt.callLater(page.applySelection)
        }
        function onFormReset(key) { if (key === page.pageKey) { page.initialized = false; page.schema = [] } }
        function onFormValues(key, patch) {
            if (key !== page.pageKey) return
            page.values = Object.assign({}, page.values, patch)
            for (let i = 0; i < fields.count; i++) {
                const item = fields.itemAt(i)
                if (item && patch[item.definition.key] !== undefined) item.value = patch[item.definition.key]
            }
        }
    }
    FolderDialog { id: projectPicker; title: "选择工作区"; onAccepted: page.invoke("project", {path: decodeURIComponent(selectedFolder.toString().replace(/^file:\/\//, "").replace(/^\/([A-Za-z]:)/, "$1"))}) }
    FileDialog {
        id: savePicker; title: "另存为"; fileMode: FileDialog.SaveFile
        onAccepted: workbench.pageAction(page.pageKey, "saveAs", JSON.stringify({path:page.editorPath, target:decodeURIComponent(selectedFile.toString().replace(/^file:\/\//, "").replace(/^\/([A-Za-z]:)/, "$1"))}))
    }
    function offsets(text, needle, lineStarts) {
        const positions = lineStarts ? [0] : []
        if (!needle.length) return positions
        for (let start = text.indexOf(needle); start !== -1; start = text.indexOf(needle, start + needle.length))
            positions.push(lineStarts ? start + 1 : start)
        return positions
    }
    function indexAt(positions, offset) {
        let low = 0, high = positions.length
        while (low < high) {
            const mid = Math.floor((low + high) / 2)
            if (positions[mid] <= offset) low = mid + 1
            else high = mid
        }
        return low - 1
    }
    function openFind() {
        jumpOpen = false
        const selected = code.editor.selectedText
        if (!findOpen && selected && !/[\r\n]/.test(selected)) findText.text = selected
        findOpen = true
        findText.forceActiveFocus()
        findText.selectAll()
    }
    function findMatch(backward) {
        if (!matches.length) return
        const edge = backward ? code.editor.selectionStart : code.editor.selectionEnd
        const before = indexAt(matches, edge - 1)
        const index = backward ? (before + matches.length) % matches.length : (before + 1) % matches.length
        code.editor.select(matches[index], matches[index] + findText.text.length)
        code.revealPosition(matches[index])
    }
    function replaceMatch() {
        if (selectedMatch < 0) { findMatch(false); return }
        const start = code.editor.selectionStart
        code.editor.forceActiveFocus()
        controller.replaceCodeSelection(code.editor.textDocument, start, code.editor.selectionEnd, replaceText.text)
        code.editor.cursorPosition = start + replaceText.text.length
        findMatch(false)
    }
    function openJump() {
        findOpen = false
        jumpOpen = true
        jumpText.text = (cursorLine + 1) + ":" + cursorColumn
        jumpText.forceActiveFocus()
        jumpText.selectAll()
    }
    function jumpOffset(value) {
        const parts = /^([1-9]\d*)(?:\s*:\s*([1-9]\d*))?$/.exec(value.trim())
        if (!parts || Number(parts[1]) > lineStarts.length) return -1
        const line = Number(parts[1]) - 1
        const end = line + 1 < lineStarts.length ? lineStarts[line + 1] - 1 : code.text.length
        return Math.min(end, lineStarts[line] + Number(parts[2] || 1) - 1)
    }
    function jumpToPosition() {
        const position = jumpOffset(jumpText.text)
        if (position < 0) return
        jumpOpen = false
        code.editor.forceActiveFocus()
        code.editor.cursorPosition = position
        code.revealPosition(position)
    }
    function closeNavigation() {
        findOpen = false
        jumpOpen = false
        code.editor.forceActiveFocus()
    }

    function applySelection() {
        const hit = pendingSelection
        if (!hit || editorPath !== hit.path || workbench.busy) return
        pendingSelection = null
        code.editor.forceActiveFocus()
        if (hit.line) {
            const start = jumpOffset(hit.line + ":" + hit.column)
            if (start >= 0) {
                code.editor.select(start, start + hit.length)
                code.revealPosition(start)
            }
        } else {
            code.editor.cursorPosition = Math.min(hit.position ?? cursorPositions[hit.path] ?? 0, code.text.length)
            code.revealPosition(code.editor.cursorPosition)
        }
    }
    function openEntry(entry) {
        if (!entry || workbench.busy) return
        pendingSelection = entry.path === editorPath && !entry.line
            ? {path: entry.path, position: code.editor.cursorPosition} : entry
        if (entry.path === editorPath) applySelection()
        else invoke("open", {path: entry.path})
    }
    function openQuick() {
        if (!workbench.busy) quickOpen.open()
    }
    function openSearch() {
        sidebarOpen = true
        sidebar.search(code.editor.selectedText)
    }
    CodeQuickOpen { id: quickOpen; objectName: "codeQuickOpen"; rootPath: page.viewData.hint || ""; onPicked: entry => page.openEntry(entry) }
    PageSurface { anchors.fill: parent; material: page.material; radius: page.ide ? 0 : 20; tint: page.ide ? "#191b1f" : "#22232d39" }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: page.ide ? 0 : 18; spacing: page.ide ? 0 : 12
        Text { visible: !page.ide; Layout.fillWidth: true; text: page.viewData.hint || "正在载入…"; color: "#9eacbc"; font.pixelSize: 12; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideMiddle }
        Flow {
            visible: !page.ide; Layout.fillWidth: true; spacing: 7
            Repeater { model: page.viewData.actions || []
                PageButton { required property var modelData; material: page.material; text: modelData.label; enabled: !workbench.busy || modelData.key === "cancel"; ink: modelData.danger ? Theme.danger : Theme.textPrimary; onClicked: page.invoke(modelData.key) }
            }
            PageButton { material: page.material; glyph: "refresh-cw"; tip: "刷新"; enabled: !workbench.busy; onClicked: page.invoke("refresh") }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: page.files
            RowLayout {
                anchors.fill: parent; spacing: page.ide ? 0 : 12
                Rectangle {
                    visible: page.ide; Layout.preferredWidth: 43; Layout.fillHeight: true; color: "#181b1f"
                    Column {
                        anchors.top: parent.top; width: parent.width; spacing: 2
                        Repeater {
                            model: [{key:"explorer", glyph:"folder", tip:"资源管理器 · Ctrl+Shift+E"},
                                    {key:"search", glyph:"search", tip:"工作区搜索 · Ctrl+Shift+F"},
                                    {key:"git", glyph:"git-branch", tip:"Git 变更"},
                                    {key:"terminal", glyph:"square-terminal", tip:"终端面板 · Ctrl+J"}]
                            IdeButton {
                                required property var modelData
                                width: parent.width; height: 42; glyph: modelData.glyph; tip: modelData.tip
                                objectName: "codeRail_" + modelData.key
                                selected: modelData.key === "terminal" ? page.dockOpen && page.dockTab === "terminal"
                                    : page.sidebarOpen && sidebar.mode === modelData.key
                                Rectangle { visible: parent.selected; y: 7; height: parent.height - 14; width: 2; color: "#82b1f1" }
                                onClicked: {
                                    if (modelData.key === "terminal") { page.toggleDock("terminal"); return }
                                    if (modelData.key === "git") { if (!workbench.busy) page.invoke("git"); return }
                                    if (selected) { page.sidebarOpen = false; code.editor.forceActiveFocus(); return }
                                    page.sidebarOpen = true
                                    if (modelData.key === "search") page.openSearch()
                                    else sidebar.explorer()
                                }
                            }
                        }
                    }
                }
                CodeSidebar {
                    id: sidebar; visible: page.ide && page.sidebarOpen
                    Layout.preferredWidth: Math.min(250, Math.max(165, parent.width * 0.27)); Layout.fillHeight: true
                    viewData: page.ide ? page.viewData : ({}); activePath: page.editorPath
                    onAction: (key, args) => page.invoke(key, args)
                    onPicked: entry => page.openEntry(entry)
                }
                ColumnLayout {
                    visible: !page.ide
                    Layout.preferredWidth: Math.min(200, parent.width * 0.3); Layout.fillHeight: true; spacing: 7
                    Text { Layout.fillWidth: true; text: page.viewData.directory || "项目文件"; color: "#8897a8"; elide: Text.ElideMiddle; font.pixelSize: 11 }
                    TextField {
                        id: fileQuery; objectName: "projectFileSearch"; Layout.fillWidth: true; placeholderText: "查找文件 · Enter"
                        color: "#e4eaf1"; placeholderTextColor: "#8293a5"; font.pixelSize: 12; selectByMouse: true
                        background: PageSurface { material: page.material; radius: 9 }
                        onAccepted: page.invoke("search", {query:text})
                    }
                    ListView {
                        id: tree; objectName: "projectFiles"; Layout.fillWidth: true; Layout.fillHeight: true
                        model: page.viewData.rows || []; clip: true; spacing: 3
                        ScrollBar.vertical: ScrollBar {}
                        delegate: PageButton {
                            required property var modelData
                            width: tree.width - 6; material: page.material; text: modelData.title; glyph: modelData.directory ? "folder" : "file"
                            quiet: true; selected: page.editorPath.endsWith(modelData.id.replace(/\//g, "\\")) || page.editorPath.endsWith(modelData.id)
                            tip: modelData.title; enabled: !workbench.busy
                            contentItem: Text { text: modelData.title; color: "#c7d0db"; font.pixelSize: 12; verticalAlignment: Text.AlignVCenter; elide: Text.ElideMiddle; leftPadding: 8 }
                            onClicked: page.invoke("open", {id:modelData.id})
                        }
                    }
                }
                ColumnLayout {
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: page.ide ? 0 : 9
                    Flickable {
                        id: tabStrip
                        visible: !page.ide || tabs.width > 0
                        Layout.fillWidth: true; Layout.preferredHeight: 34; contentWidth: tabs.width; clip: true
                        boundsBehavior: Flickable.StopAtBounds
                        flickableDirection: Flickable.HorizontalFlick
                        Row { id: tabs; spacing: page.ide ? 0 : 5
                            Repeater { model: page.viewData.tabs || []
                                Row {
                                    id: fileTab
                                    required property var modelData
                                    readonly property bool selected: page.editorPath === modelData.path
                                    spacing: page.ide ? 0 : 2
                                    Component.onCompleted: if (selected) Qt.callLater(reveal)
                                    onSelectedChanged: if (selected) Qt.callLater(reveal)
                                    function reveal() {
                                        if (x < tabStrip.contentX) tabStrip.contentX = x
                                        else if (x + width > tabStrip.contentX + tabStrip.width)
                                            tabStrip.contentX = x + width - tabStrip.width
                                    }
                                    PageButton {
                                        material: page.material; height: 32
                                        text: fileTab.modelData.name + ((fileTab.selected ? page.bufferDirty : fileTab.modelData.dirty) ? " •" : "")
                                        Rectangle { visible: page.ide && fileTab.selected; width: parent.width + 28; height: 2; color: "#84b5f5"; z: 1 }
                                        tip: fileTab.modelData.path; selected: fileTab.selected; enabled: !workbench.busy
                                        onClicked: page.invoke("open", {path:fileTab.modelData.path})
                                    }
                                    PageButton {
                                        objectName: "closeEditorTab"; material: page.material; width: 28; height: 32; glyph: "x"; quiet: true
                                        tip: "关闭 " + fileTab.modelData.name; enabled: !workbench.busy
                                        onClicked: page.invoke("close", {path:fileTab.modelData.path})
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        visible: page.ide; Layout.fillWidth: true; Layout.preferredHeight: 31; color: "#212429"
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 10; anchors.rightMargin: 4; spacing: 3
                            Text { Layout.fillWidth: true; text: page.editorPath ? page.editorPath.slice((page.viewData.hint || "").length + 1).replace(/[\\\\/]/g, "  ›  ") : "编辑器"; elide: Text.ElideMiddle; color: "#aab4c2"; font.pixelSize: 12 }
                            IdeButton { glyph: "save"; tip: "保存 · Ctrl+S"; enabled: page.hasDocument && !workbench.busy; onClicked: page.invoke("save", {path:page.editorPath}) }
                            IdeButton { glyph: "sliders-horizontal"; tip: "编辑器操作"; enabled: page.hasDocument && !workbench.busy; onClicked: editMenu.open()
                                Menu { id: editMenu; y: parent.height
                                    Repeater {
                                        model: [{key:"saveAs",label:"另存为…"},{key:"find",label:"查找 / 替换"},{key:"reference",label:"原文参考"},{key:"compare",label:"比较磁盘版本"},{key:"merge",label:"合并"},{key:"discard",label:"重新载入"},{key:"close",label:"关闭文件"}]
                                        MenuItem { required property var modelData; text: modelData.label; onTriggered: page.invoke(modelData.key, {path:page.editorPath}) }
                                    }
                                }
                            }
                        }
                    }
                    Item {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        PageSurface { anchors.fill: parent; material: page.material; radius: page.ide ? 0 : 14; tint: page.ide ? "#1c1e22" : "#50131b25"; emphasis: !page.ide && code.editor.activeFocus ? 0.5 : 0 }
                        Item {
                            id: gutter; objectName: "editorLineNumbers"
                            visible: page.hasDocument; clip: true
                            x: 8; y: code.y; width: visible ? Math.max(28, String(page.lineStarts.length).length * 8 + 11) : 0; height: code.height
                            readonly property int firstLine: page.indexAt(page.lineStarts, Math.max(0, code.editor.positionAt(1, code.contentY)))
                            readonly property real lineHeight: Math.max(1, code.editor.cursorRectangle.height)
                            Repeater {
                                model: gutter.visible ? Math.min(page.lineStarts.length - gutter.firstLine, Math.ceil(gutter.height / gutter.lineHeight) + 3) : 0
                                Text {
                                    required property int index
                                    readonly property int line: gutter.firstLine + index
                                    readonly property rect location: code.editor.positionToRectangle(page.lineStarts[line] || 0)
                                    y: location.y - code.contentY; width: gutter.width - 8; height: location.height
                                    text: line + 1
                                    color: line === page.cursorLine ? Theme.accent : Theme.textMuted
                                    opacity: line === page.cursorLine ? 0.95 : 0.72
                                    font.family: code.editor.font.family
                                    font.pixelSize: code.editor.font.pixelSize
                                    font.weight: line === page.cursorLine ? Font.DemiBold : Font.Normal
                                    horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        Rectangle {
                            visible: gutter.visible
                            x: gutter.x + gutter.width + 1; y: 12; width: 1; height: parent.height - 24
                            color: "#16ffffff"
                        }
                        Rectangle {
                            visible: page.referenceOpen
                            x: parent.width / 2; y: 12; width: 1; height: parent.height - 24
                            color: "#1cffffff"
                        }
                        ComposerText {
                            id: code; objectName: "nativeCodeEditor"; editorName: "codeEditorInput"
                            anchors.fill: parent; anchors.margins: 14; anchors.leftMargin: gutter.visible ? gutter.width + 10 : 14
                            anchors.rightMargin: page.referenceOpen ? parent.width / 2 + 7 : 14; submitOnEnter: false; horizontalScroll: true
                            placeholder: page.ide ? "" : "选择文件开始编辑"
                            editor.readOnly: !page.editorPath || workbench.busy
                            editor.font.family: "Cascadia Code"; editor.font.pixelSize: 13
                            Component.onCompleted: controller.formatCode(editor.textDocument)
                            onTextChanged: {
                                if (!page.syncing && page.editorPath && editor.activeFocus) {
                                    page.editorVersion++
                                    workbench.updateDocument(page.editorPath, text, page.editorVersion,
                                                             text !== (page.viewData.document || {}).original)
                                }
                            }
                            ScrollBar.vertical: ScrollBar {}
                            ScrollBar.horizontal: ScrollBar {}
                        }
                        Column {
                            visible: page.ide && !page.hasDocument; anchors.centerIn: parent; spacing: 8
                            IdeButton { glyph: "file-code"; text: "快速打开文件"; tip: "Ctrl+P"; onClicked: page.openQuick()
                                Text { anchors.left: parent.right; anchors.verticalCenter: parent.verticalCenter; text: "Ctrl+P"; color: "#929ca9"; font.pixelSize: 11 }
                            }
                            IdeButton { glyph: "search"; text: "工作区搜索"; tip: "Ctrl+Shift+F"; onClicked: page.openSearch()
                                Text { anchors.left: parent.right; anchors.verticalCenter: parent.verticalCenter; text: "Ctrl+Shift+F"; color: "#929ca9"; font.pixelSize: 11 }
                            }
                        }
                        ComposerText {
                            visible: page.referenceOpen; x: parent.width / 2 + 7; y: 14; width: parent.width / 2 - 21; height: parent.height - 28
                            text: (page.viewData.document || {}).original || ""; submitOnEnter: false; horizontalScroll: true
                            editor.readOnly: true; editor.font.family: "Cascadia Code"; editor.font.pixelSize: 13
                            Component.onCompleted: controller.formatCode(editor.textDocument)
                            ScrollBar.vertical: ScrollBar {}
                            ScrollBar.horizontal: ScrollBar {}
                        }
                    }
                    RowLayout {
                        visible: page.findOpen; Layout.fillWidth: true; spacing: 6
                        TextField { id: findText; objectName: "editorFindInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "在文件中查找"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: 9; emphasis: findText.activeFocus ? 0.65 : 0 } onAccepted: page.findMatch(false) }
                        Text { objectName: "editorMatchCount"; visible: !!findText.text; color: Theme.textSecondary; font.pixelSize: 12; text: !page.matches.length ? "无匹配" : page.selectedMatch >= 0 ? (page.selectedMatch + 1) + " / " + page.matches.length : page.matches.length + " 处" }
                        PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "chevron-up"; tip: "上一处 · Shift+F3"; enabled: !!page.matches.length; onClicked: page.findMatch(true) }
                        PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "chevron-down"; tip: "下一处 · F3"; enabled: !!page.matches.length; onClicked: page.findMatch(false) }
                        PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "x"; tip: "关闭查找 · Esc"; onClicked: page.closeNavigation() }
                    }
                    RowLayout {
                        visible: page.findOpen; Layout.fillWidth: true; spacing: 6
                        TextField { id: replaceText; objectName: "editorReplaceInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "替换为"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: 9; emphasis: replaceText.activeFocus ? 0.65 : 0 } onAccepted: if (!workbench.busy && page.matches.length) page.replaceMatch() }
                        PageButton { objectName: "replaceEditorMatch"; material: page.material; text: "替换"; enabled: !workbench.busy && !!page.matches.length; onClicked: page.replaceMatch() }
                    }
                    RowLayout {
                        visible: page.jumpOpen; Layout.fillWidth: true; spacing: 6
                        TextField { id: jumpText; objectName: "editorJumpInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "行:列"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: 9; emphasis: jumpText.activeFocus ? 0.65 : 0 } onAccepted: page.jumpToPosition() }
                        Text { color: Theme.textSecondary; text: "第 1–" + page.lineStarts.length + " 行"; font.pixelSize: 12 }
                        PageButton { material: page.material; text: "跳转"; enabled: page.jumpOffset(jumpText.text) >= 0; onClicked: page.jumpToPosition() }
                        PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "x"; tip: "取消跳转 · Esc"; onClicked: page.closeNavigation() }
                    }
                    Loader {
                        id: dock
                        visible: page.ide && page.dockOpen; active: page.ide && page.dockVisited
                        Layout.fillWidth: true; Layout.preferredHeight: Math.min(page.dockHeight, page.height * 0.56)
                        sourceComponent: CodeTerminalPanel {
                            resultText: page.viewData.result || ""; tab: page.dockTab
                            onTabRequested: name => page.dockTab = name
                            onCollapseRequested: page.toggleDock(page.dockTab)
                            onResizeRequested: pixels => page.dockHeight = Math.min(page.height * 0.56, Math.max(110, pixels))
                        }
                    }
                    Rectangle {
                        visible: page.ide || page.hasDocument; Layout.fillWidth: true; Layout.preferredHeight: 26
                        color: page.ide ? "#202328" : "transparent"
                        Rectangle { visible: page.ide; width: parent.width; height: 1; color: "#363c44" }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 8; anchors.rightMargin: 6; spacing: 7
                            Glyph { visible: page.ide; name: "folder"; Layout.preferredWidth: 13; Layout.preferredHeight: 13; ink: "#9aa7b8" }
                            Text {
                                Layout.fillWidth: true
                                text: page.ide ? workbench.workspace.replace(/\\/g, "/").split("/").pop() : page.editorPath
                                color: Theme.textMuted; elide: Text.ElideMiddle; font.pixelSize: 11
                            }
                            Text { text: (page.viewData.document || {}).status === "conflict" ? "磁盘有变更" : page.bufferDirty ? "未保存" : ""; color: Theme.warning; font.pixelSize: 11 }
                            PageButton { visible: page.hasDocument; objectName: "editorPosition"; material: page.material; quiet: true; Layout.preferredHeight: 24; text: "行 " + (page.cursorLine + 1) + "，列 " + page.cursorColumn; tip: "跳转到行 · Ctrl+G"; onClicked: page.openJump() }
                            Text { visible: page.hasDocument; text: ((page.viewData.document || {}).encoding || "").toUpperCase(); color: Theme.textMuted; font.pixelSize: 11 }
                            IdeButton { visible: page.ide; glyph: "square-terminal"; tip: "切换终端"; selected: page.dockOpen && page.dockTab === "terminal"; Layout.preferredHeight: 22; onClicked: page.toggleDock("terminal") }
                        }
                    }
                    Flow {
                        visible: !page.ide; Layout.fillWidth: true; spacing: 6
                        Repeater { model: [{key:"save",label:"保存"},{key:"saveAs",label:"另存为"},{key:"find",label:"查找 / 替换"},{key:"reference",label:"原文参考"},{key:"compare",label:"比较磁盘版本"},{key:"merge",label:"合并"},{key:"discard",label:"重新载入"},{key:"close",label:"关闭文件"}]
                            PageButton { required property var modelData; material: page.material; text: modelData.label; enabled: !!page.editorPath && !workbench.busy; onClicked: page.invoke(modelData.key, {path:page.editorPath}) }
                        }
                    }
                }
            }
        }
        ScrollView {
            id: body; visible: !page.files; Layout.fillWidth: true; Layout.fillHeight: true
            contentWidth: availableWidth; clip: true
            ColumnLayout {
                width: body.availableWidth; spacing: 14
                Repeater {
                    id: fields; model: page.schema
                    WorkbenchField {
                        required property var modelData
                        Layout.fillWidth: true; material: page.material; definition: modelData
                        onEdited: value => {
                            page.values[definition.key] = value
                            if (page.pageKey === "computer" && ["model", "consent", "clicks"].includes(definition.key)) page.invoke("settings")
                            if (["config", "agents", "connector_accounts"].includes(page.pageKey)) workbench.setFormDirty(page.pageKey, true)
                        }
                    }
                }
                Image { Layout.fillWidth: true; Layout.preferredHeight: visible ? 260 : 0; visible: !!page.viewData.image; source: page.viewData.image || ""; fillMode: Image.PreserveAspectFit; cache: false }
                Repeater {
                    model: page.viewData.rows || []
                    Item {
                        required property var modelData
                        property bool compact: page.pageKey === "config"
                        Layout.fillWidth: true; implicitHeight: compact ? 60 : rowContent.implicitHeight + 24
                        GlassSurface { anchors.fill: parent; material: page.material; radius: 14; tint: "#252c3743" }
                        RowLayout {
                            visible: parent.compact; anchors.fill: parent; anchors.margins: 12; spacing: 14
                            Text { Layout.preferredWidth: Math.min(180, parent.width * 0.3); text: modelData.title; color: "#e1e7ee"; font.pixelSize: 14; elide: Text.ElideRight }
                            Text { Layout.fillWidth: true; text: modelData.subtitle || ""; color: "#8e9daf"; font.pixelSize: 12; elide: Text.ElideMiddle }
                            Repeater {
                                model: modelData.actions || []
                                GlassButton { required property var modelData; material: page.material; text: modelData.label; height: 32; enabled: !workbench.busy; onClicked: page.invoke(modelData.key, {id:parent.parent.modelData.id}) }
                            }
                        }
                        ColumnLayout {
                            id: rowContent; visible: !parent.compact; anchors.left: parent.left; anchors.right: parent.right; anchors.top: parent.top; anchors.margins: 12; spacing: 7
                            Text { Layout.fillWidth: true; text: modelData.title; color: "#e1e7ee"; font.pixelSize: 14; wrapMode: Text.Wrap; maximumLineCount: 5; elide: Text.ElideRight }
                            Text { Layout.fillWidth: true; text: modelData.subtitle || ""; color: "#8e9daf"; font.pixelSize: 11; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideRight }
                            Flow {
                                Layout.fillWidth: true; spacing: 6
                                property var row: modelData
                                Repeater { model: modelData.actions || []
                                    GlassButton {
                                        required property var modelData
                                        material: page.material; text: modelData.label; height: 32
                                        ink: modelData.danger ? "#e9aaa4" : "#cbd4de"; enabled: !workbench.busy
                                        onClicked: page.invoke(modelData.key, {id:parent.row.id})
                                    }
                                }
                            }
                        }
                    }
                }
                Item { Layout.preferredHeight: 12 }
            }
        }
        ColumnLayout {
            visible: !page.ide && !!page.viewData.result; Layout.fillWidth: true; spacing: 7
            PageButton { material: page.material; text: page.outputOpen ? "收起结果" : "展开结果"; onClicked: page.outputOpen = !page.outputOpen }
            ScrollView {
                visible: page.outputOpen; Layout.fillWidth: true; Layout.preferredHeight: Math.min(240, page.height * 0.3); clip: true
                TextArea { text: page.viewData.result || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: "#c5d1dd"; font.family: "Cascadia Code"; font.pixelSize: 12; background: null }
            }
        }
    }
    Shortcut { sequence: "Ctrl+S"; enabled: page.visible && page.files && !!page.editorPath && !workbench.busy; onActivated: page.invoke("save", {path:page.editorPath}) }
    Shortcut { sequence: "Ctrl+F"; enabled: page.visible && page.files && page.hasDocument; onActivated: page.openFind() }
    Shortcut { sequence: "Ctrl+H"; enabled: page.visible && page.files && page.hasDocument; onActivated: { page.openFind(); replaceText.forceActiveFocus() } }
    Shortcut { sequence: "F3"; enabled: page.visible && page.findOpen; onActivated: page.findMatch(false) }
    Shortcut { sequence: "Shift+F3"; enabled: page.visible && page.findOpen; onActivated: page.findMatch(true) }
    Shortcut { sequence: "Shift+Return"; enabled: page.visible && page.findOpen && findText.activeFocus; onActivated: page.findMatch(true) }
    Shortcut { sequence: "Ctrl+G"; enabled: page.visible && page.files && page.hasDocument; onActivated: page.openJump() }
    Shortcut { sequence: "Escape"; enabled: page.visible && !controller.busy && (page.findOpen || page.jumpOpen); onActivated: page.closeNavigation() }
    Shortcut { sequence: "Ctrl+W"; enabled: page.visible && page.files && page.hasDocument && !workbench.busy; onActivated: page.invoke("close", {path:page.editorPath}) }
    Shortcut { sequence: "Ctrl+P"; enabled: page.visible && page.files; onActivated: if (page.ide) page.openQuick(); else fileQuery.forceActiveFocus() }
    Shortcut { sequence: "Ctrl+Shift+F"; enabled: page.visible && page.ide && !quickOpen.visible; onActivated: page.openSearch() }
    Shortcut { sequence: "Ctrl+Shift+E"; enabled: page.visible && page.ide && !quickOpen.visible; onActivated: { page.sidebarOpen = true; sidebar.explorer() } }
    Shortcut { sequence: "Ctrl+`"; enabled: page.visible && page.ide && !quickOpen.visible; onActivated: page.toggleDock("terminal") }
    Shortcut { sequence: "Ctrl+J"; enabled: page.visible && page.ide && !quickOpen.visible; onActivated: page.toggleDock(page.dockTab) }
}
