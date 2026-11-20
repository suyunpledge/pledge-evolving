import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Item {
    id: page
    objectName: "connectorsPage"
    required property var material
    readonly property var service: workbench.mcp
    property string selectedId: ""
    property string region: "all"
    property string search: ""
    readonly property bool showAccounts: region !== "china" && !search.trim()
    readonly property var results: service.presets.filter(row => {
        const query = page.search.trim().toLowerCase()
        return (page.region === "all" || page.region === row.region
                || (page.region === "global" && row.region === "us"))
            && (!query || [row.name, row.detail, row.category, row.id].some(value => value.toLowerCase().includes(query)))
    })
    readonly property string selectedSetup: selected?.setup || ""
    readonly property var groups: service.categories.map(category => ({
        title: category, rows: page.results.filter(row => row.category === category)
    })).filter(group => group.rows.length)
    readonly property var selected: service.rows.concat(service.families, service.presets).find(row => row.id === selectedId) || null
    function connectSelected() {
        const row = page.selected
        if (row.loginMode === "manual-token") {
            browserForm.begin(row.loginMode)
            page.service.openCredentialPage(row.id)
        } else if ((row.loginMode === "app-oauth" || row.loginMode === "google-cloud") && !row.configured) {
            browserForm.begin(row.loginMode)
        } else {
            page.service.connectOAuth(row.id)
        }
    }
    function showPairing(family, identifier) {
        const popup = family === "google" ? googlePairing : microsoftPairing
        popup.show(identifier)
    }
    function icon(name) {
        if (name?.startsWith("@")) return Qt.resolvedUrl("../../assets/brands-src/" + name.slice(1) + ".png")
        return name ? Qt.resolvedUrl("../../assets/connectors/" + (name.includes(".") ? name : name + ".svg")) : ""
    }

    ColumnLayout {
        anchors.fill: parent; spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Image {
                source: page.icon(page.selected?.icon); visible: status === Image.Ready
                Layout.preferredWidth: 22; Layout.preferredHeight: 22; fillMode: Image.PreserveAspectFit
            }
            Glyph { name: "plug"; visible: !!page.selected && !page.selected.icon }
            Text {
                Layout.fillWidth: true; text: page.selected ? page.selected.name : "MCP 接口"
                color: "#eef2f6"; font.pixelSize: 21; font.weight: Font.DemiBold; elide: Text.ElideRight
            }
            GlassButton { material: page.material; glyph: "chevron-left"; text: "返回列表"; visible: !!page.selected; onClicked: page.selectedId = "" }
            GlassButton {
                material: page.material; glyph: "plus"; text: "自定义 MCP"; selected: true
                visible: !page.selected; enabled: page.service.loaded && !page.service.busy
                onClicked: editForm.edit(null)
            }
        }
        Text {
            Layout.fillWidth: true; text: page.service.error || page.service.accountsError; visible: !!text
            color: "#e2ad9f"; font.pixelSize: 12; wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.fillWidth: true; visible: !page.selected && page.service.loaded
            Repeater {
                model: [{key: "all", label: "全部"}, {key: "china", label: "中国"}, {key: "us", label: "美国"}, {key: "global", label: "国际"}]
                GlassButton {
                    required property var modelData
                    objectName: "mcpRegion_" + modelData.key
                    material: page.material; text: modelData.label; selected: page.region === modelData.key
                    onClicked: { page.region = modelData.key; servers.positionViewAtBeginning() }
                }
            }
            TextField {
                id: searchField; objectName: "mcpSearch"
                Layout.fillWidth: true; Layout.preferredHeight: 36
                placeholderText: "搜索服务或功能"; Accessible.name: placeholderText
                leftPadding: 11; color: "#e6ebf0"; placeholderTextColor: "#9facba"
                onTextChanged: { page.search = text; servers.positionViewAtBeginning() }
                background: GlassSurface { material: page.material; radius: 10; emphasis: searchField.activeFocus ? 0.45 : 0 }
            }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            ListView {
                id: servers; objectName: "mcpServerRows"
                anchors.fill: parent; visible: !page.selected && page.service.loaded
                model: page.region === "all" ? page.service.rows.filter(row => {
                    const query = page.search.trim().toLowerCase()
                    return row.name.toLowerCase().includes(query) || row.url.toLowerCase().includes(query)
                }) : []
                clip: true; spacing: 8; reuseItems: true
                ScrollBar.vertical: ScrollBar {
                    width: 4; policy: servers.contentHeight > servers.height ? ScrollBar.AlwaysOn : ScrollBar.AlwaysOff
                }
                delegate: GlassRow {
                    required property var modelData
                    material: page.material; width: ListView.view.width
                    text: modelData.name; detail: modelData.url; status: modelData.status; glyph: "plug"
                    statusColor: modelData.status === "连接失败" ? "#e2ad9f" : modelData.tools.length ? "#9cceb5" : "#9eacbd"
                    onClicked: page.selectedId = modelData.id
                }
                footer: Column {
                    width: servers.width; spacing: 8
                    Item { width: 1; height: 4 }
                    Text { visible: page.showAccounts; text: "内置连接器"; color: "#aab5c4"; font.pixelSize: 13; topPadding: 14; bottomPadding: 4 }
                    Repeater {
                        model: page.showAccounts ? [{family:"google", name:"Google Workspace", detail:"Gmail、Drive、文档、日历等"}, {family:"microsoft", name:"Microsoft 365", detail:"Outlook、OneDrive、Teams 等"}] : []
                        GlassRow {
                            required property var modelData
                            material: page.material; width: parent.width
                            text: modelData.name; detail: modelData.detail; status: "配对"
                            iconUrl: page.icon(modelData.family)
                            enabled: !page.service.busy && !page.service.accountsError
                            onClicked: page.showPairing(modelData.family, "")
                        }
                    }
                    Repeater {
                        model: page.showAccounts ? page.service.families.filter(row => row.paired) : []
                        GlassRow {
                            required property var modelData
                            material: page.material; width: parent.width
                            text: modelData.name; detail: modelData.detail; glyph: modelData.glyph
                            iconUrl: page.icon(modelData.icon)
                            status: modelData.status || "已配对"; statusColor: "#9cceb5"
                            onClicked: page.selectedId = modelData.id
                        }
                    }
                    GlassRow {
                        material: page.material; width: parent.width
                        visible: page.showAccounts
                        text: "GitHub 账户"; detail: "Keystone 内置 GitHub 工具"; status: "账户配置"
                        iconUrl: page.icon("github")
                        onClicked: workbench.showConnectorAccounts("github")
                    }
                    Repeater {
                        model: page.groups
                        Column {
                            required property var modelData
                            width: parent.width; spacing: 8
                            Text { text: modelData.title; color: "#aab5c4"; font.pixelSize: 13; topPadding: 14; bottomPadding: 4 }
                            Repeater {
                                model: modelData.rows
                                GlassRow {
                                    required property var modelData
                                    material: page.material; width: parent.width
                                    objectName: "mcpRow_" + modelData.id
                                    text: modelData.name; detail: modelData.detail
                                    iconUrl: page.icon(modelData.icon); glyph: "plug"
                                    status: modelData.setup && modelData.setup.endsWith("endpoint") ? "配置地址" : modelData.setup ? "接入指南"
                                            : modelData.status || (modelData.hasCredential || (modelData.key === "obsidian" && modelData.configured)
                                            ? "已配置" : modelData.local ? "本机安装" : modelData.configured ? "直接连接" : "待连接")
                                    statusColor: modelData.status === "连接失败" ? "#e2ad9f" : modelData.tools.length ? "#9cceb5" : "#9eacbd"
                                    onClicked: page.selectedId = modelData.id
                                }
                            }
                        }
                    }
                    Text {
                        visible: !page.groups.length && !servers.count && !page.showAccounts
                        text: "没有匹配的 MCP 服务"; color: "#aab5c4"; topPadding: 18
                    }
                }
            }
            ColumnLayout {
                anchors.fill: parent; visible: !!page.selected; spacing: 10
                Text {
                    Layout.fillWidth: true
                    text: page.selected?.key === "obsidian" && page.service.vault
                          ? page.service.vault : page.selected ? page.selected.url || page.selected.detail : ""
                    color: "#a9b7c6"; font.pixelSize: 13
                    wrapMode: Text.Wrap; elide: Text.ElideNone
                }
                Flow {
                    Layout.fillWidth: true; spacing: 6
                    GlassButton {
                        material: page.material; glyph: "refresh-cw"
                        text: page.selected?.local && !page.selected.tools.length ? "连接" : page.selected?.family || page.selected?.key ? "获取工具" : "测试连接"
                        visible: !!page.selected && !page.selected.setup && (!(page.selected.oauth || page.selected.local) || page.selected.configured)
                        enabled: !page.service.busy && (!page.selected?.family || page.selected.paired)
                                 && (!page.selected?.key || page.selected.configured)
                        onClicked: page.service.test(page.selectedId)
                    }
                    GlassButton { material: page.material; glyph: "square-pen"; text: "编辑"; visible: !!page.selected && !page.selected.family && !page.selected.key && !page.selected.setup; enabled: !page.service.busy; onClicked: editForm.edit(page.selected) }
                    GlassButton { material: page.material; glyph: "x"; text: "移除"; visible: !!page.selected && !page.selected.family && !page.selected.key && !page.selected.setup; enabled: !page.service.busy; onClicked: removePrompt.open() }
                    GlassButton { material: page.material; glyph: "key-round"; text: "重新配对"; visible: !!page.selected?.family; enabled: !page.service.busy; onClicked: page.showPairing(page.selected.family, page.selected.id) }
                    GlassButton { material: page.material; glyph: "x"; text: "断开 Google 账户"; visible: page.selected?.family === "google" && page.selected.paired; enabled: !page.service.busy; onClicked: disconnectPrompt.open() }
                    GlassButton {
                        material: page.material; glyph: "key-round"; visible: !!page.selected?.oauth
                        text: page.selected?.configured ? "重新授权" : "连接"
                        enabled: !page.service.busy; onClicked: page.connectSelected()
                    }
                    GlassButton {
                        material: page.material; text: "更换飞书应用"; glyph: "square-pen"
                        visible: page.selected?.loginMode === "app-oauth" && page.selected.configured
                        enabled: !page.service.busy; onClicked: browserForm.begin("app-oauth")
                    }
                    GlassButton {
                        material: page.material; text: "更换 Google Cloud 项目"; glyph: "square-pen"
                        visible: page.selected?.loginMode === "google-cloud" && page.selected.configured
                        enabled: !page.service.busy; onClicked: browserForm.begin("google-cloud")
                    }
                    GlassButton {
                        material: page.material; glyph: "folder-open"; visible: page.selected?.key === "obsidian"
                        text: page.service.vault ? "更换仓库" : "连接 Obsidian"
                        enabled: !page.service.busy; onClicked: vaultPicker.open()
                    }
                    GlassButton {
                        material: page.material; glyph: "key-round"; visible: !!page.selected?.credential && (!page.selected?.oauth || page.selected?.dualAuth)
                        text: page.selected?.dualAuth ? "使用访问密钥"
                            : page.selected?.hasCredential ? "更换凭据" : page.selected?.optionalCredential ? "添加 API Key" : "连接"
                        enabled: !page.service.busy; onClicked: presetForm.open()
                    }
                    GlassButton {
                        material: page.material; glyph: "x"; text: page.selected?.oauth ? "断开连接" : "移除凭据"
                        visible: !!page.selected?.hasCredential
                        enabled: !page.service.busy; onClicked: removePrompt.open()
                    }
                    GlassButton {
                        material: page.material; glyph: "plus"; text: "配置 MCP 地址"
                        visible: page.selectedSetup.endsWith("endpoint")
                        enabled: !page.service.busy
                        onClicked: editForm.edit(page.selected)
                    }
                    GlassButton {
                        material: page.material; glyph: "external-link"; text: "连接文档"
                        visible: !!page.selected?.docs
                        onClicked: Qt.openUrlExternally(page.selected.docs)
                    }
                }
                Text {
                    Layout.fillWidth: true; color: "#99a9bb"; font.pixelSize: 12
                    text: page.service.authorizing && page.selected?.oauth
                          ? (page.service.login.url ? "请在浏览器完成授权。" : page.service.login.stage || "正在准备浏览器授权…")
                          : page.selected ? page.selected.status || (page.selected.paired ? "已配对" : "") : ""
                }
                RowLayout {
                    visible: page.service.authorizing
                    GlassButton { material: page.material; text: "打开授权页"; glyph: "external-link"; visible: !!page.service.login.url; onClicked: page.service.openAuthorization() }
                    GlassButton { material: page.material; text: "取消授权"; onClicked: page.service.cancelPairing() }
                }
                Text {
                    Layout.fillWidth: true
                    visible: !!page.selected?.tools.length && !page.selected?.family
                    text: page.selected?.key === "tencent-meeting" ? "查询工具可在 Work 对话使用，会议变更在本页确认。"
                        : ["google-maps", "google-maps-code"].includes(page.selected?.key)
                          ? "地图工具结果在此查看，来源随结果显示。"
                          : "本次已获取的工具可在 Work 对话的手动批准或完全访问模式使用。"
                    color: "#9eacbd"; font.pixelSize: 12; wrapMode: Text.Wrap
                }
                ListView {
                    Layout.fillWidth: true; Layout.fillHeight: true
                    model: page.selected ? page.selected.tools : []
                    clip: true; spacing: 8; reuseItems: true
                    ScrollBar.vertical: ScrollBar { width: 4 }
                    delegate: GlassRow {
                        required property var modelData
                        material: page.material; width: ListView.view.width
                        text: modelData.name; detail: modelData.description; glyph: "puzzle"
                        iconUrl: page.icon(page.selected?.icon)
                        onClicked: toolInfo.inspect(modelData)
                    }
                }
            }
        }
        RowLayout {
            visible: !page.service.loaded; Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: page.service.busy ? "正在加载 MCP 配置…" : "MCP 配置尚未加载"; color: "#9aa9bc" }
            GlassButton { material: page.material; text: "重试"; visible: !page.service.busy; onClicked: page.service.load() }
        }
    }

    FolderDialog {
        id: vaultPicker
        title: "选择 Obsidian 仓库"
        onAccepted: page.service.connectVault(selectedFolder)
    }

    ConnectorPairing {
        id: googlePairing
        family: "google"; material: page.material; service: page.service
    }
    ConnectorPairing {
        id: microsoftPairing
        family: "microsoft"
        material: page.material; service: page.service
        onMicrosoftSettingsRequested: {
            microsoftPairing.close()
            workbench.showConnectorAccounts("microsoft")
        }
    }

    Popup {
        id: browserForm; objectName: "mcpBrowserForm"
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(530, parent.width - 40); padding: 20; modal: true; focus: true
        property string mode: ""
        property bool submitting: false
        readonly property bool feishu: mode === "app-oauth"
        readonly property bool cloud: mode === "google-cloud"
        readonly property bool appCredentials: feishu || cloud
        property string failure: ""
        function begin(value) {
            mode = value
            failure = ""
            submitting = false
            open()
        }
        onOpened: appCredentials ? clientInput.forceActiveFocus() : browserSecret.forceActiveFocus()
        onClosed: { clientInput.clear(); scopeOrProject.clear(); browserSecret.clear(); submitting = false }
        closePolicy: submitting ? Popup.NoAutoClose : Popup.CloseOnEscape
        background: GlassSurface { material: page.material; radius: 18; tint: "#ed19232d" }
        Connections {
            target: page.service
            function onChanged() {
                if (!browserForm.submitting || page.service.busy)
                    return
                browserForm.submitting = false
                if (page.service.error)
                    browserForm.failure = page.service.error
                else
                    browserForm.close()
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                text: "连接 " + (page.selected?.name || "")
                color: "#f0f3f6"; font.pixelSize: 19; font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#becbd9"; font.pixelSize: 13
                text: browserForm.feishu
                    ? "为飞书自建应用开通所需的用户权限，并在安全设置中添加下方回调地址。"
                    : browserForm.cloud
                    ? "启用此 Google Cloud API，并填写桌面 OAuth 客户端与计费项目。工具权限由该项目的 IAM 角色决定。"
                    : page.selected?.loginHint || ""
            }
            TextField {
                Layout.fillWidth: true; visible: browserForm.feishu
                readOnly: true; selectByMouse: true; text: page.service.feishuRedirect
                color: "#c9d6e4"; leftPadding: 10; Layout.preferredHeight: 36
                background: GlassSurface { material: page.material; radius: 8 }
            }
            TextField {
                id: clientInput; Layout.fillWidth: true; Layout.preferredHeight: 38
                visible: browserForm.appCredentials
                placeholderText: browserForm.cloud ? "Google 桌面 OAuth Client ID" : "飞书 App ID"
                maximumLength: 245; enabled: !browserForm.submitting
                color: "#e6ebf0"; placeholderTextColor: "#9facba"; leftPadding: 10
                background: GlassSurface { material: page.material; radius: 8 }
            }
            TextField {
                id: browserSecret; Layout.fillWidth: true; Layout.preferredHeight: 38
                placeholderText: browserForm.feishu ? "飞书 App Secret" : browserForm.cloud ? "OAuth Client Secret"
                                 : page.selected?.credential || "授权结果或 access_token"
                maximumLength: 8192; enabled: !browserForm.submitting
                echoMode: TextInput.Password; color: "#e6ebf0"; placeholderTextColor: "#9facba"; leftPadding: 10
                background: GlassSurface { material: page.material; radius: 8 }
            }
            TextField {
                id: scopeOrProject; Layout.fillWidth: true; Layout.preferredHeight: 38
                visible: browserForm.appCredentials
                placeholderText: browserForm.cloud ? "Google Cloud 项目 ID" : "用户权限标识，空格分隔（可选）"
                maximumLength: 4096; enabled: !browserForm.submitting
                color: "#e6ebf0"; placeholderTextColor: "#9facba"; leftPadding: 10
                background: GlassSurface { material: page.material; radius: 8 }
            }
            Text {
                Layout.fillWidth: true; visible: !!text; color: browserForm.failure ? "#e2ad9f" : "#becbd9"
                wrapMode: Text.Wrap; text: browserForm.failure || (browserForm.submitting
                    ? (page.service.login.url ? "请在浏览器完成授权。" : page.service.login.stage || "") : "")
            }
            RowLayout {
                Layout.fillWidth: true; spacing: 8
                GlassButton {
                    material: page.material; glyph: "external-link"
                    text: page.service.login.url ? "打开授权页" : browserForm.feishu ? "飞书开放平台"
                          : browserForm.cloud ? "Google Cloud 控制台" : "打开官方登录页"
                    enabled: !browserForm.submitting || !!page.service.login.url
                    onClicked: {
                        if (browserForm.appCredentials && browserForm.submitting)
                            page.service.openAuthorization()
                        else if (browserForm.feishu)
                            Qt.openUrlExternally("https://open.feishu.cn/app")
                        else if (browserForm.cloud)
                            Qt.openUrlExternally("https://console.cloud.google.com/apis/credentials")
                        else
                            page.service.openCredentialPage(page.selectedId)
                    }
                }
                Item { Layout.fillWidth: true }
                GlassButton {
                    material: page.material; text: "取消"
                    onClicked: browserForm.submitting ? page.service.cancelPairing() : browserForm.close()
                }
                GlassButton {
                    material: page.material; selected: true; glyph: "plug"
                    text: browserForm.feishu ? "安装并授权" : browserForm.cloud ? "登录并连接" : "验证并连接"
                    enabled: !page.service.busy && !!browserSecret.text.trim()
                        && (!browserForm.appCredentials || !!clientInput.text.trim())
                        && (!browserForm.cloud || !!scopeOrProject.text.trim())
                    onClicked: {
                        browserForm.failure = ""
                        browserForm.submitting = true
                        if (browserForm.feishu)
                            page.service.connectFeishu(clientInput.text, browserSecret.text, scopeOrProject.text)
                        else if (browserForm.cloud)
                            page.service.connectGoogleCloud(page.selectedId, clientInput.text, browserSecret.text, scopeOrProject.text)
                        else
                            page.service.connectWithCredential(page.selectedId, browserSecret.text)
                    }
                }
            }
        }
    }

    Popup {
        id: presetForm
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(480, parent.width - 40); padding: 18; modal: true; focus: true
        property bool submitting: false
        onOpened: presetToken.forceActiveFocus()
        onClosed: { presetToken.clear(); submitting = false }
        closePolicy: submitting ? Popup.NoAutoClose : Popup.CloseOnEscape
        background: GlassSurface { material: page.material; radius: 18; tint: "#ed19232d" }
        Connections {
            target: page.service
            function onChanged() {
                if (presetForm.submitting && !page.service.busy) {
                    presetForm.submitting = false
                    if (!page.service.error)
                        presetForm.close()
                }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: "连接 " + (page.selected?.name || ""); color: "#f0f3f6"; font.pixelSize: 19; font.weight: Font.DemiBold }
            TextField {
                id: presetToken; Layout.fillWidth: true; Layout.preferredHeight: 40
                placeholderText: page.selected?.credential || ""; leftPadding: 12; maximumLength: 8192
                echoMode: TextInput.Password; color: "#e6ebf0"; placeholderTextColor: "#9facba"
                enabled: !presetForm.submitting
                background: GlassSurface { material: page.material; radius: 10; emphasis: presetToken.activeFocus ? 0.45 : 0 }
            }
            Text { Layout.fillWidth: true; visible: !!text; text: page.service.error; wrapMode: Text.Wrap; color: "#e2ad9f" }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "取消"; enabled: !presetForm.submitting; onClicked: presetForm.close() }
                GlassButton {
                    material: page.material; glyph: "plug"; text: presetForm.submitting ? "正在验证…" : "连接"
                    selected: true; enabled: !page.service.busy && !!presetToken.text.trim()
                    onClicked: {
                        presetForm.submitting = true
                        page.service.savePreset(page.selectedId, presetToken.text)
                    }
                }
            }
        }
    }

    Popup {
        id: disconnectPrompt
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(420, parent.width - 40); padding: 18; modal: true; focus: true
        background: GlassSurface { material: page.material; radius: 18; tint: "#ed19232d" }
        contentItem: ColumnLayout {
            spacing: 12
            Text { Layout.fillWidth: true; text: "断开此账户在 Keystone 中的所有 Google 连接器？"; color: "#e6ebf0"; wrapMode: Text.Wrap }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "取消"; onClicked: disconnectPrompt.close() }
                GlassButton { material: page.material; text: "确认断开"; onClicked: {
                    page.service.disconnectGoogle()
                    page.selectedId = ""
                    disconnectPrompt.close()
                } }
            }
        }
    }

    Popup {
        id: editForm; objectName: "mcpEditor"
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(580, parent.width - 40); padding: 22; modal: true; focus: true
        property string editingId: ""
        property string auth: ""
        property bool saving: false
        function edit(row) {
            editingId = row && !row.setup ? row.id : ""
            nameField.text = row ? row.name : ""
            auth = row?.setup === "query-endpoint" ? "dingtalk" : row?.auth || ""
            urlField.text = row ? row.url : ""
            tokenField.clear()
            saving = false
            open()
        }
        onOpened: nameField.forceActiveFocus()
        onClosed: { tokenField.clear(); saving = false }
        closePolicy: saving ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { material: page.material; radius: 22; tint: "#ed19232d" }
        Connections {
            target: page.service
            function onServerSaved(identifier) {
                if (!editForm.saving)
                    return
                page.selectedId = identifier
                editForm.close()
                Qt.callLater(() => page.service.test(identifier))
            }
            function onChanged() {
                if (editForm.saving && !page.service.busy) {
                    editForm.saving = false
                    if (!page.service.error) editForm.close()
                }
            }
        }
        contentItem: ColumnLayout {
            spacing: 12
            Text { text: editForm.editingId ? "编辑 MCP 接口" : "自定义 MCP 接口"; color: "#f0f3f6"; font.pixelSize: 19; font.weight: Font.DemiBold }
            TextField {
                id: nameField; objectName: "mcpName"; Layout.fillWidth: true; Layout.preferredHeight: 40
                placeholderText: "接口名称"; maximumLength: 64; leftPadding: 12; color: "#e6ebf0"; placeholderTextColor: "#9facba"
                background: GlassSurface { material: page.material; radius: 10; emphasis: nameField.activeFocus ? 0.45 : 0 }
            }
            TextField {
                id: urlField; objectName: "mcpUrl"; Layout.fillWidth: true; Layout.preferredHeight: 40
                placeholderText: editForm.auth === "dingtalk" ? "钉钉地址（将 key 填入下一栏）" : "Streamable HTTP 地址"; leftPadding: 12; maximumLength: 2048
                color: "#e6ebf0"; placeholderTextColor: "#9facba"; selectByMouse: true
                inputMethodHints: Qt.ImhUrlCharactersOnly
                background: GlassSurface { material: page.material; radius: 10; emphasis: urlField.activeFocus ? 0.45 : 0 }
            }
            TextField {
                id: tokenField; Layout.fillWidth: true; Layout.preferredHeight: 40
                placeholderText: editForm.auth === "dingtalk" ? "钉钉 MCP Key" : "Bearer 令牌（可选）"; leftPadding: 12; maximumLength: 8192
                echoMode: TextInput.Password; color: "#e6ebf0"; placeholderTextColor: "#9facba"
                background: GlassSurface { material: page.material; radius: 10; emphasis: tokenField.activeFocus ? 0.45 : 0 }
            }
            Text { Layout.fillWidth: true; visible: !!text; text: page.service.error; wrapMode: Text.Wrap; color: "#e2ad9f"; font.pixelSize: 12 }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "取消"; quiet: true; enabled: !editForm.saving; onClicked: editForm.close() }
                GlassButton {
                    material: page.material; glyph: "save"; text: editForm.saving ? "正在保存…" : "保存并测试"; selected: true
                    enabled: !page.service.busy && !!nameField.text.trim() && !!urlField.text.trim()
                    onClicked: {
                        editForm.saving = true
                        page.service.save(editForm.editingId, nameField.text, urlField.text, tokenField.text, editForm.auth)
                    }
                }
            }
        }
    }
    Popup {
        id: removePrompt
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(400, parent.width - 40); padding: 18; modal: true; focus: true
        background: GlassSurface { material: page.material; radius: 18; tint: "#ed19232d" }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true; color: "#e6ebf0"; wrapMode: Text.Wrap
                text: page.selected?.key ? "移除「" + page.selected.name + "」的本地凭据？"
                    : "移除「" + (page.selected?.name || "") + "」及其本地令牌？"
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "取消"; onClicked: removePrompt.close() }
                GlassButton { material: page.material; text: "确认移除"; onClicked: {
                    if (page.selected?.key)
                        page.service.clearPreset(page.selectedId)
                    else
                        page.service.remove(page.selectedId)
                    removePrompt.close()
                } }
            }
        }
    }
    Popup {
        id: toolInfo
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(680, parent.width - 40); height: Math.min(550, parent.height - 40)
        modal: true; focus: true; padding: 20
        property var tool: ({})
        property bool invoked: false
        function inspect(value) {
            tool = value
            invoked = false
            toolArgs.text = "{}"
            if (page.selected?.key === "wecom")
                page.service.inspectWeCom(value.name)
            open()
        }
        background: GlassSurface { material: page.material; radius: 22; tint: "#ed19232d" }
        contentItem: ColumnLayout {
            RowLayout {
                Layout.fillWidth: true
                Text { Layout.fillWidth: true; text: toolInfo.tool.name || ""; color: "#e9eef4"; font.pixelSize: 17; elide: Text.ElideRight }
                GlassButton { material: page.material; glyph: "x"; tip: "关闭工具详情"; onClicked: toolInfo.close() }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true; contentWidth: availableWidth
                TextArea {
                    readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; background: null
                    color: "#cbd6e2"; font.pixelSize: 13
                    text: toolInfo.invoked ? page.service.result || page.service.error || "正在调用…"
                        : (toolInfo.tool.description || "") + "\n\n" + (page.selected?.key === "wecom"
                          ? page.service.toolSchema || page.service.error || "正在读取企业微信参数…"
                          : JSON.stringify(toolInfo.tool.schema || {}, null, 2))
                }
            }
            ScrollView {
                Layout.fillWidth: true; Layout.preferredHeight: 96; clip: true
                background: GlassSurface { material: page.material; radius: 10 }
                TextArea {
                    id: toolArgs; Accessible.name: "工具 JSON 参数"
                    text: "{}"; wrapMode: TextEdit.Wrap; selectByMouse: true; background: null
                    color: "#e3eaf1"; font.family: "Consolas"; font.pixelSize: 13; padding: 10
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "参数说明"; quiet: true; visible: toolInfo.invoked; onClicked: toolInfo.invoked = false }
                GlassButton {
                    material: page.material; glyph: "play"; text: "调用工具"; selected: true
                    enabled: !page.service.busy && !!toolInfo.tool.name && !!page.selected
                    onClicked: invokePrompt.open()
                }
            }
        }
    }
    Popup {
        id: invokePrompt
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(440, parent.width - 40); padding: 20; modal: true; focus: true
        background: GlassSurface { material: page.material; radius: 18; tint: "#ed19232d" }
        contentItem: ColumnLayout {
            spacing: 12
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#e6ebf0"
                text: "将参数发送给「" + (page.selected ? page.selected.name : "") + "」并调用 " + (toolInfo.tool.name || "") + "？"
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: page.material; text: "取消"; onClicked: invokePrompt.close() }
                GlassButton { material: page.material; text: "确认调用"; onClicked: {
                    toolInfo.invoked = true
                    page.service.invoke(page.selectedId, toolInfo.tool.name, toolArgs.text, workbench.workspace)
                    invokePrompt.close()
                } }
            }
        }
    }
}
