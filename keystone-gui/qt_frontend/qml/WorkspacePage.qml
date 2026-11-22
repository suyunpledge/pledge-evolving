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
        if (action === "editModel") { workbench.editModel(String((row || {}).id || "")); return }
        const payload = Object.assign({}, values, row || {})
        workbench.pageAction(pageKey, action, JSON.stringify(payload))
    }
    Connections {
        target: workbench
        function onTerminalRevealRequested() { if (page.ide && page.visible && !page.dockOpen) page.showDock("output", false) }
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
    function fileIconColor(name) {
        const lower = String(name || "").toLowerCase()
        if (lower.endsWith(".py")) return "#59b2f5"
        if (lower.endsWith(".qml")) return "#68d68b"
        if (lower.endsWith(".ts") || lower.endsWith(".tsx")) return "#4da6ff"
        if (lower.endsWith(".js") || lower.endsWith(".jsx") || lower.endsWith(".mjs")) return "#f0db4f"
        if (lower.endsWith(".json")) return "#eab875"
        if (lower.endsWith(".md")) return "#8ec5fc"
        if (lower.endsWith(".frag") || lower.endsWith(".vert") || lower.endsWith(".glsl") || lower.endsWith(".css") || lower.endsWith(".html")) return "#d49ef5"
        if (lower.endsWith(".sh") || lower.endsWith(".bat") || lower.endsWith(".cmd") || lower.endsWith(".ps1")) return "#7cd992"
        if (lower.endsWith(".toml") || lower.endsWith(".yaml") || lower.endsWith(".yml")) return "#e89d5a"
        return "#8eb4dc"
    }
    function languageLabel(path) {
        const lower = String(path || "").toLowerCase()
        if (lower.endsWith(".py")) return "Python"
        if (lower.endsWith(".qml")) return "QML"
        if (lower.endsWith(".ts") || lower.endsWith(".tsx")) return "TypeScript"
        if (lower.endsWith(".js") || lower.endsWith(".jsx") || lower.endsWith(".mjs")) return "JavaScript"
        if (lower.endsWith(".json")) return "JSON"
        if (lower.endsWith(".md")) return "Markdown"
        if (lower.endsWith(".frag") || lower.endsWith(".vert") || lower.endsWith(".glsl")) return "GLSL"
        if (lower.endsWith(".html")) return "HTML"
        if (lower.endsWith(".css")) return "CSS"
        if (lower.endsWith(".toml")) return "TOML"
        if (lower.endsWith(".yaml") || lower.endsWith(".yml")) return "YAML"
        if (lower.endsWith(".c") || lower.endsWith(".h") || lower.endsWith(".cpp")) return "C/C++"
        if (lower.endsWith(".sh") || lower.endsWith(".bat") || lower.endsWith(".cmd") || lower.endsWith(".ps1")) return "Shell"
        return "Plain Text"
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
    PageSurface { anchors.fill: parent; material: page.material; radius: page.ide ? 0 : 20; tint: page.ide ? "#121721" : "#22232d39" }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: page.ide ? 0 : 18; spacing: page.ide ? 0 : 12
        Text { visible: !page.ide; Layout.fillWidth: true; text: page.viewData.hint || "正在载入…"; color: "#9eacbc"; font.pixelSize: 12; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideMiddle }
        Flow {
            visible: !page.ide; Layout.fillWidth: true; spacing: 7
            Repeater { model: page.viewData.actions || []
                PageButton { required property var modelData; material: page.material; text: modelData.label; enabled: !workbench.busy || modelData.key === "cancel" || (page.pageKey === "computer" && modelData.key === "revoke"); ink: modelData.danger ? Theme.danger : Theme.textPrimary; onClicked: page.invoke(modelData.key) }
            }
            PageButton { material: page.material; glyph: "refresh-cw"; tip: "刷新"; enabled: !workbench.busy; onClicked: page.invoke("refresh") }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: page.files
            RowLayout {
                anchors.fill: parent; spacing: page.ide ? 0 : 12
                Rectangle {
                    visible: page.ide; Layout.preferredWidth: 43; Layout.fillHeight: true; color: "#111620"
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
                    Rectangle {
                        visible: !page.ide || tabs.width > 0
                        Layout.fillWidth: true; Layout.preferredHeight: page.ide ? 35 : 34
                        color: page.ide ? "#0f141e" : "transparent"
                        Rectangle { visible: page.ide; anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#1e2736" }
                        Flickable {
                            id: tabStrip
                            anchors.fill: parent; contentWidth: tabs.width; clip: true
                            boundsBehavior: Flickable.StopAtBounds
                            flickableDirection: Flickable.HorizontalFlick
                            Row { id: tabs; spacing: page.ide ? 0 : 5
                                Repeater { model: page.viewData.tabs || []
                                    Item {
                                        id: fileTab
                                        required property var modelData
                                        readonly property bool selected: page.editorPath === modelData.path
                                        readonly property bool dirty: selected ? page.bufferDirty : !!modelData.dirty
                                        width: page.ide ? ideTabRow.implicitWidth : classicTabRow.implicitWidth
                                        height: page.ide ? 35 : 32
                                        Component.onCompleted: if (selected) Qt.callLater(reveal)
                                        onSelectedChanged: if (selected) Qt.callLater(reveal)
                                        function reveal() {
                                            if (x < tabStrip.contentX) tabStrip.contentX = x
                                            else if (x + width > tabStrip.contentX + tabStrip.width)
                                                tabStrip.contentX = x + width - tabStrip.width
                                        }
                                        // Classic non-IDE tab (Files page)
                                        Row {
                                            id: classicTabRow; visible: !page.ide; spacing: 2
                                            PageButton {
                                                material: page.material; height: 32
                                                text: fileTab.modelData.name + (fileTab.dirty ? " •" : "")
                                                tip: fileTab.modelData.path; selected: fileTab.selected; enabled: !workbench.busy
                                                onClicked: page.invoke("open", {path:fileTab.modelData.path})
                                            }
                                            PageButton {
                                                objectName: page.ide ? "" : "closeEditorTab"; material: page.material; width: 28; height: 32; glyph: "x"; quiet: true
                                                tip: "关闭 " + fileTab.modelData.name; enabled: !workbench.busy
                                                onClicked: page.invoke("close", {path:fileTab.modelData.path})
                                            }
                                        }
                                        // VS Code style IDE tab
                                        Rectangle {
                                            visible: page.ide; anchors.fill: parent
                                            color: fileTab.selected ? "#131822" : (tabHover.hovered ? "#171f2d" : "#0f141e")
                                            HoverHandler { id: tabHover }
                                            Rectangle { visible: fileTab.selected; width: parent.width; height: 2; color: "#5b9bf8" }
                                            Rectangle { visible: !fileTab.selected; anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#1e2736" }
                                            Rectangle { anchors.right: parent.right; width: 1; height: parent.height; color: "#1c2534" }
                                            Row {
                                                id: ideTabRow; height: parent.height; spacing: 0
                                                AbstractButton {
                                                    height: parent.height; implicitWidth: tabLabelRow.implicitWidth + 18
                                                    enabled: !workbench.busy; hoverEnabled: true
                                                    ToolTip.visible: hovered; ToolTip.text: fileTab.modelData.path; ToolTip.delay: 500
                                                    background: null
                                                    contentItem: Row {
                                                        id: tabLabelRow; spacing: 6; leftPadding: 11; rightPadding: 4
                                                        Glyph {
                                                            name: "file-code"; width: 14; height: 14
                                                            ink: page.fileIconColor(fileTab.modelData.name)
                                                            glowing: fileTab.selected
                                                            anchors.verticalCenter: parent.verticalCenter
                                                        }
                                                        Text {
                                                            text: fileTab.modelData.name
                                                            color: fileTab.selected ? "#edf2fa" : "#8b98ab"
                                                            font.pixelSize: 12; font.weight: fileTab.selected ? Font.Medium : Font.Normal
                                                            anchors.verticalCenter: parent.verticalCenter
                                                        }
                                                        Rectangle {
                                                            visible: fileTab.dirty
                                                            width: 6; height: 6; radius: 3; color: "#eab875"
                                                            anchors.verticalCenter: parent.verticalCenter
                                                        }
                                                    }
                                                    onClicked: page.invoke("open", {path:fileTab.modelData.path})
                                                }
                                                IdeButton {
                                                    objectName: page.ide ? "closeEditorTab" : ""
                                                    width: 24; height: 24; anchors.verticalCenter: parent.verticalCenter
                                                    glyph: "x"; tip: "关闭 " + fileTab.modelData.name
                                                    enabled: !workbench.busy
                                                    opacity: fileTab.selected || tabHover.hovered || hovered ? 0.92 : 0.35
                                                    onClicked: page.invoke("close", {path:fileTab.modelData.path})
                                                }
                                                Item { width: 5; height: 1 }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                    Rectangle {
                        visible: page.ide; Layout.fillWidth: true; Layout.preferredHeight: 28; color: "#131822"
                        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#1c2534" }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 6; spacing: 4
                            Glyph {
                                visible: page.hasDocument; name: "file-code"
                                Layout.preferredWidth: 13; Layout.preferredHeight: 13
                                ink: page.fileIconColor(page.editorPath)
                            }
                            Text {
                                Layout.fillWidth: true
                                text: page.editorPath ? page.editorPath.slice((page.viewData.hint || "").length + 1).replace(/[\\\\/]/g, "  ›  ") : "编辑器"
                                elide: Text.ElideMiddle; color: page.hasDocument ? "#b6c2d3" : "#6c7b90"; font.pixelSize: 12
                            }
                            IdeButton { glyph: "search"; tip: "在文件中查找 · Ctrl+F"; enabled: page.hasDocument && !workbench.busy; selected: page.findOpen; Layout.preferredHeight: 23; onClicked: page.openFind() }
                            IdeButton { glyph: "panels-top-left"; tip: "原文参考分屏"; enabled: page.hasDocument && !workbench.busy; selected: page.referenceOpen; Layout.preferredHeight: 23; onClicked: page.invoke("reference") }
                            IdeButton { glyph: "save"; tip: "保存 · Ctrl+S"; enabled: page.hasDocument && !workbench.busy; Layout.preferredHeight: 23; onClicked: page.invoke("save", {path:page.editorPath}) }
                            IdeButton { glyph: "sliders-horizontal"; tip: "更多编辑器操作"; enabled: page.hasDocument && !workbench.busy; Layout.preferredHeight: 23; onClicked: editMenu.open()
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
                        Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                        PageSurface { anchors.fill: parent; material: page.material; radius: page.ide ? 0 : 14; tint: page.ide ? "#131822" : "#50131b25"; emphasis: !page.ide && code.editor.activeFocus ? 0.5 : 0 }
                        // VS Code style subtle gutter background strip
                        Rectangle {
                            visible: page.ide && gutter.visible
                            x: 0; y: 0; width: gutter.x + gutter.width + 2; height: parent.height
                            color: "#10151f"
                        }
                        // VS Code style active current-line highlight band
                        Rectangle {
                            visible: page.hasDocument && code.editor.cursorRectangle.height > 0
                            x: page.ide ? 0 : 8
                            y: code.y + code.editor.cursorRectangle.y - code.contentY
                            width: page.referenceOpen ? parent.width / 2 - x : (page.ide ? parent.width : parent.width - 16)
                            height: code.editor.cursorRectangle.height
                            color: "#165b9bf8"
                            Rectangle { width: parent.width; height: 1; color: "#1c5b9bf8" }
                            Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#1c5b9bf8" }
                            Rectangle { visible: page.ide; width: 2; height: parent.height; color: "#5b9bf8" }
                        }
                        Item {
                            id: gutter; objectName: "editorLineNumbers"
                            visible: page.hasDocument; clip: true
                            x: page.ide ? 6 : 8; y: code.y; width: visible ? Math.max(34, String(page.lineStarts.length).length * 8 + 16) : 0; height: code.height
                            readonly property int firstLine: page.indexAt(page.lineStarts, Math.max(0, code.editor.positionAt(1, code.contentY)))
                            readonly property real lineHeight: Math.max(1, code.editor.cursorRectangle.height)
                            Repeater {
                                model: gutter.visible ? Math.min(page.lineStarts.length - gutter.firstLine, Math.ceil(gutter.height / gutter.lineHeight) + 3) : 0
                                Text {
                                    required property int index
                                    readonly property int line: gutter.firstLine + index
                                    readonly property rect location: code.editor.positionToRectangle(page.lineStarts[line] || 0)
                                    y: location.y - code.contentY; width: gutter.width - 10; height: location.height
                                    text: line + 1
                                    color: line === page.cursorLine ? "#e6eef8" : "#56657c"
                                    opacity: line === page.cursorLine ? 1.0 : 0.85
                                    font.family: code.editor.font.family
                                    font.pixelSize: code.editor.font.pixelSize
                                    font.weight: line === page.cursorLine ? Font.DemiBold : Font.Normal
                                    horizontalAlignment: Text.AlignRight; verticalAlignment: Text.AlignVCenter
                                }
                            }
                        }
                        Rectangle {
                            visible: gutter.visible
                            x: gutter.x + gutter.width + 1; y: page.ide ? 0 : 12; width: 1; height: page.ide ? parent.height : parent.height - 24
                            color: page.ide ? "#1e2736" : "#16ffffff"
                        }
                        Rectangle {
                            visible: page.referenceOpen
                            x: parent.width / 2; y: page.ide ? 0 : 12; width: 1; height: page.ide ? parent.height : parent.height - 24
                            color: page.ide ? "#243044" : "#1cffffff"
                        }
                        ComposerText {
                            id: code; objectName: "nativeCodeEditor"; editorName: "codeEditorInput"
                            anchors.fill: parent; anchors.margins: page.ide ? 10 : 14
                            anchors.leftMargin: gutter.visible ? gutter.x + gutter.width + 14 : (page.ide ? 16 : 14)
                            anchors.rightMargin: page.referenceOpen ? parent.width / 2 + 7 : (page.ide ? 10 : 14)
                            submitOnEnter: false; horizontalScroll: true
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
                        // VS Code style empty editor watermark & shortcut guide
                        Column {
                            visible: page.ide && !page.hasDocument; anchors.centerIn: parent; spacing: 18
                            Image {
                                source: brandUrl; width: 64; height: 64; smooth: true; mipmap: true
                                opacity: 0.16; anchors.horizontalCenter: parent.horizontalCenter
                            }
                            Column {
                                spacing: 10; anchors.horizontalCenter: parent.horizontalCenter
                                Row {
                                    spacing: 16; anchors.horizontalCenter: parent.horizontalCenter
                                    IdeButton { glyph: "file-code"; text: "快速打开文件"; tip: "Ctrl+P"; width: 136; onClicked: page.openQuick() }
                                    Row {
                                        spacing: 4; anchors.verticalCenter: parent.verticalCenter
                                        Rectangle { width: 34; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "Ctrl"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                        Rectangle { width: 22; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "P"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                    }
                                }
                                Row {
                                    spacing: 16; anchors.horizontalCenter: parent.horizontalCenter
                                    IdeButton { glyph: "search"; text: "工作区搜索"; tip: "Ctrl+Shift+F"; width: 136; onClicked: page.openSearch() }
                                    Row {
                                        spacing: 4; anchors.verticalCenter: parent.verticalCenter
                                        Rectangle { width: 34; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "Ctrl"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                        Rectangle { width: 38; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "Shift"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                        Rectangle { width: 22; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "F"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                    }
                                }
                                Row {
                                    spacing: 16; anchors.horizontalCenter: parent.horizontalCenter
                                    IdeButton { glyph: "square-terminal"; text: "切换内置终端"; tip: "Ctrl+`"; width: 136; onClicked: page.toggleDock("terminal") }
                                    Row {
                                        spacing: 4; anchors.verticalCenter: parent.verticalCenter
                                        Rectangle { width: 34; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "Ctrl"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                        Rectangle { width: 22; height: 22; radius: 4; color: "#1a2230"; border.color: "#2c394f"; Text { anchors.centerIn: parent; text: "`"; color: "#9fb0c6"; font.pixelSize: 11 } }
                                    }
                                }
                            }
                        }
                        ComposerText {
                            visible: page.referenceOpen; x: parent.width / 2 + 7; y: page.ide ? 10 : 14; width: parent.width / 2 - 17; height: parent.height - (page.ide ? 20 : 28)
                            text: (page.viewData.document || {}).original || ""; submitOnEnter: false; horizontalScroll: true
                            editor.readOnly: true; editor.font.family: "Cascadia Code"; editor.font.pixelSize: 13
                            Component.onCompleted: controller.formatCode(editor.textDocument)
                            ScrollBar.vertical: ScrollBar {}
                            ScrollBar.horizontal: ScrollBar {}
                        }
                    }
                    Rectangle {
                        visible: page.findOpen || page.jumpOpen
                        Layout.fillWidth: true
                        Layout.preferredHeight: navBarCol.implicitHeight + (page.ide ? 12 : 0)
                        color: page.ide ? "#151c28" : "transparent"
                        Rectangle { visible: page.ide; width: parent.width; height: 1; color: "#263348" }
                        Rectangle { visible: page.ide; width: 2; height: parent.height; color: "#5b9bf8" }
                        ColumnLayout {
                            id: navBarCol
                            anchors.fill: parent
                            anchors.leftMargin: page.ide ? 10 : 0; anchors.rightMargin: page.ide ? 8 : 0
                            anchors.topMargin: page.ide ? 6 : 0; anchors.bottomMargin: page.ide ? 6 : 0
                            spacing: 6
                            RowLayout {
                                visible: page.findOpen; Layout.fillWidth: true; spacing: 6
                                TextField { id: findText; objectName: "editorFindInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "在文件中查找"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: page.ide ? 5 : 9; tint: page.ide ? "#10151f" : Theme.glassTint; emphasis: findText.activeFocus ? 0.65 : 0 } onAccepted: page.findMatch(false) }
                                Text { objectName: "editorMatchCount"; visible: !!findText.text; color: Theme.textSecondary; font.pixelSize: 12; text: !page.matches.length ? "无匹配" : page.selectedMatch >= 0 ? (page.selectedMatch + 1) + " / " + page.matches.length : page.matches.length + " 处" }
                                PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "chevron-up"; tip: "上一处 · Shift+F3"; enabled: !!page.matches.length; onClicked: page.findMatch(true) }
                                PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "chevron-down"; tip: "下一处 · F3"; enabled: !!page.matches.length; onClicked: page.findMatch(false) }
                                PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "x"; tip: "关闭查找 · Esc"; onClicked: page.closeNavigation() }
                            }
                            RowLayout {
                                visible: page.findOpen; Layout.fillWidth: true; spacing: 6
                                TextField { id: replaceText; objectName: "editorReplaceInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "替换为"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: page.ide ? 5 : 9; tint: page.ide ? "#10151f" : Theme.glassTint; emphasis: replaceText.activeFocus ? 0.65 : 0 } onAccepted: if (!workbench.busy && page.matches.length) page.replaceMatch() }
                                PageButton { objectName: "replaceEditorMatch"; material: page.material; text: "替换"; enabled: !workbench.busy && !!page.matches.length; onClicked: page.replaceMatch() }
                            }
                            RowLayout {
                                visible: page.jumpOpen; Layout.fillWidth: true; spacing: 6
                                TextField { id: jumpText; objectName: "editorJumpInput"; Layout.fillWidth: true; color: Theme.textPrimary; placeholderText: "行:列"; placeholderTextColor: Theme.textMuted; font.pixelSize: 12; selectByMouse: true; leftPadding: 10; rightPadding: 10; background: PageSurface { material: page.material; radius: page.ide ? 5 : 9; tint: page.ide ? "#10151f" : Theme.glassTint; emphasis: jumpText.activeFocus ? 0.65 : 0 } onAccepted: page.jumpToPosition() }
                                Text { color: Theme.textSecondary; text: "第 1–" + page.lineStarts.length + " 行"; font.pixelSize: 12 }
                                PageButton { material: page.material; text: "跳转"; enabled: page.jumpOffset(jumpText.text) >= 0; onClicked: page.jumpToPosition() }
                                PageButton { material: page.material; Layout.preferredWidth: 30; glyph: "x"; tip: "取消跳转 · Esc"; onClicked: page.closeNavigation() }
                            }
                        }
                    }
                    Loader {
                        id: dock
                        visible: page.ide && page.dockOpen; active: page.ide && page.dockVisited
                        Layout.fillWidth: true; Layout.preferredHeight: Math.min(page.dockHeight, page.height * 0.56)
                        sourceComponent: CodeTerminalPanel {
                            resultText: page.viewData.result || ""; tab: page.dockTab
                            onTabRequested: name => page.dockTab = name
                            onCollapseRequested: page.toggleDock(page.dockTab)
                            onResizeRequested: pixels => page.dockHeight = Math.min(page.height * 0.56, Math.max(160, pixels))
                        }
                    }
                    Rectangle {
                        visible: page.ide || page.hasDocument; Layout.fillWidth: true; Layout.preferredHeight: 26
                        color: page.ide ? "#111722" : "transparent"
                        Rectangle { visible: page.ide; width: parent.width; height: 1; color: "#222e42" }
                        RowLayout {
                            anchors.fill: parent; anchors.leftMargin: page.ide ? 0 : 8; anchors.rightMargin: 8; spacing: 8
                            Rectangle {
                                visible: page.ide; Layout.fillHeight: true; Layout.preferredWidth: wsBadgeRow.implicitWidth + 18
                                color: "#1c3354"
                                Row {
                                    id: wsBadgeRow; anchors.centerIn: parent; spacing: 5
                                    Glyph { name: "folder"; width: 12; height: 12; ink: "#8ec0ff"; anchors.verticalCenter: parent.verticalCenter }
                                    Text {
                                        text: workbench.workspace.replace(/\\/g, "/").split("/").pop() || "Workspace"
                                        color: "#dce9fa"; font.pixelSize: 11; font.weight: Font.Medium
                                        anchors.verticalCenter: parent.verticalCenter
                                    }
                                }
                            }
                            Text {
                                visible: !page.ide; Layout.fillWidth: true
                                text: page.editorPath; color: Theme.textMuted; elide: Text.ElideMiddle; font.pixelSize: 11
                            }
                            Item { visible: page.ide; Layout.fillWidth: true }
                            Text { text: (page.viewData.document || {}).status === "conflict" ? "磁盘有变更" : page.bufferDirty ? "● 未保存" : ""; color: Theme.warning; font.pixelSize: 11 }
                            PageButton { visible: page.hasDocument; objectName: "editorPosition"; material: page.material; quiet: true; Layout.preferredHeight: 22; text: "行 " + (page.cursorLine + 1) + "，列 " + page.cursorColumn; tip: "跳转到行 · Ctrl+G"; onClicked: page.openJump() }
                            Text { visible: page.hasDocument; text: ((page.viewData.document || {}).encoding || "UTF-8").toUpperCase(); color: Theme.textMuted; font.pixelSize: 11 }
                            Text { visible: page.ide && page.hasDocument; text: page.languageLabel(page.editorPath); color: "#8fb4dc"; font.pixelSize: 11 }
                            IdeButton { visible: page.ide; glyph: "square-terminal"; tip: "切换终端 · Ctrl+`"; selected: page.dockOpen && page.dockTab === "terminal"; Layout.preferredHeight: 22; onClicked: page.toggleDock("terminal") }
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
                TodoList { Layout.fillWidth: true; items: page.viewData.todos || []; running: workbench.busy }
                ComputerPreview {
                    Layout.fillWidth: true; visible: page.pageKey === "computer" && !!imageSource
                    material: page.material; enabled: !workbench.busy
                    imageSource: page.viewData.image || ""; observationId: page.viewData.observationId || ""
                    proposedPoint: page.viewData.point || null; canInput: !!page.viewData.canInput
                    onClickRequested: (observation, x, y) => page.invoke("click", {observation_id: observation, x: x, y: y})
                }
                Repeater {
                    id: fields; model: page.schema
                    WorkbenchField {
                        required property var modelData
                        Layout.fillWidth: true; material: page.material; definition: modelData
                        onEdited: value => {
                            page.values[definition.key] = value
                            if (page.pageKey === "computer" && ["model", "consent", "clicks"].includes(definition.key)) page.invoke("settings", {changed: definition.key})
                            if (["config", "agents", "connector_accounts"].includes(page.pageKey)) workbench.setFormDirty(page.pageKey, true)
                        }
                    }
                }
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
