import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: panel
    objectName: "mediaModelsPopup"
    required property var material
    required property var service
    required property bool english
    property bool fromRequest: false
    property string kind: "image"
    property string profileId: ""
    property bool asDefault: false
    readonly property var available: service.presets.filter(item => item.kind === kind)
    readonly property var stored: service.profiles.filter(item => item.kind === kind)
    readonly property var choices: [{id: "", name: tr("新建配置", "New profile")}].concat(
        stored.map(row => ({id: row.id, name: row.model + (row.selected ? tr(" · 默认", " · Default") : "")})))
    readonly property var currentPreset: available[provider.currentIndex]
    modal: true
    focus: true
    padding: 24
    closePolicy: service.saving ? Popup.NoAutoClose : Popup.CloseOnEscape
    Overlay.modal: Rectangle { color: "#9905080d" }
    background: GlassSurface { material: panel.material; radius: 24; tint: "#ed19232d"; emphasis: 0.6 }

    function tr(zh, en) { return english ? en : zh }
    function showSettings() {
        fromRequest = false
        switchKind("image")
        Qt.callLater(open)
    }
    function switchKind(value) {
        kind = value
        const selected = stored.find(row => row.selected)
        loadProfile(selected ? selected.id : "")
    }
    function loadProfile(identifier, keepNotice = false) {
        if (!keepNotice) service.clearNotice()
        profileId = identifier
        const row = stored.find(entry => entry.id === identifier)
        models.currentIndex = Math.max(0, choices.findIndex(entry => entry.id === identifier))
        asDefault = row ? row.selected : !stored.length
        provider.currentIndex = row ? Math.max(0, available.findIndex(v => v.id === row.protocol)) : 0
        urlField.text = row ? row.url : (currentPreset ? currentPreset.url : "")
        modelField.text = row ? row.model : ""
        keyField.clear()
    }
    function loadProvider() {
        service.clearNotice()
        urlField.text = currentPreset ? currentPreset.url : ""
        modelField.clear()
        keyField.clear()
    }

    onClosed: {
        keyField.clear()
        if (fromRequest && service.requestKind) service.cancelRequest()
        fromRequest = false
    }
    Connections {
        target: panel.service
        function onChanged() {
            const requested = panel.service.requestKind
            if (requested && !panel.fromRequest) {
                panel.fromRequest = true
                panel.switchKind(requested)
                Qt.callLater(panel.open)
            } else if (!requested && panel.fromRequest && panel.visible) {
                panel.close()
            }
        }
        function onSaved(identifier, resumed) {
            if (!resumed && panel.opened) panel.loadProfile(identifier, true)
        }
    }

    component FieldLabel: Text {
        color: "#aebdcd"; font.pixelSize: 12
    }
    component ConfigField: TextField {
        Layout.fillWidth: true; Layout.preferredHeight: 38
        color: "#edf2f8"; placeholderTextColor: "#8192a5"
        leftPadding: 11; rightPadding: 11; selectByMouse: true
        enabled: !panel.service.saving
        background: GlassSurface { material: panel.material; radius: 10; tint: "#57212d3a" }
    }
    component ConfigChoice: AppComboBox {
        Layout.fillWidth: true; Layout.preferredHeight: 38
        textRole: "name"; valueRole: "id"; leftPadding: 11
        enabled: !panel.service.saving
        contentItem: Text { text: parent.displayText; color: "#edf2f8"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
        background: GlassSurface { material: panel.material; radius: 10; tint: "#57212d3a" }
    }

    contentItem: ColumnLayout {
        id: content
        spacing: 8
        Text {
            id: heading
            Layout.fillWidth: true; color: "#f1f5fa"; font.pixelSize: 22; font.weight: Font.DemiBold
            text: panel.fromRequest
                ? (panel.kind === "image" ? panel.tr("配置图片生成模型", "Set up an image model")
                                           : panel.tr("配置视频生成模型", "Set up a video model"))
                : panel.tr("图片与视频模型", "Image and video models")
        }
        RowLayout {
            id: tabs
            Layout.fillWidth: true; visible: !panel.fromRequest; spacing: 8
            GlassButton {
                objectName: "mediaTabImage"
                Layout.fillWidth: true; material: panel.material; glyph: "image"
                text: panel.tr("图片", "Images"); selected: panel.kind === "image"
                enabled: !panel.service.saving
                onClicked: panel.switchKind("image")
            }
            GlassButton {
                objectName: "mediaTabVideo"
                Layout.fillWidth: true; material: panel.material; glyph: "play"
                text: panel.tr("视频", "Video"); selected: panel.kind === "video"
                enabled: !panel.service.saving
                onClicked: panel.switchKind("video")
            }
        }
        Text {
            id: requestHint
            visible: panel.fromRequest
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#a5b5c7"; font.pixelSize: 13
            text: panel.tr("保存后会继续当前生成请求。选择跳过可返回对话。", "Save to continue this request, or skip to return to chat.")
        }
        AppScrollView {
            id: fieldsScroll
            readonly property real otherHeight: heading.implicitHeight + actions.implicitHeight + content.spacing * 2
                + (tabs.visible ? tabs.implicitHeight + content.spacing : 0)
                + (requestHint.visible ? requestHint.implicitHeight + content.spacing : 0)
                + (feedback.visible ? feedback.implicitHeight + content.spacing : 0)
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(fields.implicitHeight, Math.max(100,
                panel.parent.height - 48 - panel.topPadding - panel.bottomPadding - otherHeight))
            contentWidth: availableWidth
            clip: true
            ColumnLayout {
                id: fields
                width: fieldsScroll.availableWidth
                spacing: 8
                FieldLabel { visible: panel.stored.length > 0; text: panel.tr("已保存的模型", "Saved models") }
                ConfigChoice {
                    id: models; objectName: "mediaSavedProfiles"; visible: panel.stored.length > 0
                    Accessible.name: panel.tr("已保存的模型", "Saved models")
                    model: panel.choices
                    onActivated: panel.loadProfile(currentValue)
                }
                FieldLabel { text: panel.tr("接口协议", "API protocol") }
                ConfigChoice {
                    id: provider; objectName: "mediaProtocol"; model: panel.available
                    Accessible.name: panel.tr("接口协议", "API protocol")
                    onActivated: panel.loadProvider()
                }
                FieldLabel { text: panel.tr("接口地址", "API base URL") }
                ConfigField {
                    id: urlField; objectName: "mediaEndpoint"
                    Accessible.name: panel.tr("接口地址", "API base URL")
                    inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText
                }
                FieldLabel { text: panel.tr("模型 ID", "Model ID") }
                ConfigField {
                    id: modelField; objectName: "mediaModelId"
                    Accessible.name: panel.tr("模型 ID", "Model ID")
                    placeholderText: panel.currentPreset ? panel.currentPreset.example : ""
                    inputMethodHints: Qt.ImhNoPredictiveText
                }
                FieldLabel { text: panel.tr("API 密钥", "API key") }
                ConfigField {
                    id: keyField; objectName: "mediaKey"; echoMode: TextInput.Password
                    Accessible.name: panel.tr("API 密钥", "API key")
                    inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                    placeholderText: panel.profileId ? panel.tr("同一接口可沿用已保存的密钥", "Keep the saved key for this endpoint")
                                                     : panel.tr("填写厂商 API 密钥", "Enter the provider API key")
                }
            }
        }
        Text {
            id: feedback
            objectName: "mediaSetupNotice"
            Layout.fillWidth: true
            text: panel.service.saving ? panel.tr("正在保存…", "Saving…") : panel.service.notice
            visible: text.length > 0
            wrapMode: Text.Wrap; textFormat: Text.PlainText; color: "#b7c7da"; font.pixelSize: 12
            maximumLineCount: 3; elide: Text.ElideRight
            ToolTip.visible: feedbackHover.hovered && truncated
            ToolTip.text: text
            HoverHandler { id: feedbackHover }
        }
        RowLayout {
            id: actions
            Layout.fillWidth: true; spacing: 8
            GlassButton {
                visible: !panel.fromRequest; material: panel.material; selected: panel.asDefault
                glyph: panel.asDefault ? "check" : ""
                enabled: !panel.service.saving && !panel.stored.some(row => row.id === panel.profileId && row.selected)
                text: panel.asDefault ? panel.tr("默认模型", "Default model") : panel.tr("设为默认", "Set as default")
                onClicked: panel.asDefault = !panel.asDefault
            }
            Item { Layout.fillWidth: true }
            GlassButton {
                objectName: "mediaSkip"
                material: panel.material; quiet: true; enabled: !panel.service.saving
                text: panel.fromRequest ? panel.tr("跳过本次", "Skip") : panel.tr("关闭", "Close")
                onClicked: panel.close()
            }
            GlassButton {
                objectName: "mediaSave"
                material: panel.material; selected: true
                enabled: !panel.service.saving && !!panel.currentPreset && !!urlField.text.trim() && !!modelField.text.trim()
                text: panel.fromRequest ? panel.tr("保存并生成", "Save and generate") : panel.tr("保存配置", "Save profile")
                onClicked: panel.service.saveProfile(panel.profileId, panel.currentPreset.id, urlField.text,
                                                    modelField.text, keyField.text, panel.asDefault)
            }
        }
    }
}
