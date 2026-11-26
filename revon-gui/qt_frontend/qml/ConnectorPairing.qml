import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: dialog
    required property string family
    objectName: "connectorPairing_" + family
    required property var material
    required property var service
    signal microsoftSettingsRequested()
    parent: Overlay.overlay
    anchors.centerIn: parent
    width: Math.min(710, parent.width - 30)
    height: Math.min(google ? 676 : 532, parent.height - 30)
    padding: 20
    modal: true
    focus: true
    closePolicy: service.pairing ? Popup.NoAutoClose : Popup.CloseOnEscape
    property var selection: []
    property bool submitted: false
    readonly property bool google: family === "google"
    readonly property string familyName: google ? "Google Workspace" : "Microsoft 365"
    readonly property var choices: service.families.filter(row => row.family === family)
    readonly property var completed: choices.filter(row => row.paired && selection.includes(row.id))

    function show(identifier) {
        selection = identifier ? [identifier] : []
        submitted = false
        clientId.text = service.accounts[family]?.clientId || ""
        credential.text = google ? "" : service.accounts.microsoft?.tenant || "common"
        open()
        options.positionViewAtBeginning()
    }
    function toggle(identifier) {
        selection = selection.includes(identifier) ? selection.filter(id => id !== identifier) : selection.concat(identifier)
    }
    onClosed: if (google) credential.clear()
    background: GlassSurface { material: dialog.material; radius: 22; tint: "#ee19232d" }

    component CredentialField: TextField {
        Layout.fillWidth: true; Layout.preferredHeight: 36
        leftPadding: 10; maximumLength: 512; selectByMouse: true
        Accessible.name: placeholderText
        color: "#e6ebf0"; placeholderTextColor: "#a2aebb"
        background: GlassSurface { material: dialog.material; radius: 10; emphasis: parent.activeFocus ? 0.5 : 0 }
    }

    contentItem: ColumnLayout {
        spacing: 9
        RowLayout {
            Layout.fillWidth: true
            Image {
                source: Qt.resolvedUrl("../../assets/connectors/" + dialog.family + ".svg")
                Layout.preferredWidth: 22; Layout.preferredHeight: 22; fillMode: Image.PreserveAspectFit
            }
            Text {
                text: "配对 " + dialog.familyName; color: "#f0f3f6"; font.pixelSize: 21; font.weight: Font.DemiBold
                Layout.fillWidth: true
            }
            GlassButton { material: dialog.material; glyph: "x"; tip: "关闭"; enabled: !dialog.service.pairing; onClicked: dialog.close() }
        }
        RowLayout {
            Layout.fillWidth: true
            Text {
                text: "已选 " + dialog.selection.length + " / " + dialog.choices.length + " 项"
                color: "#b2bece"; Layout.fillWidth: true
            }
            GlassButton {
                objectName: "pairSelectAll"; material: dialog.material; glyph: "check"
                text: dialog.selection.length === dialog.choices.length ? "取消全选" : "全选"
                enabled: !dialog.service.pairing
                onClicked: dialog.selection = dialog.selection.length === dialog.choices.length
                           ? [] : dialog.choices.map(row => row.id)
            }
        }
        AppListView {
            id: options
            objectName: "pairOptions"
            Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumHeight: 100
            model: dialog.choices; spacing: 6; clip: true
            delegate: GlassRow {
                required property var modelData
                objectName: "pairOption_" + modelData.id
                material: dialog.material; width: ListView.view.rowWidth; height: 42
                text: modelData.name; detail: modelData.detail; glyph: modelData.glyph
                iconUrl: Qt.resolvedUrl("../../assets/connectors/" + modelData.icon + ".svg")
                status: modelData.paired ? "已配对" : ""
                statusColor: "#9cceb5"
                Accessible.checkable: true
                Accessible.checked: checked
                checked: dialog.selection.includes(modelData.id)
                actionGlyph: checked ? "circle-check" : "square"
                enabled: !dialog.service.pairing
                onClicked: dialog.toggle(modelData.id)
            }
        }
        RowLayout {
            Layout.fillWidth: true; visible: dialog.selection.length > 0 && !dialog.service.pairing
            CredentialField {
                id: clientId
                placeholderText: dialog.google ? "Google 桌面 OAuth Client ID" : "Microsoft Entra Client ID"
            }
            CredentialField {
                id: credential
                Layout.fillWidth: dialog.google
                Layout.preferredWidth: dialog.google ? -1 : 152
                echoMode: dialog.google ? TextInput.Password : TextInput.Normal
                placeholderText: dialog.google ? (dialog.service.accounts.google?.hasSecret ? "Client Secret（可沿用）" : "Google Client Secret") : "Tenant（默认 common）"
            }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            visible: !dialog.service.pairing && dialog.selection.length > 0
            color: "#aebaca"; font.pixelSize: 12
            text: dialog.google ? "使用 Google 桌面 OAuth 客户端，需启用所选 Workspace API 与 MCP 服务。"
                                : "使用已启用设备代码流程的 Entra 公共客户端。"
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; visible: !!text && dialog.submitted
            color: "#e2ad9f"; font.pixelSize: 12
            text: dialog.service.pairErrors[dialog.family] || dialog.service.error
        }
        Text {
            Layout.fillWidth: true; font.pixelSize: 12; color: "#9cceb5"
            visible: dialog.submitted && !dialog.service.pairing && dialog.completed.length > 0
            text: "所选服务中已有 " + dialog.completed.length + " 项完成配对"
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            visible: dialog.service.pairing; color: "#d7e4f0"; font.pixelSize: 13
            text: dialog.service.login.family === "google" ? "请在浏览器完成 Google 授权。"
                : dialog.service.login.family === "microsoft" ? "请在浏览器输入 Microsoft 设备代码 " + dialog.service.login.code
                : "正在处理配对…"
        }
        RowLayout {
            Layout.fillWidth: true
            GlassButton {
                material: dialog.material; text: "Microsoft 权限设置"; quiet: true
                visible: !dialog.google && !dialog.service.pairing; enabled: !dialog.service.busy
                onClicked: dialog.microsoftSettingsRequested()
            }
            Item { Layout.fillWidth: true }
            GlassButton {
                material: dialog.material; glyph: "copy"; text: "复制代码"; visible: !!dialog.service.login.code
                onClicked: dialog.service.copyAuthorizationCode()
            }
            GlassButton {
                material: dialog.material; glyph: "external-link"; text: "打开授权"; visible: !!dialog.service.login.url
                onClicked: dialog.service.openAuthorization()
            }
            GlassButton {
                material: dialog.material; text: "取消配对"; visible: dialog.service.pairing
                onClicked: dialog.service.cancelPairing()
            }
            GlassButton {
                objectName: "pairSubmit"; material: dialog.material; glyph: "key-round"; selected: true
                text: dialog.selection.length === dialog.choices.length ? "配对全部" : "配对所选"
                visible: !dialog.service.pairing
                enabled: !dialog.service.busy && dialog.selection.length > 0
                onClicked: {
                    dialog.submitted = true
                    dialog.service.pair(dialog.family, dialog.selection, clientId.text, credential.text)
                    if (dialog.google && dialog.service.pairing)
                        credential.clear()
                }
            }
        }
    }
}
