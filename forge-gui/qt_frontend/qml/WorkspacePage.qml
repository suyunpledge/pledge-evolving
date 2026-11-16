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
    property var schema: []
    property var values: ({})
    property bool initialized: false
    property string editorPath: ""
    property int editorVersion: 0
    property bool syncing: false
    property bool outputOpen: false
    property bool findOpen: false
    property bool referenceOpen: false
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
            editorPath = doc.path
            editorVersion = Number(doc.version || 0)
            if (code.text !== doc.text) code.text = doc.text
            syncing = false
        } else if (!doc.path && files) {
            syncing = true; editorPath = ""; code.text = ""; syncing = false
        }
        if (viewData.result) outputOpen = true
    }
    onViewDataChanged: sync()
    Component.onCompleted: sync()
    function invoke(action, row) {
        if (action === "chooseProject") { projectPicker.open(); return }
        if (action === "find") { findOpen = !findOpen; if (findOpen) findText.forceActiveFocus(); return }
        if (action === "reference") { referenceOpen = !referenceOpen; return }
        if (action === "saveAs") { savePicker.open(); return }
        const payload = Object.assign({}, values, row || {})
        workbench.pageAction(pageKey, action, JSON.stringify(payload))
    }
    Connections {
        target: workbench
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
    function findNext() {
        if (!findText.text) return
        let position = code.text.indexOf(findText.text, code.editor.selectionEnd)
        if (position < 0) position = code.text.indexOf(findText.text)
        if (position >= 0) code.editor.select(position, position + findText.text.length)
    }

    GlassSurface { anchors.fill: parent; material: page.material; radius: 20; tint: "#22232d39" }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 18; spacing: 12
        Text { Layout.fillWidth: true; text: page.viewData.hint || "正在载入…"; color: "#9eacbc"; font.pixelSize: 12; wrapMode: Text.Wrap; maximumLineCount: 3; elide: Text.ElideMiddle }
        Flow {
            Layout.fillWidth: true; spacing: 7
            Repeater { model: page.viewData.actions || []
                GlassButton { required property var modelData; material: page.material; text: modelData.label; enabled: !workbench.busy || modelData.key === "cancel"; ink: modelData.danger ? "#e9aaa4" : "#e1e5e9"; onClicked: page.invoke(modelData.key) }
            }
            GlassButton { material: page.material; glyph: "refresh"; tip: "刷新"; enabled: !workbench.busy; onClicked: page.invoke("refresh") }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            visible: page.files
            RowLayout {
                anchors.fill: parent; spacing: 12
                ColumnLayout {
                    Layout.preferredWidth: Math.min(200, parent.width * 0.3); Layout.fillHeight: true; spacing: 7
                    Text { Layout.fillWidth: true; text: page.viewData.directory || "项目文件"; color: "#8897a8"; elide: Text.ElideMiddle; font.pixelSize: 11 }
                    TextField {
                        id: fileQuery; objectName: "projectFileSearch"; Layout.fillWidth: true; placeholderText: "查找文件 · Enter"
                        color: "#e4eaf1"; placeholderTextColor: "#8293a5"; font.pixelSize: 12; selectByMouse: true
                        background: GlassSurface { material: page.material; radius: 9 }
                        onAccepted: page.invoke("search", {query:text})
                    }
                    ListView {
                        id: tree; objectName: "projectFiles"; Layout.fillWidth: true; Layout.fillHeight: true
                        model: page.viewData.rows || []; clip: true; spacing: 3
                        ScrollBar.vertical: ScrollBar {}
                        delegate: GlassButton {
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
                    Layout.fillWidth: true; Layout.fillHeight: true; spacing: 9
                    Flickable {
                        Layout.fillWidth: true; Layout.preferredHeight: 34; contentWidth: tabs.width; clip: true
                        Row { id: tabs; spacing: 5
                            Repeater { model: page.viewData.tabs || []
                                GlassButton { required property var modelData; material: page.material; height: 32; text: modelData.name + (modelData.dirty ? " •" : ""); selected: page.editorPath === modelData.path; onClicked: page.invoke("open", {path:modelData.path}) }
                            }
                        }
                    }
                    Item {
                        Layout.fillWidth: true; Layout.fillHeight: true
                        GlassSurface { anchors.fill: parent; material: page.material; radius: 14; tint: "#50131b25"; emphasis: code.editor.activeFocus ? 0.5 : 0 }
                        ComposerText {
                            id: code; objectName: "nativeCodeEditor"; editorName: "codeEditorInput"
                            anchors.fill: parent; anchors.margins: 14; anchors.rightMargin: page.referenceOpen ? parent.width / 2 + 7 : 14; submitOnEnter: false; horizontalScroll: true
                            placeholder: "选择文件开始编辑"
                            editor.readOnly: !page.editorPath || workbench.busy
                            editor.font.family: "Cascadia Code"; editor.font.pixelSize: 13
                            Component.onCompleted: controller.formatCode(editor.textDocument)
                            onTextChanged: {
                                if (!page.syncing && page.editorPath && editor.activeFocus) {
                                    page.editorVersion++
                                    workbench.updateDocument(page.editorPath, text, page.editorVersion)
                                }
                            }
                            ScrollBar.vertical: ScrollBar {}
                            ScrollBar.horizontal: ScrollBar {}
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
                        visible: page.findOpen; Layout.fillWidth: true
                        TextField { id: findText; Layout.fillWidth: true; color: "#edf1f5"; placeholderText: "查找"; background: GlassSurface { material: page.material; radius: 9 } onAccepted: page.findNext() }
                        TextField { id: replaceText; Layout.fillWidth: true; color: "#edf1f5"; placeholderText: "替换为"; background: GlassSurface { material: page.material; radius: 9 } }
                        GlassButton { material: page.material; text: "下一处"; onClicked: page.findNext() }
                        GlassButton { material: page.material; text: "替换"; enabled: !workbench.busy && !!findText.text; onClicked: { if (code.editor.selectedText === findText.text) {code.editor.forceActiveFocus(); const start = code.editor.selectionStart; code.editor.remove(start, code.editor.selectionEnd); code.editor.insert(start, replaceText.text)} page.findNext() } }
                    }
                    Text { Layout.fillWidth: true; text: page.editorPath ? "行 " + code.text.slice(0, code.editor.cursorPosition).split("\n").length + " · " + ((page.viewData.document || {}).encoding || "") + " · " + ((page.viewData.document || {}).status || "") + " · " + page.editorPath : ""; color: "#8c9aab"; elide: Text.ElideMiddle; font.pixelSize: 11 }
                    Flow {
                        Layout.fillWidth: true; spacing: 6
                        Repeater { model: [{key:"save",label:"保存"},{key:"saveAs",label:"另存为"},{key:"find",label:"查找 / 替换"},{key:"reference",label:"原文参考"},{key:"compare",label:"比较磁盘版本"},{key:"merge",label:"合并"},{key:"discard",label:"重新载入"},{key:"close",label:"关闭文件"}]
                            GlassButton { required property var modelData; material: page.material; text: modelData.label; enabled: !!page.editorPath && !workbench.busy; onClicked: page.invoke(modelData.key, {path:page.editorPath}) }
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
            visible: !!page.viewData.result; Layout.fillWidth: true; spacing: 7
            GlassButton { material: page.material; text: page.outputOpen ? "收起结果" : "展开结果"; onClicked: page.outputOpen = !page.outputOpen }
            ScrollView {
                visible: page.outputOpen; Layout.fillWidth: true; Layout.preferredHeight: Math.min(240, page.height * 0.3); clip: true
                TextArea { text: page.viewData.result || ""; readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: "#c5d1dd"; font.family: "Cascadia Code"; font.pixelSize: 12; background: null }
            }
        }
    }
    Shortcut { sequence: "Ctrl+S"; enabled: page.visible && page.files && !!page.editorPath && !workbench.busy; onActivated: page.invoke("save", {path:page.editorPath}) }
    Shortcut { sequence: "Ctrl+F"; enabled: page.visible && page.files && !!page.editorPath; onActivated: page.invoke("find") }
    Shortcut { sequence: "Ctrl+P"; enabled: page.visible && page.files; onActivated: fileQuery.forceActiveFocus() }
}
