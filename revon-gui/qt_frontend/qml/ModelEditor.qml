import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 管理已有模型：左侧选行，右侧改接口地址、模型 ID、显示名称、协议、思考档位、温度与密钥。
// 密钥只写不读：输入框永远为空，留空表示保持原密钥。
Popup {
    id: editor
    objectName: "modelEditor"
    required property var material
    required property var service
    property bool changesAllowed: true
    property string currentId: ""
    property bool confirmDelete: false
    readonly property var row: {
        const rows = service.rows || []
        for (let i = 0; i < rows.length; ++i) if (rows[i].id === currentId) return rows[i]
        return null
    }
    readonly property var effortLevels: ["low", "medium", "high", "max"]
    property var chosenEfforts: []
    signal addRequested()
    modal: true; focus: true; padding: 0
    closePolicy: Popup.CloseOnEscape
    Overlay.modal: Rectangle { color: "#6605080d" }
    background: GlassSurface { material: editor.material; radius: 24; emphasis: 0.6 }
    onOpened: {
        if (row && labelField.enabled) labelField.forceActiveFocus(Qt.TabFocusReason)
        else addModel.forceActiveFocus(Qt.TabFocusReason)
    }

    function openFor(rowId) {
        service.reload()
        const rows = service.rows || []
        currentId = rowId && rows.some(r => r.id === rowId) ? rowId : (rows.length ? rows[0].id : "")
        load()
        open()
    }
    function load() {
        confirmDelete = false
        keyField.clear()
        service.clearStatus()
        const r = row
        if (!r) return
        labelField.text = r.label
        urlField.text = r.baseURL
        modelField.text = r.model
        temperatureField.text = r.temperature
        wireBox.currentIndex = Math.max(0, wireBox.indexOfValue(r.wire))
        if (r.efforts === "auto") { effortMode.currentIndex = 0; chosenEfforts = [] }
        else if (r.efforts === "none") { effortMode.currentIndex = 2; chosenEfforts = [] }
        else { effortMode.currentIndex = 1; chosenEfforts = r.efforts.split(",") }
    }
    function values() {
        const mode = effortMode.currentValue
        return {
            label: labelField.text, baseURL: urlField.text, model: modelField.text,
            wire: wireBox.currentValue, temperature: temperatureField.text,
            efforts: mode === "custom" ? (chosenEfforts.length ? chosenEfforts.join(",") : "none") : mode
        }
    }
    function toggleEffort(level) {
        const next = chosenEfforts.slice()
        const at = next.indexOf(level)
        if (at >= 0) next.splice(at, 1); else next.push(level)
        chosenEfforts = effortLevels.filter(l => next.indexOf(l) >= 0)
        service.clearStatus()
    }
    function keyStateText(state, ref) {
        return state === "saved" ? "密钥已保存" : state === "missing" ? "未找到密钥" : state === "none" ? "无需密钥"
             : state === "inline" ? "明文密钥（建议更换）" : "自定义密钥表达式"
    }
    Connections {
        target: editor.service
        function onSaved(rowId) { if (rowId === "") editor.openFor("") ; else keyField.clear() }
    }

    contentItem: Item {
        implicitWidth: 860; implicitHeight: 600
        Text { x: 26; y: 22; text: "模型配置"; color: Theme.textPrimary; font.pixelSize: 20; font.weight: Font.DemiBold }
        Text { x: 26; y: 52; text: "编辑已添加的模型；密钥保存在本机密钥库，不会在此显示。"; color: Theme.textMuted; font.pixelSize: 12 }
        GlassButton {
            x: parent.width - width - 18; y: 16; material: editor.material; glyph: "x"; quiet: true; tip: "关闭"
            onClicked: editor.close()
        }
        // —— 左：模型列表 ——
        ListView {
            id: list
            objectName: "modelEditorList"
            x: 16; y: 84; width: 268; height: parent.height - y - 66
            model: editor.service.rows; spacing: 6; clip: true
            ScrollBar.vertical: ScrollBar { width: 4 }
            delegate: AbstractButton {
                id: item
                required property var modelData
                readonly property bool current: editor.currentId === modelData.id
                Accessible.name: item.modelData.title
                Accessible.role: Accessible.ListItem
                Accessible.selectable: true
                Accessible.selected: current
                width: list.width; height: 58; hoverEnabled: true
                leftPadding: 12; rightPadding: 12
                background: GlassSurface {
                    material: editor.material; radius: 13
                    visible: item.current || item.hovered
                    emphasis: item.current ? 1 : 0.2; hoverAmount: item.hovered ? 1 : 0
                }
                contentItem: Column {
                    spacing: 4; topPadding: 9
                    Text { width: parent.width; text: item.modelData.title; elide: Text.ElideRight; color: item.current ? Theme.textPrimary : Theme.textSecondary; font.pixelSize: 13; font.weight: item.current ? Font.Medium : Font.Normal }
                    Text {
                        width: parent.width; elide: Text.ElideRight; font.pixelSize: 11
                        text: item.modelData.model + (item.modelData.disabled ? " · 已停用" : "") + (item.modelData.keyState === "missing" ? " · 缺少密钥" : "")
                        color: item.modelData.keyState === "missing" ? Theme.warning : Theme.textMuted
                    }
                }
                onClicked: { editor.currentId = modelData.id; editor.load() }
            }
        }
        Text {
            visible: list.count === 0; x: 28; y: 96; width: 240; wrapMode: Text.Wrap
            text: "还没有模型。点「添加模型」接入第一个。"; color: Theme.textMuted; font.pixelSize: 12
        }
        GlassButton {
            id: addModel
            x: 16; y: parent.height - 52; width: 268; material: editor.material; glyph: "plus"; text: "添加模型"
            onClicked: { editor.close(); editor.addRequested() }
        }
        Rectangle { x: 296; y: 84; width: 1; height: parent.height - 100; color: Theme.divider }
        // —— 右：表单 ——
        ScrollView {
            id: formScroll
            x: 312; y: 80; width: parent.width - x - 20
            height: Math.max(0, (editorStatus.visible ? editorStatus.y : editorActions.y) - y - 8)
            clip: true
            enabled: editor.row !== null && editor.changesAllowed && !editor.service.busy
            ColumnLayout {
                width: formScroll.availableWidth - 8
                spacing: 7
                component FieldLabel: Text { color: "#acb9c8"; font.pixelSize: 12; Layout.topMargin: 4 }
                component EditField: TextField {
                    Layout.fillWidth: true; Layout.preferredHeight: 38
                    color: "#edf1f5"; placeholderTextColor: "#8a99ac"
                    selectByMouse: true; leftPadding: 12; rightPadding: 12
                    background: GlassSurface { material: editor.material; radius: 10; tint: "#56212d38"; emphasis: parent.activeFocus ? 0.6 : 0 }
                    onTextEdited: editor.service.clearStatus()
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 7
                        FieldLabel { text: "显示名称" }
                        EditField { id: labelField; objectName: "editLabel"; Accessible.name: "显示名称"; placeholderText: "留空时显示模型 ID" }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 170; Layout.maximumWidth: 170; spacing: 7
                        FieldLabel { text: "协议" }
                        ComboBox {
                            id: wireBox; objectName: "editWire"
                            Accessible.name: "协议"
                            Layout.fillWidth: true; Layout.preferredHeight: 38
                            model: editor.service.wires; textRole: "label"; valueRole: "value"; leftPadding: 12
                            contentItem: Text { text: wireBox.displayText; color: "#edf1f5"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                            background: GlassSurface { material: editor.material; radius: 10; tint: "#56212d38" }
                            onActivated: editor.service.clearStatus()
                        }
                    }
                }
                FieldLabel { text: "接口地址" }
                EditField { id: urlField; objectName: "editEndpoint"; Accessible.name: "接口地址"; placeholderText: "https://api.example.com/v1"; inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText }
                RowLayout {
                    Layout.fillWidth: true; spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 7
                        FieldLabel { text: "模型 ID" }
                        EditField { id: modelField; objectName: "editModel"; Accessible.name: "模型 ID"; inputMethodHints: Qt.ImhNoPredictiveText }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 170; Layout.maximumWidth: 170; spacing: 7
                        FieldLabel { text: "温度（0–2，可留空）" }
                        EditField { id: temperatureField; objectName: "editTemperature"; Accessible.name: "温度"; inputMethodHints: Qt.ImhFormattedNumbersOnly }
                    }
                }
                FieldLabel { text: "思考强度档位" }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    ComboBox {
                        id: effortMode; objectName: "editEffortMode"
                        Accessible.name: "思考强度档位"
                        Layout.preferredWidth: 200; Layout.preferredHeight: 38
                        model: [{value: "auto", label: "自动（按厂商实测）"}, {value: "custom", label: "指定可用档位"}, {value: "none", label: "不支持思考强度"}]
                        textRole: "label"; valueRole: "value"; leftPadding: 12
                        contentItem: Text { text: effortMode.displayText; color: "#edf1f5"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
                        background: GlassSurface { material: editor.material; radius: 10; tint: "#56212d38" }
                        onActivated: editor.service.clearStatus()
                    }
                    Repeater {
                        model: editor.effortLevels
                        GlassButton {
                            required property string modelData
                            visible: effortMode.currentValue === "custom"
                            material: editor.material; text: modelData; height: 34
                            selected: editor.chosenEfforts.indexOf(modelData) >= 0
                            onClicked: editor.toggleEffort(modelData)
                        }
                    }
                }
                FieldLabel {
                    Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.textDim; font.pixelSize: 11
                    text: effortMode.currentValue === "auto" ? "按 revon 的厂商实测画像决定可用档位；未识别的端点原样发送。"
                        : effortMode.currentValue === "none" ? "主界面的思考强度将停用，请求不带思考参数。"
                        : "只有选中的档位可在主界面选择；网关会拒绝列表之外的档位。"
                }
                FieldLabel {
                    id: keyLabel; text: "API 密钥"; Accessible.role: Accessible.StaticText
                    Accessible.name: text; Accessible.labelFor: keyField
                }
                RowLayout {
                    Layout.fillWidth: true; spacing: 10
                    EditField {
                        id: keyField; objectName: "editKey"
                        Accessible.name: keyLabel.text
                        Accessible.labelledBy: keyLabel
                        echoMode: TextInput.Password
                        inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                        placeholderText: editor.row && editor.row.keyState === "saved" ? "已保存（不显示）。留空保持不变，填写则替换" : "填写密钥"
                    }
                    Text {
                        objectName: "editKeyState"
                        text: editor.row ? editor.keyStateText(editor.row.keyState, editor.row.keyRef) : ""
                        color: editor.row && editor.row.keyState === "saved" ? Theme.success : Theme.warning; font.pixelSize: 12
                    }
                }
                FieldLabel {
                    visible: editor.row !== null && editor.row.keyRef.length > 0
                    Layout.fillWidth: true; elide: Text.ElideMiddle; color: Theme.textDim; font.pixelSize: 11
                    text: editor.row ? "引用：env." + editor.row.keyRef : ""
                }
            }
        }
        // —— 底部：状态与操作 ——
        Text {
            id: editorStatus
            objectName: "modelEditorStatus"
            x: 312; y: editorActions.y - height - 6; width: parent.width - x - 20
            visible: text.length > 0
            text: editor.service.status; wrapMode: Text.Wrap; textFormat: Text.PlainText
            color: editor.service.success ? Theme.successText : editor.service.busy ? Theme.textSecondary : Theme.dangerText
            font.pixelSize: 12
        }
        Row {
            id: editorActions
            x: parent.width - width - 20; y: parent.height - 54; spacing: 8
            enabled: editor.row !== null && editor.changesAllowed && !editor.service.busy
            GlassButton {
                objectName: "deleteModel"
                material: editor.material; glyph: "trash-2"; quiet: !editor.confirmDelete
                text: editor.confirmDelete ? "确认删除" : "删除"
                ink: "#e9aaa4"; glyphInk: "#e9aaa4"
                onClicked: editor.confirmDelete ? editor.service.remove(editor.currentId) : (editor.confirmDelete = true)
            }
            GlassButton {
                objectName: "testModel"
                material: editor.material; glyph: "plug"; text: "测试连接"
                onClicked: editor.service.test(editor.currentId, editor.values(), keyField.text)
            }
            GlassButton {
                objectName: "saveModel"
                material: editor.material; glyph: "save"; text: "保存"; primary: true
                onClicked: editor.service.save(editor.currentId, editor.values(), keyField.text)
            }
        }
    }
}
