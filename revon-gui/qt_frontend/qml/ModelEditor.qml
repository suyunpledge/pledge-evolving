import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 供应商由请求地址识别。同一供应商的各条配置保留独立的模型、连接参数和密钥。
Popup {
    id: editor
    objectName: "modelEditor"
    required property var material
    required property var service
    property bool changesAllowed: true
    property string currentId: ""
    property bool confirmDelete: false
    property bool showHistory: false
    readonly property real sidebarWidth: Math.min(246, Math.max(172, width * 0.27))
    readonly property real formX: sidebarWidth + 32
    readonly property var row: {
        const rows = service.rows || []
        for (let i = 0; i < rows.length; ++i) if (rows[i].id === currentId) return rows[i]
        return null
    }
    readonly property var effortLevels: ["low", "medium", "high", "max"]
    property var chosenEfforts: []
    property var tokenEdits: ({})
    readonly property var tokenModelIds: {
        const primary = modelField.text.trim()
        return [primary, ...Object.keys(row?.tokenModels || {}).filter(name => name !== row?.model && name !== primary)].filter(Boolean)
    }
    function loadTokenFields() {
        const id = tokenModelChoice.currentText || modelField.text.trim()
        tokenLimits.load(Object.assign({}, row?.tokenModels?.[id] || {}, tokenEdits[id] || {}))
    }
    readonly property var providers: {
        const groups = new Map()
        const query = providerSearch.text.trim().toLowerCase()
        for (const item of service.rows || []) {
            const key = item.providerKey || item.id
            const name = item.providerName || item.title
            const terms = [name, item.title, item.label, item.modelBrand, item.providerLabel, item.displayBaseURL]
            if (query && !terms.some(value => String(value || "").toLowerCase().includes(query))) continue
            if (!groups.has(key)) groups.set(key, { key: key, name: name, icon: item.providerIcon || "", first: item.id, count: 0 })
            groups.get(key).count++
        }
        return Array.from(groups.values())
    }
    readonly property var providerModels: row ? (service.rows || []).filter(item =>
        (item.providerKey || item.id) === (row.providerKey || row.id)) : []
    signal addRequested()
    signal catalogRequested()
    modal: true; focus: true; padding: 0
    closePolicy: Popup.CloseOnEscape
    Overlay.modal: Rectangle { color: "#6605080d" }
    background: GlassSurface { material: editor.material; radius: 24; emphasis: 0.6 }
    onOpened: {
        if (row && modelField.enabled) modelField.forceActiveFocus(Qt.TabFocusReason)
        else addModel.forceActiveFocus(Qt.TabFocusReason)
    }
    onClosed: keyField.clear()

    function openFor(rowId, rowsReady = false) {
        showHistory = false
        providerSearch.clear()
        if (!rowsReady) service.reload()
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
        tokenEdits = ({})
        labelField.text = r.label
        providerLabel.text = r.providerLabel || ""
        urlField.text = r.baseURL
        modelField.text = r.model
        tokenModelChoice.currentIndex = 0
        loadTokenFields()
        temperatureField.text = r.temperature
        wireBox.currentIndex = Math.max(0, wireBox.indexOfValue(r.wire))
        if (r.efforts === "auto") { effortMode.currentIndex = 0; chosenEfforts = [] }
        else if (r.efforts === "none") { effortMode.currentIndex = 2; chosenEfforts = [] }
        else { effortMode.currentIndex = 1; chosenEfforts = r.efforts.split(",") }
    }
    function values() {
        const mode = effortMode.currentValue
        const edits = {}
        for (const name of tokenModelIds) if (tokenEdits[name]) edits[name] = tokenEdits[name]
        return {
            providerLabel: providerLabel.text, label: labelField.text, baseURL: urlField.text, model: modelField.text,
            wire: wireBox.currentValue, temperature: temperatureField.text,
            tokenEdits: edits,
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
        function onSaved(rowId) {
            if (rowId === "") editor.openFor("")
            else { editor.tokenEdits = ({}); editor.loadTokenFields(); keyField.clear() }
        }
    }

    contentItem: Item {
        implicitWidth: 860; implicitHeight: 600
        Text {
            x: 22; anchors.verticalCenter: headerTabs.verticalCenter
            text: "模型与供应商"; color: Theme.textPrimary; font.pixelSize: 21; font.weight: Font.DemiBold
        }
        Row {
            id: headerTabs; x: editor.formX; y: 16; spacing: 6
            GlassButton { material: editor.material; text: "模型配置"; selected: !editor.showHistory; quiet: true; onClicked: editor.showHistory = false }
            GlassButton {
                objectName: "requestHistoryTab"
                material: editor.material; text: "请求记录"; selected: editor.showHistory; quiet: true
                onClicked: { editor.showHistory = true; editor.service.refreshHistory() }
            }
        }
        GlassButton {
            x: parent.width - width - 18; y: 16; material: editor.material; glyph: "x"; quiet: true; tip: "关闭"
            onClicked: editor.close()
        }
        Rectangle { x: 0; y: 66; width: parent.width; height: 1; color: Theme.divider }
        TextField {
            id: providerSearch; objectName: "modelProviderSearch"; visible: !editor.showHistory
            x: 16; y: 80; width: editor.sidebarWidth - 12; height: 36
            placeholderText: "搜索供应商或模型"; Accessible.name: placeholderText
            onAccepted: {
                if (editor.providers.length && !editor.service.busy) {
                    editor.currentId = editor.providers[0].first
                    editor.load()
                    routeChoice.forceActiveFocus(Qt.TabFocusReason)
                }
            }
            leftPadding: 12; rightPadding: 8; selectByMouse: true
            color: Theme.textPrimary; placeholderTextColor: Theme.textMuted
            background: GlassSurface { material: editor.material; radius: 10; emphasis: providerSearch.activeFocus ? 0.6 : 0 }
        }
        AppListView {
            id: list; objectName: "modelEditorList"; visible: !editor.showHistory
            x: 16; y: 126; width: providerSearch.width; height: Math.max(0, parent.height - y - 64)
            model: editor.providers; spacing: 4; clip: true; enabled: !editor.service.busy
            delegate: GlassRow {
                required property var modelData
                objectName: "providerGroup_" + modelData.key
                material: editor.material; width: list.rowWidth; height: 44
                text: modelData.name; iconUrl: modelData.icon; status: String(modelData.count)
                checked: !!editor.row && (editor.row.providerKey || editor.row.id) === modelData.key
                actionGlyph: checked ? "check" : "chevron-right"
                Accessible.role: Accessible.ListItem; Accessible.selectable: true; Accessible.selected: checked
                onClicked: { if (!checked || providerSearch.text.trim()) { editor.currentId = modelData.first; editor.load() } }
            }
        }
        Text {
            x: list.x; y: list.y + 8; width: list.width
            visible: list.visible && !list.count
            text: providerSearch.text ? "没有匹配项" : "还没有配置供应商"
            color: Theme.textMuted; font.pixelSize: 12; horizontalAlignment: Text.AlignHCenter
        }
        RowLayout {
            visible: !editor.showHistory; x: providerSearch.x; y: parent.height - 54; width: providerSearch.width; spacing: 5
            GlassButton {
                id: addModel; material: editor.material; glyph: "plus"; text: "添加模型"; Layout.fillWidth: true
                onClicked: { editor.close(); editor.addRequested() }
            }
            GlassButton {
                material: editor.material; glyph: "box"; tip: "浏览供应商目录"
                onClicked: { editor.close(); editor.catalogRequested() }
            }
        }
        Rectangle { visible: !editor.showHistory; x: editor.formX - 16; y: 67; width: 1; height: parent.height - y; color: Theme.divider }
        ColumnLayout {
            id: routeHeader; visible: !editor.showHistory && !!editor.row
            x: editor.formX; y: 80; width: parent.width - x - 24; spacing: 5
            RowLayout {
                Layout.fillWidth: true; spacing: 12
                Text {
                    Layout.fillWidth: true; text: editor.row ? editor.row.providerName || editor.row.title : ""
                    textFormat: Text.PlainText; color: Theme.textPrimary; font.pixelSize: 20; font.weight: Font.DemiBold; elide: Text.ElideRight
                }
                Text {
                    text: editor.row && editor.row.disabled ? "已停用" : ""; color: Theme.textMuted; font.pixelSize: 12
                }
                AppComboBox {
                    id: routeChoice; objectName: "providerModelChoice"; Layout.preferredWidth: Math.min(220, routeHeader.width * 0.48)
                    Layout.preferredHeight: 36; Accessible.name: "供应商下的模型配置"
                    enabled: !editor.service.busy
                    model: editor.providerModels; textRole: "choiceTitle"; valueRole: "id"
                    leftPadding: 12
                    contentItem: Text {
                        text: routeChoice.displayText; textFormat: Text.PlainText; color: Theme.textPrimary
                        verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight
                    }
                    background: GlassSurface { material: editor.material; radius: 10; emphasis: routeChoice.activeFocus ? 0.6 : 0 }
                    currentIndex: editor.providerModels.findIndex(r => r.id === editor.currentId)
                    onActivated: { editor.currentId = currentValue; editor.load() }
                }
            }
            Text {
                Layout.fillWidth: true; textFormat: Text.PlainText
                text: editor.row ? [editor.row.planLabel !== editor.row.protocolLabel ? editor.row.planLabel : "", editor.row.protocolLabel,
                    editor.row.modelBrand ? "模型品牌 " + editor.row.modelBrand : ""].filter(Boolean).join("  ·  ") : ""
                color: Theme.textMuted; font.pixelSize: 12; elide: Text.ElideRight
            }
        }
        ProviderRequestHistory {
            visible: editor.showHistory; x: 20; y: 78; width: parent.width - 40; height: parent.height - y - 18
            material: editor.material; service: editor.service
        }
        AppScrollView {
            id: formScroll
            visible: !editor.showHistory && !!editor.row
            x: editor.formX; y: routeHeader.y + routeHeader.height + 12; width: parent.width - x - 20
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
                        FieldLabel { text: "模型 ID" }
                        EditField { id: modelField; objectName: "editModel"; Accessible.name: "模型 ID"; inputMethodHints: Qt.ImhNoPredictiveText }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 170; Layout.maximumWidth: 170; spacing: 7
                        FieldLabel { text: "协议" }
                        AppComboBox {
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
                FieldLabel { text: "接口地址 · Base URL" }
                EditField { id: urlField; objectName: "editEndpoint"; Accessible.name: "接口地址"; placeholderText: "https://api.example.com/v1"; inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText }
                RowLayout {
                    Layout.fillWidth: true; spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 7
                        FieldLabel { text: "网关别名（可选）" }
                        EditField { id: labelField; objectName: "editLabel"; Accessible.name: "网关别名"; placeholderText: "留空时沿用模型 ID" }
                    }
                    ColumnLayout {
                        Layout.preferredWidth: 170; Layout.maximumWidth: 170; spacing: 7
                        FieldLabel { text: "温度（0–2，可留空）" }
                        EditField { id: temperatureField; objectName: "editTemperature"; Accessible.name: "温度"; inputMethodHints: Qt.ImhFormattedNumbersOnly }
                    }
                }
                FieldLabel { visible: tokenModelChoice.visible; text: "设置输入输出的模型" }
                AppComboBox {
                    id: tokenModelChoice; objectName: "tokenModelChoice"; visible: count > 1
                    Layout.fillWidth: true; Accessible.name: "设置输入输出的模型"
                    model: editor.tokenModelIds
                    onCurrentTextChanged: if (tokenLimits) editor.loadTokenFields()
                }
                ModelTokenFields {
                    id: tokenLimits; material: editor.material
                    onEdited: {
                        const name = tokenModelChoice.currentText || modelField.text.trim()
                        if (name) editor.tokenEdits = Object.assign({}, editor.tokenEdits, { [name]: values() })
                        editor.service.clearStatus()
                    }
                }
                FieldLabel { text: "供应商备注（此配置）" }
                EditField { id: providerLabel; objectName: "editProviderLabel"; Accessible.name: "供应商备注"; placeholderText: "选填，例如团队代理或地区" }
                FieldLabel { text: "思考强度档位" }
                RowLayout {
                    Layout.fillWidth: true; spacing: 8
                    AppComboBox {
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
            x: editor.formX; y: editorActions.y - height - 6; width: parent.width - x - 20
            visible: !editor.showHistory && text.length > 0
            text: editor.service.status; wrapMode: Text.Wrap; textFormat: Text.PlainText
            color: editor.service.success ? Theme.successText : editor.service.busy ? Theme.textSecondary : Theme.dangerText
            font.pixelSize: 12
        }
        Row {
            id: editorActions; visible: !editor.showHistory && !!editor.row
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
