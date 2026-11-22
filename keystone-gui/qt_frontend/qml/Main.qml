import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "keystoneQuickWindow"
    width: 1280; height: 860
    minimumWidth: workbench.browserVisible ? 1080 : 820; minimumHeight: 620
    visible: typeof smokeMode === "undefined" || !smokeMode
    title: controller.isDemo ? "Keystone · Integrated Workbench Preview" : "Keystone"
    color: Theme.bgBase
    font: uiFont
    readonly property real sideWidth: width < 1000 ? 220 : 252
    readonly property real browserPanelWidth: workbench.browserVisible ? Math.min(workbench.browserWidth, Math.max(300, width - sideWidth - 450)) : 0
    readonly property real mainRight: width - browserPanelWidth
    readonly property var setup: workbench.onboarding
    readonly property bool english: setup.language === "en"
    function label(zh, en) { return english ? en : zh }
    readonly property bool chatVisible: workbench.page === "chat"
    readonly property bool conversationOnly: controller.conversationMode === "chat"
    readonly property real columnWidth: Math.min(800, mainRight - sideWidth - 48)
    readonly property real columnX: sideWidth + (mainRight - sideWidth - columnWidth) / 2
    readonly property var editor: editorScroll.editor
    readonly property bool codingVisible: workbench.page === "development"
    property bool agentPaneOpen: true
    readonly property real codingWidth: agentPaneOpen ? Math.min(430, Math.max(300, mainRight * 0.30)) : 0
    signal quickFileRequested()
    onChatVisibleChanged: {
        if (chatVisible) Qt.callLater(focusWorkComposer)
    }
    function focusWorkComposer() {
        if (chatVisible && !onboarding.opened && !localToolSetup.opened && !mediaModels.opened && !toolCatalog.opened && !modelEditor.opened) {
            editor.forceActiveFocus()
            editorScroll.refreshInputMethod()
        }
    }
    property bool followTail: true
    property bool exitConfirmed: false
    onClosing: event => {
        if (setup.busy) {
            event.accepted = false
            return
        }
        if (!exitConfirmed && (workbench.hasUnsaved || workbench.busy || workbench.terminalRunning || controller.busy || workbench.mcp.saving || workbench.mediaModels.saving)) {
            event.accepted = false
            exitPopup.open()
        }
    }
    property string sessionQuery: ""
    property var material: QtObject {
        property var scene: window.contentItem
        property var raw: sceneTexture
        property var blurred: blurTexture
        property bool hardware: gpuEnabled
        property bool solid: controller.solid
        property real strength: controller.material
        property bool reduced: controller.reduceMotion
        property real epoch: window.width + window.height + history.contentY + composer.y + composer.height + options.height + modelMenu.height + modelList.contentY + modelButton.width + effortButton.width
    }

    readonly property var wallpaper: workbench.wallpaper
    readonly property bool hasWallpaper: window.wallpaper.kind !== ""
    readonly property string ambientMode: window.codingVisible ? "code" : (window.conversationOnly ? "chat" : "work")
    onAmbientModeChanged: Qt.callLater(refreshBackdrop)
    // Keep the static ambient capture separate from changing page content.
    Item {
        id: ambient
        anchors.fill: parent
        Rectangle { anchors.fill: parent; color: Theme.bgBase }
        ShaderEffect {
            id: auroraShader
            anchors.fill: parent
            visible: gpuEnabled && !stillWallpaper.loaded && !motionWallpaper.active
            property vector2d canvasSize: Qt.vector2d(width, height)
            property real modeAccent: window.codingVisible ? 1.0 : 0.0
            fragmentShader: "../shaders/ambient.frag.qsb"
        }
        Rectangle { anchors.fill: parent; visible: !gpuEnabled && !stillWallpaper.loaded && !motionWallpaper.active; color: Theme.bgFallback }
        DreamyBackdrop {
            id: dreamyBackdrop
            anchors.fill: parent
            visible: !window.hasWallpaper && !stillWallpaper.loaded && !motionWallpaper.active
            sideInset: window.sideWidth
            mode: window.ambientMode
            onPainted: Qt.callLater(window.refreshBackdrop)
        }
        Image {
            id: stillWallpaper; objectName: "stillWallpaper"
            readonly property bool loaded: status === Image.Ready
            anchors.fill: parent
            source: window.wallpaper.kind === "image" ? window.wallpaper.source : ""
            asynchronous: true; cache: false; autoTransform: true
            fillMode: Image.PreserveAspectCrop; clip: true
            onStatusChanged: {
                if (status === Image.Error) window.wallpaper.reportFailure(window.wallpaper.currentId)
                Qt.callLater(window.refreshBackdrop)
            }
        }
        Rectangle {
            anchors.fill: parent; color: "#080c12"
            visible: stillWallpaper.loaded
            opacity: window.wallpaper.dim
            onOpacityChanged: Qt.callLater(window.refreshBackdrop)
        }
    }
    Loader {
        id: motionWallpaper
        anchors.fill: parent
        active: window.wallpaper.kind === "gif" || window.wallpaper.kind === "video"
        sourceComponent: MotionWallpaper {
            wallpaper: window.wallpaper
            // Keep playing beside other apps; stop decoding once nobody can see it.
            running: window.visible && window.visibility !== Window.Minimized
                     && window.visibility !== Window.Hidden && !controller.reduceMotion
        }
    }
    Rectangle {
        width: window.sideWidth; height: parent.height
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#960a0e16" }
            GradientStop { position: 0.5; color: "#780f1726" }
            GradientStop { position: 1.0; color: "#920a0e16" }
        }
    }
    Rectangle {
        x: window.sideWidth; width: 1; height: parent.height
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#0cffffff" }
            GradientStop { position: 0.35; color: "#26ffffff" }
            GradientStop { position: 0.7; color: "#1cffffff" }
            GradientStop { position: 1.0; color: "#0affffff" }
        }
    }
    Connections {
        target: window.wallpaper
        function onChanged() { Qt.callLater(window.refreshBackdrop) }
    }
    Item {
        id: workBackdrop
        anchors.fill: parent
        visible: window.chatVisible
        ListView {
            id: messages
            visible: window.chatVisible
            objectName: "conversationList"
            x: window.columnX; y: 90
            width: window.columnWidth; height: window.height - y - 10
            clip: true
            spacing: 26
            topMargin: 18
            // positionViewAtEnd includes the footer, but not the flickable's bottomMargin.
            footer: Item { width: messages.width; height: composer.height + 76 }
            model: controller.messages
            boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { width: 5; policy: ScrollBar.AsNeeded }
            onMovementStarted: window.followTail = false
            onMovementEnded: if (atYEnd) window.followTail = true
            onContentHeightChanged: if (window.followTail) Qt.callLater(positionViewAtEnd)
            delegate: Item {
                id: message
                required property string roleName
                required property string textContent
                required property string state
                required property string changeKey
                required property var changeInfo
                required property int historyIndex
                required property var toolReceipts
                required property var steps
                required property var todos
                required property bool continuable
                width: messages.width
                height: messageBody.height + (roleName === "user" ? 24 : 0)
                    + (messageActions.visible ? messageActions.height + 4 : 0)
                Rectangle {
                    visible: message.roleName === "user"
                    anchors.right: parent.right
                    width: Math.min(parent.width - 32, messageText.contentWidth + 32)
                    height: messageBody.height + 24
                    radius: 16
                    color: Theme.userBubble
                    border.color: Theme.userBubbleBorder
                    border.width: 1
                }
                Column {
                    id: messageBody
                    width: parent.width - (message.roleName === "user" ? 32 : 0)
                    x: message.roleName === "user" ? 16 : 0
                    y: message.roleName === "user" ? 12 : 0
                    spacing: 12
                    Row {
                        visible: message.roleName !== "user"
                        spacing: 9
                        Image { width: 22; height: 22; source: brandUrl; smooth: true }
                        Text { text: "Keystone"; color: "#dfe4eb"; font.weight: Font.DemiBold; font.pixelSize: 13; anchors.verticalCenter: parent.verticalCenter }
                    }
                    TodoList { width: parent.width; items: message.todos || []; running: message.state === "streaming" }
                    ToolTimeline {
                        width: parent.width
                        steps: message.steps || []
                        running: message.state === "streaming"
                        visible: steps.length > 0
                    }
                    TextEdit {
                        id: messageText
                        objectName: "messageBodyText"
                        visible: text.length > 0
                        width: parent.width
                        height: Math.max(20, contentHeight)
                        text: message.textContent
                        color: "#dce1e7"
                        font.pixelSize: 15
                        textFormat: message.roleName === "user" ? TextEdit.PlainText : TextEdit.MarkdownText
                        horizontalAlignment: message.roleName === "user" ? TextEdit.AlignRight : TextEdit.AlignLeft
                        wrapMode: TextEdit.Wrap
                        readOnly: true
                        selectByMouse: true
                        selectionColor: "#526477"
                        selectedTextColor: "white"
                        onLinkActivated: link => controller.openLink(link)
                        function polishTypography() {
                            if (typeof controller !== "undefined" && message.roleName !== "user") controller.formatMessage(textDocument)
                        }
                        onTextChanged: Qt.callLater(polishTypography)
                        Component.onCompleted: Qt.callLater(polishTypography)
                    }
                    ToolReceipts {
                        width: parent.width
                        visible: receipts.length > 0
                        material: window.material
                        receipts: message.toolReceipts || []
                    }
                    ChangeCard {
                        objectName: "replyChangeCard"
                        width: parent.width
                        visible: message.changeKey !== "" && message.changeInfo.count > 0 && message.state !== "streaming"
                        height: visible ? implicitHeight : 0
                        material: window.material
                        info: message.changeInfo || ({files: [], count: 0, added: 0, removed: 0})
                        onReviewRequested: controller.changeReview.open(message.changeKey)
                        onRevertRequested: { controller.changeReview.open(message.changeKey); changeReview.confirmUndo = true }
                    }
                    Row {
                        visible: message.roleName !== "user" && message.state !== "streaming"
                        spacing: 14
                        Row {
                            spacing: 5
                            Glyph { name: "copy"; ink: copyHover.hovered ? Theme.textPrimary : Theme.textMuted; width: 13; height: 13; anchors.verticalCenter: parent.verticalCenter }
                            Text { text: "复制"; color: copyHover.hovered ? Theme.textPrimary : Theme.textMuted; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                            HoverHandler { id: copyHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: controller.copyText(message.textContent) }
                        }
                        Text { visible: message.state === "stopped" || message.state === "error"; text: message.state === "stopped" ? "已停止" : "回复未完成"; color: Theme.warning; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                        Row {
                            visible: message.state === "error" && controller.canRetry
                            spacing: 5
                            Glyph { name: "refresh-cw"; ink: retryHover.hovered ? Theme.accentInk : Theme.accent; width: 13; height: 13; anchors.verticalCenter: parent.verticalCenter }
                            Text { text: "重试"; color: retryHover.hovered ? Theme.accentInk : Theme.accent; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                            HoverHandler { id: retryHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: controller.retry() }
                        }
                        Text {
                            objectName: "continueWork"
                            visible: message.continuable && !controller.busy
                            text: "继续"; color: continueHover.hovered ? "#e3e7eb" : "#c8d7e5"; font.pixelSize: 12
                            HoverHandler { id: continueHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: controller.continueWork() }
                        }
                    }
                }
                Row {
                    id: messageActions
                    visible: message.roleName === "user" && message.historyIndex >= 0
                    enabled: !controller.busy
                    anchors.right: parent.right
                    y: messageBody.height + 28
                    spacing: 3
                    GlassButton {
                        objectName: "editSentMessage"
                        material: window.material; quiet: true; text: "编辑"; glyph: "square-pen"
                        implicitHeight: 30
                        onClicked: reviseMessage.begin(message.historyIndex, message.textContent, false)
                    }
                    GlassButton {
                        objectName: "withdrawSentMessage"
                        material: window.material; quiet: true; text: "撤回"; glyph: "undo-2"
                        implicitHeight: 30
                        onClicked: reviseMessage.begin(message.historyIndex, message.textContent, true)
                    }
                }
            }
        }
        Rectangle {
            visible: !window.hasWallpaper
            x: window.sideWidth + 1; y: 0
            width: window.mainRight - window.sideWidth - 1; height: 74
            gradient: Gradient {
                GradientStop { position: 0.0; color: "#48090d14" }
                GradientStop { position: 1.0; color: "#00090d14" }
            }
        }
        Column {
            id: welcome
            objectName: "welcome"
            visible: window.chatVisible && messages.count === 0
            x: window.columnX + 16
            // 欢迎区始终落在输入框（及其上方的错误提示）之上，不与之重叠。
            y: Math.max(96, Math.min(window.height * 0.24, (errorBanner.visible ? errorBanner.y : composer.y) - height - 28))
            width: window.columnWidth - 32
            spacing: 16
            Item {
                width: 48; height: 48
                Canvas {
                    id: brandHalo
                    anchors.centerIn: parent; width: 112; height: 112
                    property bool warm: !window.conversationOnly
                    onWarmChanged: requestPaint()
                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)
                        const g = ctx.createRadialGradient(width / 2, height / 2, 4, width / 2, height / 2, width / 2)
                        const rgb = warm ? "232, 157, 90" : "91, 155, 248"
                        g.addColorStop(0.0, `rgba(${rgb}, 0.28)`)
                        g.addColorStop(0.45, `rgba(${rgb}, 0.10)`)
                        g.addColorStop(1.0, `rgba(${rgb}, 0.0)`)
                        ctx.fillStyle = g
                        ctx.fillRect(0, 0, width, height)
                    }
                }
                Image { anchors.centerIn: parent; source: brandUrl; width: 42; height: 42; smooth: true }
            }
            Text { text: window.conversationOnly ? "今天想聊些什么？" : "今天想完成什么？"; color: "#f0f1f3"; font.pixelSize: 30; font.weight: Font.DemiBold }
            Text { text: window.conversationOnly ? "可以使用已连接且获准的 MCP。需要实际操作时，AI 可切换到 Work。" : "描述目标，带上上下文，让想法成为下一步。"; color: "#9fa8b4"; font.pixelSize: 15; wrapMode: Text.Wrap; width: parent.width }
            Item { width: 1; height: 4 }
            Text { text: "选择角色"; color: "#7f8998"; font.pixelSize: 12 }
            // 角色模板：一键换成预设角色（指引、工具、权限上限、步数预算都随之切换）。
            Flow {
                objectName: "templatePicker"
                width: parent.width; spacing: 8
                GlassButton {
                    material: window.material; glyph: "bot"; text: "通用"; height: 34
                    selected: controller.templates.activeId === ""; enabled: !controller.busy
                    onClicked: controller.templates.select("")
                }
                Repeater {
                    model: controller.templates.rows
                    GlassButton {
                        required property var modelData
                        objectName: "templateChip_" + modelData.id
                        material: window.material; glyph: modelData.icon; text: modelData.name; height: 34
                        selected: modelData.active; enabled: !controller.busy; tip: modelData.description
                        onClicked: controller.templates.select(modelData.id)
                    }
                }
                GlassButton {
                    material: window.material; glyph: "sliders-horizontal"; text: "管理"; quiet: true; height: 34
                    tip: "复制并自定义角色"; onClicked: rolePicker.open()
                }
            }
            Text {
                text: controller.templates.activeDescription; color: "#a9b4c1"; font.pixelSize: 13
                wrapMode: Text.Wrap; width: parent.width
            }
            Flow {
                objectName: "templateStarters"
                visible: controller.templates.activeStarters.length > 0
                width: parent.width; spacing: 6
                Repeater {
                    model: controller.templates.activeStarters
                    GlassButton {
                        required property string modelData
                        material: window.material; glyph: "sparkles"; text: modelData; quiet: true; height: 32
                        tip: "填入输入框"; onClicked: controller.templates.useStarter(modelData)
                    }
                }
            }
        }
    }
    ShaderEffectSource {
        id: sceneTexture
        sourceItem: ambient
        textureSize: Qt.size(Math.ceil(window.width / 2), Math.ceil(window.height / 2))
        live: false; hideSource: false; visible: false
        smooth: true
    }
    ShaderEffect {
        id: horizontalBlur
        width: Math.ceil(window.width / 2); height: Math.ceil(window.height / 2)
        visible: false
        property var sourceTexture: sceneTexture
        property vector2d direction: Qt.vector2d(3 / window.width, 0)
        fragmentShader: "../shaders/blur.frag.qsb"
    }
    ShaderEffectSource { id: horizontalTexture; sourceItem: horizontalBlur; live: false; visible: false; hideSource: true }
    ShaderEffect {
        id: verticalBlur
        width: horizontalBlur.width; height: horizontalBlur.height
        visible: false
        property var sourceTexture: horizontalTexture
        property vector2d direction: Qt.vector2d(0, 3 / window.height)
        fragmentShader: "../shaders/blur.frag.qsb"
    }
    ShaderEffectSource { id: blurTexture; sourceItem: verticalBlur; live: false; visible: false; hideSource: true }
    function refreshBackdrop() {
        if (!gpuEnabled || controller.solid) return
        sceneTexture.scheduleUpdate()
        horizontalTexture.scheduleUpdate()
        blurTexture.scheduleUpdate()
    }
    onWidthChanged: Qt.callLater(refreshBackdrop)
    onHeightChanged: Qt.callLater(refreshBackdrop)
    Connections { target: controller; function onChanged() {
        if (window.material.solid !== window.lastSolid) {
            window.lastSolid = window.material.solid
            Qt.callLater(window.refreshBackdrop)
        }
    } }
    property bool lastSolid: false

    Row {
        visible: !window.codingVisible; x: 22; y: 23; spacing: 10
        Image { width: 27; height: 27; source: brandUrl; smooth: true }
        Text { text: "Keystone"; color: "#f2f3f5"; font.pixelSize: 21; font.weight: Font.DemiBold; anchors.verticalCenter: parent.verticalCenter }
    }
    GlassButton {
        visible: !window.codingVisible && !workbench.navigationOpen
        material: window.material; x: 18; y: 184; width: window.sideWidth - 36; height: 42
        glyph: "square-pen"; text: window.label("新建对话", "New chat"); enabled: !controller.busy
        onClicked: { workbench.navigate("chat"); controller.newSession() }
    }
    TextField {
        id: search
        visible: !window.codingVisible && !workbench.navigationOpen
        x: 18; y: 238; width: window.sideWidth - 36; height: 36
        placeholderText: window.label("搜索对话", "Search chats"); placeholderTextColor: Theme.textMuted
        color: Theme.textPrimary; leftPadding: 34; rightPadding: 10
        onTextChanged: window.sessionQuery = text.toLowerCase()
        background: GlassSurface { material: window.material; radius: 11; emphasis: search.activeFocus ? 0.6 : 0; tint: "#18263140" }
        Glyph { x: 11; anchors.verticalCenter: parent.verticalCenter; name: "search"; ink: search.activeFocus ? Theme.accent : Theme.textMuted; width: 15; height: 15 }
    }
    Text { visible: !window.codingVisible && !workbench.navigationOpen; x: 24; y: 298; text: window.label("最近对话", "Recent chats"); color: "#7f8998"; font.pixelSize: 11 }
    ListView {
        id: history
        visible: !window.codingVisible && !workbench.navigationOpen
        objectName: "sessionList"
        x: 12; y: 324; width: window.sideWidth - 24; height: window.height - y - 163
        clip: true; spacing: 7
        model: controller.sessions
        ScrollBar.vertical: ScrollBar { width: 3 }
        delegate: AbstractButton {
            id: session
            required property string sessionId
            required property string title
            required property string subtitle
            readonly property bool current: controller.sessionId === sessionId
            readonly property bool matches: window.sessionQuery.length === 0 || title.toLowerCase().includes(window.sessionQuery)
            width: history.width; height: matches ? 66 : 0; visible: matches
            enabled: !controller.busy; hoverEnabled: true
            leftPadding: 13; rightPadding: 13; topPadding: 14; bottomPadding: 12
            focusPolicy: Qt.StrongFocus
            Accessible.name: title
            Accessible.role: Accessible.ListItem
            background: GlassSurface {
                material: window.material; radius: 15
                visible: session.current || session.hovered || session.visualFocus
                emphasis: session.current ? 1 : 0.2
                hoverAmount: session.hovered ? 1 : 0
                pointer: sessionHover.point.position
            }
            HoverHandler { id: sessionHover; cursorShape: Qt.PointingHandCursor }
            contentItem: Column {
                spacing: 7
                Text { text: session.title; width: parent.width; elide: Text.ElideRight; color: session.current ? "#f4f5f6" : "#b2bbc6"; font.pixelSize: 13; font.weight: session.current ? Font.Medium : Font.Normal }
                Text { text: session.subtitle; color: "#7f8b99"; font.pixelSize: 11 }
            }
            onClicked: { workbench.navigate("chat"); controller.selectSession(sessionId) }
            Keys.onReturnPressed: clicked()
        }
    }
    Column {
        visible: !window.codingVisible; x: 18; y: window.height - 148; width: window.sideWidth - 36; spacing: 8
        GlassButton { objectName: "permissionButton"; material: window.material; width: parent.width; glyph: "shield"; text: workbench.permissionLabel; selected: true; tip: "切换执行权限"; enabled: !controller.busy && !workbench.busy && workbench.approvals.length === 0; onClicked: permissionPopup.open() }
        GlassButton { material: window.material; width: parent.width; glyph: "panels-top-left"; text: "Agent 协作设置"; quiet: true; onClicked: workbench.navigate("agents") }
        GlassButton { material: window.material; objectName: "connectionSettingsEntry"; width: parent.width; glyph: "settings-2"; text: window.label("连接与外观", "Connection & appearance"); quiet: true; onClicked: settings.open() }
    }
    Text {
        objectName: "pageTitle"
        visible: !window.codingVisible
        x: window.sideWidth + 28; y: 38 - height / 2; width: window.mainRight - x - 220
        text: window.chatVisible ? controller.title : workbench.pageTitle; elide: Text.ElideRight
        color: Theme.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold
    }
    Rectangle { visible: window.chatVisible; x: window.sideWidth; y: 75; width: window.mainRight - x; height: 1; color: Theme.divider; opacity: messages.contentY > -messages.topMargin + 4 ? 1 : 0 }
    Row {
        visible: !window.codingVisible; x: window.mainRight - width - 20; y: 20; spacing: 8
        GlassButton { objectName: "quickSetupToolbar"; visible: workbench.page === "config"; material: window.material; glyph: "plug"; text: window.label("一键配置", "Quick setup"); onClicked: onboarding.showSetup() }
        GlassButton { objectName: "manageModelsToolbar"; visible: workbench.page === "config"; material: window.material; glyph: "settings-2"; text: "管理模型"; onClicked: window.openModelEditor("") }
        GlassButton { objectName: "mediaModelsToolbar"; visible: workbench.page === "config"; material: window.material; glyph: "image"; text: window.label("图片与视频", "Image and video"); onClicked: mediaModels.showSettings() }
        GlassButton { visible: workbench.page === "config" && setup.developer; material: window.material; glyph: "box"; text: "模型厂商"; onClicked: workbench.showProviderCatalog("") }
        GlassButton { visible: workbench.page === "connector_accounts"; material: window.material; glyph: "chevron-left"; text: "MCP 接口"; onClicked: workbench.navigate("connectors") }
        GlassButton { visible: workbench.page === "tools"; material: window.material; glyph: "blocks"; text: "Work 工具"; onClicked: toolCatalog.open() }
        GlassButton { visible: window.chatVisible; material: window.material; glyph: "pencil"; quiet: true; tip: "重命名对话"; onClicked: { renameField.text = controller.title; renamePopup.open() } }
        GlassButton {
            objectName: "filesToggle"; material: window.material
            glyph: workbench.page === "files" ? "chevron-left" : "folder-open"
            text: workbench.page === "files" ? "收起文件" : "展开文件"
            quiet: workbench.page !== "files"; onClicked: workbench.toggleFiles()
        }
    }
    GlassButton {
        material: window.material; text: "回到最新"; glyph: "arrow-down"
        anchors.horizontalCenter: composer.horizontalCenter; y: composer.y - 48
        visible: window.chatVisible && !window.followTail && messages.count > 0
        onClicked: { window.followTail = true; messages.positionViewAtEnd() }
    }
    Item {
        id: composer
        visible: window.chatVisible
        objectName: "composer"
        x: window.columnX; width: window.columnWidth
        y: window.height - height - 42
        height: composerColumn.height + 32
        GlassSurface {
            id: composerGlass
            anchors.fill: parent; material: window.material; radius: 24
            emphasis: editor.activeFocus ? 0.45 : 0
            hoverAmount: composerHover.hovered ? 1 : 0
            pointer: composerHover.point.position
            tint: "#143e4654"
        }
        HoverHandler { id: composerHover }
        MouseArea { anchors.fill: parent; onClicked: editor.forceActiveFocus(); cursorShape: Qt.IBeamCursor }
        Column {
            id: composerColumn
            x: 18; y: 16; width: parent.width - 36; spacing: 12
            Flow {
                width: parent.width; spacing: 6
                visible: controller.attachments.length > 0
                Repeater {
                    model: controller.attachments
                    GlassButton { required property string modelData; required property int index; material: window.material; text: modelData; glyph: "x"; height: 29; onClicked: controller.removeAttachment(index) }
                }
            }
            ComposerText {
                id: editorScroll
                width: parent.width
                height: Math.min(Math.max(60, editor.contentHeight + 10), Math.max(60, window.height * 0.19))
                editorName: "messageEditor"
                placeholder: "向 Keystone 描述你的想法…"
                text: controller.draft
                onTextChanged: if (controller.draft !== text) controller.draft = text
                onSubmit: controller.send()
            }
            // 左侧是决定这条消息怎么跑的三项：模型、思考强度、角色；右侧是附加能力与发送。
            RowLayout {
                id: composerBar
                width: parent.width; spacing: 6
                readonly property bool roomy: window.columnWidth > 640
                readonly property int modelChars: window.columnWidth < 560 ? 10 : window.columnWidth < 700 ? 16 : 24
                GlassButton {
                    id: modelButton
                    objectName: "modelButton"
                    material: window.material; glyph: "box"
                    text: controller.model.length > composerBar.modelChars ? controller.model.slice(0, composerBar.modelChars - 1) + "…" : controller.model
                    selected: modelMenu.opened
                    enabled: !controller.busy; tip: "切换模型：" + controller.model; onClicked: modelMenu.open()
                }
                GlassButton {
                    id: effortButton
                    objectName: "effortButton"
                    readonly property var names: ({off: "默认", low: "轻量", medium: "标准", high: "深入"})
                    material: window.material; glyph: "gauge"
                    text: (window.columnWidth > 520 ? "思考 · " : "") + names[controller.effectiveReasoning]
                    selected: effortPicker.opened
                    ink: controller.reasoningSupported ? "#dce3ec" : Theme.textMuted
                    glyphInk: controller.reasoningSupported ? (controller.effectiveReasoning === "off" ? "#dce3ec" : Theme.accent) : Theme.textDim
                    enabled: !controller.busy
                    tip: controller.reasoningSupported ? "思考强度（点击调整）" : "当前模型不支持调节思考强度"
                    onClicked: effortPicker.open()
                }
                GlassButton {
                    id: optionsButton
                    material: window.material; glyph: "sliders-horizontal"; text: window.columnWidth > 600 ? "选项" : ""
                    selected: options.opened; tip: "事前规划与外观"; onClicked: options.open()
                }
                GlassButton {
                    id: roleButton
                    objectName: "templateButton"
                    material: window.material; glyph: controller.templates.activeIcon
                    text: window.columnWidth > 600 ? controller.templates.activeName : ""
                    selected: controller.templates.activeId !== ""
                    enabled: !controller.busy; tip: "当前角色：" + controller.templates.activeName + "（点击切换）"
                    onClicked: rolePicker.open()
                }
                Item { Layout.fillWidth: true }
                GlassButton { objectName: "attachButton"; material: window.material; glyph: "paperclip"; quiet: true; tip: "添加文本附件"; enabled: !controller.busy; onClicked: files.open() }
                GlassButton {
                    objectName: "publicSearchButton"
                    visible: !window.conversationOnly
                    material: window.material; glyph: "search"; quiet: true
                    text: composerBar.roomy ? "搜索" : ""
                    tip: "联网搜索"; onClicked: searchPanel.open()
                }
                GlassButton {
                    objectName: "workToolCatalogButton"
                    material: window.material; glyph: "wrench"; quiet: true
                    text: composerBar.roomy ? "工具" : ""
                    tip: controller.toolSettings.enabled ? "管理当前模式的工具" : "工具调用已关闭"
                    onClicked: toolCatalog.open()
                }
                GlassButton {
                    objectName: "chatMcpButton"
                    visible: window.conversationOnly
                    material: window.material; glyph: "plug"; text: "MCP"; quiet: true
                    tip: "管理 Chat 可用的远程 MCP"; onClicked: workbench.navigate("connectors")
                }
                Item { width: 2 }
                GlassButton {
                    objectName: "sendButton"
                    material: window.material; glyph: controller.busy ? "square" : "arrow-up"
                    width: 38; height: 38; cornerRadius: 19; primary: !controller.busy; selected: controller.busy
                    enabled: controller.busy || (!controller.modelSwitching && editor.text.trim().length > 0)
                    tip: controller.busy ? "停止生成" : "发送消息"
                    onClicked: controller.busy ? controller.stop() : controller.send()
                }
            }
        }
    }
    Text { visible: window.chatVisible; x: composer.x + 4; y: window.height - 27; text: controller.status; color: "#7e8998"; font.pixelSize: 11 }
    Text { visible: window.chatVisible && window.columnWidth > 520; anchors.right: composer.right; y: window.height - 27; text: "Enter 发送  ·  Shift + Enter 换行"; color: "#7e8998"; font.pixelSize: 11 }
    Rectangle {
        id: errorBanner
        visible: window.chatVisible && controller.error.length > 0
        x: composer.x; y: composer.y - height - 12; width: composer.width
        height: errorText.implicitHeight + 24; radius: 12
        color: Theme.dangerSurface; border.color: Theme.dangerBorder; border.width: 1
        Glyph { x: 12; y: 12; name: "triangle-alert"; ink: Theme.danger; width: 16; height: 16 }
        Text { id: errorText; x: 34; y: 12; width: parent.width - 46; text: controller.error; color: Theme.dangerText; wrapMode: Text.Wrap; font.pixelSize: 12 }
        TapHandler { onTapped: if (!controller.busy) settings.open() }
    }

    Popup {
        id: modelMenu
        focus: true
        parent: Overlay.overlay
        x: Math.min(composer.x + 18, window.width - width - 16)
        y: Math.max(16, composer.y - height - 10)
        width: Math.min(340, window.columnWidth); padding: 12
        height: Math.min(440, controller.models.length * 41 + 104)
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { material: window.material; radius: 20; emphasis: 0.3 }
        onAboutToShow: controller.refreshModelChoices()
        onClosed: if (window.chatVisible && !modelEditor.opened && !onboarding.opened) window.focusWorkComposer()
        contentItem: Column {
            spacing: 5
            Text { text: "模型"; color: "#9aa7b5"; font.pixelSize: 12; padding: 6 }
            ListView {
                id: modelList
                width: parent.width; height: modelMenu.availableHeight - 78
                model: controller.models; spacing: 5; clip: true
                ScrollBar.vertical: ScrollBar { width: 4 }
                delegate: GlassButton { required property string modelData; material: window.material; width: ListView.view.width; text: modelData.length > 33 ? modelData.slice(0, 32) + "…" : modelData; selected: controller.model === modelData; enabled: !controller.modelSwitching; quiet: true; onClicked: {controller.setModel(modelData); modelMenu.close()} }
            }
            Rectangle { width: parent.width; height: 1; color: Theme.divider }
            Row {
                width: parent.width; spacing: 6
                GlassButton {
                    objectName: "manageModelsButton"
                    width: (parent.width - 6) / 2; material: window.material; glyph: "settings-2"; text: "管理模型"; quiet: true
                    enabled: !controller.isDemo
                    onClicked: { modelMenu.close(); window.openModelEditor("") }
                }
                GlassButton {
                    width: (parent.width - 6) / 2; material: window.material; glyph: "plus"; text: "添加模型"; quiet: true
                    onClicked: { modelMenu.close(); onboarding.showSetup() }
                }
            }
        }
    }
    Popup {
        id: chartPreview
        objectName: "chartAttachmentPreview"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(960, window.width - 48)
        height: Math.min(680, window.height - 48)
        padding: 16; modal: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { material: window.material; radius: 18; tint: "#ed1c2430" }
        onClosed: chartImage.source = ""
        contentItem: ColumnLayout {
            spacing: 10
            RowLayout {
                Layout.fillWidth: true
                Text {
                    id: chartName; Layout.fillWidth: true
                    textFormat: Text.PlainText; elide: Text.ElideMiddle; color: Theme.textPrimary
                }
                GlassButton { objectName: "closeChartPreview"; material: window.material; glyph: "x"; tip: "关闭预览"; onClicked: chartPreview.close() }
            }
            Image {
                id: chartImage; objectName: "attachmentChartImage"
                Layout.fillWidth: true; Layout.fillHeight: true; Layout.minimumWidth: 0; Layout.minimumHeight: 0
                fillMode: Image.PreserveAspectFit; asynchronous: true; mipmap: true
                Accessible.name: chartName.text
            }
            Text { visible: chartImage.status === Image.Error; text: "无法读取图表文件。"; color: Theme.warning }
        }
        Connections {
            target: controller
            function onChartPreviewRequested(url, filename) {
                chartName.text = filename
                chartImage.source = url
                chartPreview.open()
            }
        }
    }
    EffortPicker {
        id: effortPicker
        parent: Overlay.overlay
        x: Math.max(16, Math.min(composer.x + 18 + effortButton.x, window.width - width - 16))
        y: Math.max(16, composer.y - height - 10)
        material: window.material
        service: controller
        onClosed: if (window.chatVisible) window.focusWorkComposer()
    }
    ModelEditor {
        id: modelEditor
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2); y: Math.round((parent.height - height) / 2)
        width: Math.min(860, window.width - 48)
        height: Math.min(600, window.height - 48)
        material: window.material
        service: workbench.modelRows
        changesAllowed: !controller.busy
        onAddRequested: onboarding.showSetup()
        onClosed: if (window.chatVisible) window.focusWorkComposer()
    }
    function openModelEditor(rowId) {
        workbench.modelRows.reload()
        modelEditor.openFor(rowId)
    }
    RolePicker {
        id: rolePicker
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2); y: Math.round((parent.height - height) / 2)
        width: Math.min(720, window.width - 48)
        height: Math.min(660, window.height - 48)
        material: window.material
        service: controller.templates
        changesAllowed: !controller.busy
        onClosed: window.focusWorkComposer()
    }
    WorkToolCatalog {
        id: toolCatalog
        parent: Overlay.overlay
        material: window.material
        service: controller.toolSettings
        chatMode: window.conversationOnly
        changesAllowed: !controller.busy
        onClosed: window.focusWorkComposer()
    }

    Popup {
        id: options
        focus: true
        objectName: "glassOptions"
        onAboutToShow: controller.planning.refresh()
        parent: Overlay.overlay
        x: Math.min(composer.x + 170, window.width - width - 20)
        y: Math.max(8, Math.min(window.height - height - 8, composer.y - height - 12))
        width: 348; padding: 20
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { objectName: "optionsGlass"; material: window.material; radius: 22; emphasis: 0.4 }
        onClosed: if (window.chatVisible) window.focusWorkComposer()
        contentItem: Column {
            spacing: 12
            Text { text: "对话选项"; color: "#eef1f4"; font.pixelSize: 16; font.weight: Font.DemiBold }
            Text { text: "事前规划"; color: Theme.textSecondary; font.pixelSize: 12 }
            Row {
                spacing: 7
                Repeater { model: [{key:"auto",label:"自动判断"},{key:"manual",label:"手动开启"}]
                    GlassButton { required property var modelData; material: window.material; width: 124; text: modelData.label
                        selected: controller.planning.policy === modelData.key; enabled: !controller.busy
                        onClicked: controller.planning.save("policy", modelData.key) }
                }
            }
            Text { text: controller.planning.policy === "auto" ? "多步骤或复杂任务先规划，简单任务直接执行。" : "下一条消息的规划"; color: Theme.textSecondary; font.pixelSize: 12 }
            Row {
                visible: controller.planning.policy === "manual"; spacing: 7
                Repeater { model: [{key:"none",label:"关闭"},{key:"low",label:"简要"},{key:"medium",label:"标准"},{key:"high",label:"深入"}]
                    GlassButton { required property var modelData; material: window.material; width: 70; text: modelData.label
                        selected: controller.planning.level === modelData.key; enabled: !controller.busy
                        onClicked: controller.planning.save("level", modelData.key) }
                }
            }
            WorkbenchField {
                width: parent.width; material: window.material; enabled: !controller.busy
                definition: ({key: "planningModel", label: "规划模型", kind: "choice",
                              value: controller.planning.model, options: controller.planning.options})
                onDefinitionChanged: value = definition.value
                onEdited: value => controller.planning.save("model", value)
            }
            GlassButton {
                width: parent.width; material: window.material; glyph: "sliders-horizontal"
                objectName: "managePlanningKey"
                text: controller.planning.hasKey ? "管理独立规划密钥" : "设置独立规划密钥"
                enabled: !!controller.planning.endpoint && !controller.busy && !controller.isDemo
                onClicked: { options.close(); plannerKey.open() }
            }
            Text { text: "外观"; color: Theme.textSecondary; font.pixelSize: 12; topPadding: 4 }
            Text { text: "玻璃通透度"; color: "#a9b3bf"; font.pixelSize: 12 }
            Slider {
                id: materialSlider
                width: parent.width; from: 0; to: 1; value: controller.material
                onMoved: controller.setMaterial(value)
                background: GlassSurface { material: window.material; x: materialSlider.leftPadding; y: materialSlider.topPadding + materialSlider.availableHeight / 2 - 4; width: materialSlider.availableWidth; height: 8; radius: 4 }
                handle: Rectangle { x: materialSlider.leftPadding + materialSlider.visualPosition * (materialSlider.availableWidth - width); y: materialSlider.topPadding + materialSlider.availableHeight / 2 - height / 2; width: 22; height: 22; radius: 11; color: "#e7eaed"; border.color: "#7f919ca9" }
            }
            GlassButton { material: window.material; width: parent.width; text: controller.solid ? "✓  降低透明度" : "降低透明度"; selected: controller.solid; onClicked: controller.setSolid(!controller.solid) }
            GlassButton { material: window.material; width: parent.width; text: controller.reduceMotion ? "✓  减少动态效果" : "减少动态效果"; selected: controller.reduceMotion; onClicked: controller.setReduceMotion(!controller.reduceMotion) }
        }
    }
    Popup {
        id: plannerKey
        objectName: "plannerKeyPopup"
        parent: Overlay.overlay; anchors.centerIn: parent
        modal: true; focus: true; width: Math.min(window.width - 44, 416); padding: 20
        property string selection: ""
        property string address: ""
        property string feedback: ""
        function saveKey(value) {
            if (controller.planning.setCredential(selection, address, value)) close()
            else feedback = controller.error || "当前无法保存，请重新打开设置。"
        }
        onAboutToShow: {
            controller.planning.refresh()
            selection = controller.planning.model
            address = controller.planning.endpoint
            feedback = ""
        }
        onClosed: plannerPassword.clear()
        background: GlassSurface { material: window.material; radius: 20; tint: "#ed18212b" }
        contentItem: Column {
            spacing: 12
            Text { text: "独立规划密钥"; color: Theme.textPrimary; font.pixelSize: 17; font.weight: Font.DemiBold }
            Text { width: parent.width; color: Theme.textSecondary; wrapMode: Text.WrapAnywhere; textFormat: Text.PlainText
                   text: (plannerKey.selection ? JSON.parse(plannerKey.selection).join(" / ") : "") + "\n" + plannerKey.address }
            TextField {
                id: plannerPassword; objectName: "plannerPassword"; width: parent.width
                echoMode: TextInput.Password; inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
                Accessible.name: "规划专用 API 密钥"; placeholderText: "填入此模型的规划专用 API 密钥"
                selectByMouse: true; color: Theme.textPrimary; placeholderTextColor: Theme.textMuted; leftPadding: 10
                background: GlassSurface { material: window.material; radius: 10; emphasis: parent.activeFocus ? 0.8 : 0 }
            }
            Text { visible: !!plannerKey.feedback; width: parent.width; wrapMode: Text.Wrap
                   text: plannerKey.feedback; color: Theme.dangerText; textFormat: Text.PlainText }
            Row {
                spacing: 8
                GlassButton { objectName: "savePlannerKey"; material: window.material; text: "保存密钥"; enabled: !!plannerPassword.text && !controller.busy
                    onClicked: plannerKey.saveKey(plannerPassword.text) }
                GlassButton { material: window.material; text: "恢复提供商密钥"; visible: controller.planning.hasKey; enabled: !controller.busy
                    onClicked: plannerKey.saveKey("") }
                GlassButton { material: window.material; text: "关闭"; onClicked: plannerKey.close() }
            }
        }
    }
    Popup {
        id: settings
        objectName: "connectionSettings"
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, window.width - 48); padding: 24
        modal: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        Overlay.modal: Rectangle { color: "#6605080d" }
        background: GlassSurface { material: window.material; radius: 24; emphasis: 0.6 }
        onOpened: endpoint.text = controller.gateway
        onClosed: if (window.chatVisible) window.focusWorkComposer()
        contentItem: Column {
            spacing: 16
            Text { text: "连接与外观"; color: "#f0f2f4"; font.pixelSize: 20; font.weight: Font.DemiBold }
            Text { text: "Keystone 网关地址"; color: "#a7b3c0"; font.pixelSize: 13 }
            TextField { id: endpoint; width: parent.width; height: 42; color: "#e2e7ec"; selectByMouse: true; leftPadding: 12; background: GlassSurface { material: window.material; radius: 12; emphasis: endpoint.activeFocus ? 0.8 : 0 } }
            Text { width: parent.width; text: "沿用现有 Keystone 网关。Coding、工具和连接器均在当前工作台内打开。"; color: "#939eac"; wrapMode: Text.Wrap; font.pixelSize: 12 }
            GlassButton { material: window.material; width: parent.width; text: "应用并连接"; glyph: "plug"; enabled: !controller.busy; onClicked: {controller.setGateway(endpoint.text); settings.close()} }
            GlassButton { objectName: "manageModelsEntry"; material: window.material; width: parent.width; text: "管理已添加的模型"; glyph: "settings-2"; enabled: !controller.isDemo; onClicked: { settings.close(); window.openModelEditor("") } }
            GlassButton { objectName: "quickSetupEntry"; material: window.material; width: parent.width; text: window.label("一键配置模型", "Quick model setup"); glyph: "plug"; onClicked: { settings.close(); onboarding.showSetup() } }
            GlassButton { objectName: "welcomeGuideEntry"; material: window.material; width: parent.width; text: window.label("重新运行首次引导", "Run setup guide"); glyph: "sparkles"; onClicked: { settings.close(); onboarding.showGuide() } }
            GlassButton { material: window.material; width: parent.width; text: "壁纸"; glyph: "image"; onClicked: { settings.close(); wallpaperSettings.open() } }
            GlassButton { material: window.material; width: parent.width; text: "调整玻璃外观"; glyph: "sliders-horizontal"; onClicked: {settings.close(); options.open()} }
            GlassButton { material: window.material; width: parent.width; text: "打开完整工作台"; quiet: true; onClicked: controller.openWorkbench("config") }
            Text { width: parent.width; text: gpuEnabled ? "GPU 渲染 · 原生文本输入" : "兼容渲染 · 玻璃光学效果不可用"; color: "#798797"; font.pixelSize: 11 }
        }
    }
    PublicSearch {
        id: searchPanel
        parent: Overlay.overlay; material: window.material
        onRestoreComposer: window.focusWorkComposer()
        width: Math.min(710, window.width - 36); height: Math.min(610, window.height - 36)
    }
    WallpaperSettings {
        id: wallpaperSettings
        parent: Overlay.overlay
        x: Math.round((parent.width - width) / 2); y: Math.round((parent.height - height) / 2)
        width: Math.min(520, window.width - 48)
        height: Math.min(implicitHeight, window.height - 48)
        material: window.material; wallpaper: window.wallpaper
        reduced: controller.reduceMotion
        onClosed: if (window.chatVisible) window.focusWorkComposer()
    }
    ChangeReview {
        id: changeReview
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(1040, window.width - 64); height: Math.min(760, window.height - 64)
        material: window.material; service: controller.changeReview
        onClosed: editor.forceActiveFocus()
    }
    Rectangle {
        visible: workbench.localTools.message !== "" || workbench.mediaModels.status !== ""
        x: window.mainRight - width - 24; y: 66; width: Math.min(410, window.mainRight - window.sideWidth - 48)
        height: 64; radius: 12; color: "#ed202c39"; z: 3
        Column {
            anchors.fill: parent; anchors.margins: 12; spacing: 8
            Text { width: parent.width; text: workbench.mediaModels.status || workbench.localTools.message; color: "#dce9f5"; elide: Text.ElideRight; font.pixelSize: 12 }
            ProgressBar {
                width: parent.width; from: 0; to: 100
                value: Math.max(0, workbench.localTools.percent)
                indeterminate: workbench.mediaModels.status !== "" || workbench.localTools.percent < 0
            }
        }
    }
    MediaModels {
        id: mediaModels
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(620, window.width - 48)
        material: window.material; service: workbench.mediaModels; english: window.english
        onClosed: window.focusWorkComposer()
    }
    LocalToolSetup {
        id: localToolSetup
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(560, window.width - 48)
        material: window.material; service: workbench.localTools
        onClosed: window.focusWorkComposer()
    }
    Onboarding {
        id: onboarding
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(610, window.width - 48)
        material: window.material; service: window.setup
        onMediaModelsRequested: mediaModels.showSettings()
        onClosed: window.focusWorkComposer()
    }
    ProviderCatalog {
        id: providerCatalog
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(860, window.width - 48); height: Math.min(660, window.height - 48)
        material: window.material; setup: window.setup
        onSetupRequested: (provider, url) => onboarding.showSetup(provider, url)
        onClosed: if (window.chatVisible) window.focusWorkComposer()
    }
    Popup {
        id: reviseMessage; objectName: "reviseMessagePopup"
        parent: Overlay.overlay; anchors.centerIn: parent
        width: Math.min(600, window.width - 48); padding: 22; modal: true; focus: true
        property string sessionId: ""
        property int historyIndex: -1
        property bool withdrawing: false
        property string failure: ""
        function begin(index, text, withdraw) {
            sessionId = controller.sessionId
            historyIndex = index
            withdrawing = withdraw
            revisionText.text = withdraw ? "" : text
            failure = ""
            open()
        }
        onOpened: {
            if (withdrawing) cancelRevision.forceActiveFocus()
            else revisionText.forceActiveFocus()
        }
        onClosed: editor.forceActiveFocus()
        background: GlassSurface { material: window.material; radius: 20; tint: "#ed18212b" }
        contentItem: ColumnLayout {
            spacing: 14
            Text {
                text: reviseMessage.withdrawing ? "撤回消息" : "编辑消息"
                color: "#edf0f3"; font.pixelSize: 19; font.weight: Font.DemiBold
            }
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap
                text: reviseMessage.withdrawing
                    ? "这条消息及之后的对话将从会话中移除。"
                    : "重新发送会替换这条消息，并从这里重新生成回复。之后的对话将被移除。"
                color: "#a7b3c0"; font.pixelSize: 13
            }
            ScrollView {
                visible: !reviseMessage.withdrawing
                Layout.fillWidth: true; Layout.preferredHeight: 160
                clip: true
                background: Rectangle { color: "#111820"; radius: 9; border.color: "#415064" }
                TextArea {
                    id: revisionText; objectName: "revisionText"
                    Accessible.name: "已发送消息的内容"
                    color: "#e4eaf0"; font.pixelSize: 15; padding: 12
                    selectByMouse: true; wrapMode: TextEdit.Wrap
                    background: null
                }
            }
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap
                text: "已执行的文件修改可通过修改卡片单独回退。"
                color: "#a7b3c0"; font.pixelSize: 12
            }
            Text {
                Layout.fillWidth: true; wrapMode: Text.Wrap; visible: !!text
                text: reviseMessage.failure; color: "#e0af9b"; font.pixelSize: 12
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { id: cancelRevision; material: window.material; text: "取消"; quiet: true; onClicked: reviseMessage.close() }
                GlassButton {
                    objectName: "applyMessageRevision"
                    material: window.material; selected: true
                    text: reviseMessage.withdrawing ? "确认撤回" : "重新发送"
                    enabled: !controller.busy && (reviseMessage.withdrawing
                        || (revisionText.text.trim().length > 0 && !revisionText.inputMethodComposing))
                    onClicked: {
                        const success = reviseMessage.withdrawing
                            ? controller.withdrawMessage(reviseMessage.sessionId, reviseMessage.historyIndex)
                            : controller.editMessage(reviseMessage.sessionId, reviseMessage.historyIndex, revisionText.text)
                        if (success) reviseMessage.close()
                        else reviseMessage.failure = controller.error || "该消息目前无法修改。"
                    }
                }
            }
        }
    }
    Popup {
        id: renamePopup
        parent: Overlay.overlay; anchors.centerIn: parent; width: 380; padding: 22; modal: true; focus: true
        background: GlassSurface { material: window.material; radius: 20 }
        onClosed: if (window.chatVisible) window.focusWorkComposer()
        contentItem: Column { spacing: 16
            Text { text: "重命名对话"; color: "#edf0f3"; font.pixelSize: 17 }
            TextField { id: renameField; width: parent.width; color: "#edf0f3"; background: GlassSurface { material: window.material; radius: 10 } onAccepted: {controller.renameSession(text); renamePopup.close()} }
            GlassButton { material: window.material; text: "保存"; width: parent.width; onClicked: {controller.renameSession(renameField.text); renamePopup.close()} }
        }
    }
    FileDialog { id: files; title: "添加文本附件"; nameFilters: ["文本与代码 (*.txt *.md *.py *.js *.ts *.json *.qml *.css *.html)", "所有文件 (*)"]; onAccepted: controller.addAttachment(selectedFile) }
    Column {
        visible: !window.codingVisible; x: 18; y: 82; width: window.sideWidth - 36; spacing: 9
        Row {
            id: modeBar
            width: parent.width; spacing: 6
            readonly property real buttonWidth: setup.developer ? (width - 2 * spacing) / 3 : (width - spacing) / 2
            GlassButton {
                objectName: "selectChatMode"; material: window.material; width: modeBar.buttonWidth
                glyph: modeBar.buttonWidth >= 68 ? "message-square" : ""; text: "Chat"; selected: window.chatVisible && window.conversationOnly
                tip: "普通对话及 MCP"; enabled: !controller.busy
                onClicked: { controller.setConversationMode("chat"); workbench.navigate("chat"); window.focusWorkComposer() }
            }
            GlassButton {
                objectName: "selectWorkMode"; material: window.material; width: modeBar.buttonWidth
                glyph: modeBar.buttonWidth >= 68 ? "wrench" : ""; text: "Work"; selected: window.chatVisible && !window.conversationOnly
                tip: "执行任务与使用工具"; enabled: !controller.busy
                onClicked: { controller.setConversationMode("work"); workbench.navigate("chat"); window.focusWorkComposer() }
            }
            GlassButton { material: window.material; width: modeBar.buttonWidth; glyph: modeBar.buttonWidth >= 68 ? "code" : ""; text: "Coding"; visible: setup.developer; selected: window.codingVisible; onClicked: workbench.navigate("development") }
        }
        Row {
            spacing: 8
            GlassButton { material: window.material; width: (parent.parent.width - 8) / 2; text: window.label("功能", "Tools"); glyph: "grid-2x2"; selected: workbench.navigationOpen; onClicked: workbench.toggleNavigation() }
            GlassButton {
                material: window.material; width: (parent.parent.width - 8) / 2
                text: workbench.browserVisible ? "收起浏览器" : "浏览器"
                glyph: workbench.browserVisible ? "chevron-right" : "globe"
                selected: workbench.browserVisible; onClicked: workbench.toggleBrowser()
            }
        }
    }
    Flickable {
        visible: !window.codingVisible && workbench.navigationOpen
        x: 18; y: 184; width: window.sideWidth - 36; height: window.height - y - 164
        contentHeight: navigation.height; clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { width: 3 }
        Column {
            id: navigation; width: parent.width; spacing: 7
            Repeater {
                model: [
                    {key:"task", label:window.label("任务", "Tasks"), icon:"list-checks"},
                    {key:"files", label:window.label("文件与项目", "Files & projects"), icon:"folder"},
                    {key:"search", label:window.label("联网搜索", "Web search"), icon:"search"},
                    {key:"tools", label:window.label("工具与插件", "Tools & plugins"), icon:"blocks"},
                    {key:"config", label:window.label("模型与配置", "Model settings"), icon:"settings-2"},
                    {key:"agents", label:"Agents", icon:"users"},
                    {key:"knowledge", label:window.label("知识库", "Knowledge"), icon:"book-open"},
                    {key:"evolution", label:window.label("演化", "Evolution"), icon:"sparkles"},
                    {key:"schedules", label:window.label("定时任务", "Schedules"), icon:"calendar-clock"},
                    {key:"connectors", label:window.label("连接器", "Connectors"), icon:"plug"},
                    {key:"computer", label:window.label("电脑操作", "Computer use"), icon:"monitor"},
                    {key:"preview", label:window.label("代码预览", "Code preview"), icon:"file-code"}
                ].filter(item => (setup.developer || item.key !== "evolution") && (!window.conversationOnly || item.key !== "computer"))
                GlassButton {
                    required property var modelData
                    material: window.material; width: navigation.width
                    text: modelData.label; glyph: modelData.icon; quiet: true
                    selected: workbench.page === modelData.key
                    onClicked: {
                        if (modelData.key === "search") searchPanel.open()
                        else if (modelData.key === "files") workbench.toggleFiles()
                        else if (modelData.key === "config" && !setup.developer) onboarding.showSetup()
                        else workbench.navigate(modelData.key)
                    }
                }
            }
        }
    }
    Rectangle {
        visible: window.codingVisible; width: window.mainRight; height: 39; color: "#151b26"
        RowLayout {
            anchors.fill: parent; anchors.leftMargin: 12; anchors.rightMargin: 8; spacing: 4
            Image { source: brandUrl; Layout.preferredWidth: 20; Layout.preferredHeight: 20 }
            Text { text: "Keystone"; color: "#e8edf5"; font.pixelSize: 13; font.weight: Font.DemiBold; rightPadding: 10 }
            IdeButton { text: "Chat"; enabled: !controller.busy; onClicked: { controller.setConversationMode("chat"); workbench.navigate("chat") } }
            IdeButton { text: "Work"; enabled: !controller.busy; onClicked: { controller.setConversationMode("work"); workbench.navigate("chat") } }
            Text { text: "Code"; color: "#9bc6ff"; font.pixelSize: 12; font.weight: Font.DemiBold; leftPadding: 6; rightPadding: 6 }
            Item { Layout.fillWidth: true }
            IdeButton { glyph: "search"; text: "快速打开文件    Ctrl+P"; selected: true; enabled: workbench.nativePageReady && !workbench.busy; onClicked: window.quickFileRequested() }
            Item { Layout.fillWidth: true }
            IdeButton { glyph: "globe"; tip: "内置浏览器"; selected: workbench.browserVisible; onClicked: workbench.toggleBrowser() }
            IdeButton { glyph: "message-square"; text: "AI 对话"; selected: window.agentPaneOpen; onClicked: window.agentPaneOpen = !window.agentPaneOpen }
            IdeButton { glyph: "settings-2"; tip: "连接与外观"; onClicked: settings.open() }
        }
        Rectangle { anchors.bottom: parent.bottom; width: parent.width; height: 1; color: "#263142" }
    }
    Item {
        id: nativePage; objectName: "nativePage"
        x: window.codingVisible ? 0 : window.sideWidth + 12
        y: window.codingVisible ? 39 : 76
        width: window.mainRight - x - (window.codingVisible ? window.codingWidth : 12)
        height: window.height - y - (window.codingVisible ? 0 : 12)
        visible: !window.chatVisible && workbench.page !== "connectors"

        Rectangle {
            id: hostError; width: parent.width; height: hostErrorText.height + 24
            visible: workbench.nativePageReady && workbench.error !== ""
            color: Theme.dangerSurface; border.color: Theme.dangerBorder; border.width: 1; radius: 10
            Glyph { x: 12; y: 12; name: "triangle-alert"; ink: Theme.danger; width: 16; height: 16 }
            Text { id: hostErrorText; x: 34; y: 12; width: parent.width - 92; text: workbench.error; color: Theme.dangerText; wrapMode: Text.Wrap; font.pixelSize: 12 }
            GlassButton { material: window.material; glyph: "x"; x: parent.width - width - 8; y: 8; tip: "关闭提示"; onClicked: workbench.clearError() }
        }
        Column {
            visible: !workbench.nativePageReady; anchors.centerIn: parent; width: parent.width - 64; spacing: 16
            Text { width: parent.width; text: workbench.error || "正在载入 " + workbench.pageTitle + "…"; color: "#b9c4d0"; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter }
            GlassButton { material: window.material; text: "重新载入"; visible: workbench.error !== ""; anchors.horizontalCenter: parent.horizontalCenter; onClicked: workbench.navigate(workbench.page) }
        }
        Repeater {
            model: ["development", "task", "files", "tools", "config", "agents", "knowledge", "evolution", "schedules", "preview", "computer", "connector_accounts"]
            Loader {
                required property string modelData
                property bool selected: workbench.page === modelData
                property bool visited: false
                onSelectedChanged: if (selected) visited = true
                Component.onCompleted: if (selected) visited = true
                anchors.fill: parent
                anchors.topMargin: hostError.visible ? hostError.height + 8 : 0
                active: visited; asynchronous: true
                visible: selected && status === Loader.Ready && workbench.nativePageReady
                sourceComponent: WorkspacePage {
                    id: loadedPage; material: window.material; pageKey: modelData
                    Connections { target: window; enabled: loadedPage.visible && loadedPage.ide; function onQuickFileRequested() { loadedPage.openQuick() } }
                }
            }
        }
    }

    Loader {
        id: codingPanel
        property bool visited: false
        onVisibleChanged: if (visible) visited = true
        active: visited; asynchronous: true
        visible: window.codingVisible && window.agentPaneOpen && workbench.nativePageReady
        x: window.mainRight - width - 6; y: nativePage.y + 6
        width: Math.max(0, window.codingWidth - 12); height: nativePage.height - 12
        sourceComponent: CodingPanel {
            material: window.material
            onPermissionRequested: permissionPopup.open()
            onPlannerKeyRequested: plannerKey.open()
            Component.onCompleted: if (visible) Qt.callLater(focusComposer)
        }
    }
    Loader {
        id: connectorPage
        property bool visited: false
        onVisibleChanged: if (visible) visited = true
        active: visited; asynchronous: true
        visible: workbench.page === "connectors"
        x: window.sideWidth + 24; y: 78
        width: window.mainRight - x - 24; height: window.height - y - 18
        sourceComponent: ConnectorsPage { material: window.material }
    }
    Item {
        id: browserPanel; objectName: "browserPanel"
        x: workbench.browserExpanded ? window.sideWidth : window.mainRight
        width: workbench.browserExpanded ? window.width - window.sideWidth : window.browserPanelWidth
        height: parent.height; z: workbench.browserExpanded ? 5 : 0
        visible: workbench.browserVisible
        Loader { anchors.fill: parent; active: workbench.browserLoaded; asynchronous: true; sourceComponent: Component { BrowserPane { material: window.material } } }
        MouseArea {
            enabled: !workbench.browserExpanded
            x: -4; width: 8; height: parent.height; cursorShape: Qt.SplitHCursor
            property real startX
            property real startWidth
            onPressed: mouse => { startX = mapToItem(window.contentItem, mouse.x, mouse.y).x; startWidth = workbench.browserWidth }
            onPositionChanged: mouse => { if (pressed) workbench.resizeBrowser(startWidth + startX - mapToItem(window.contentItem, mouse.x, mouse.y).x) }
        }
    }
    Popup {
        id: permissionPopup; objectName: "permissionPopup"
        parent: Overlay.overlay; x: 14; y: Math.max(80, window.height - height - 160)
        width: window.sideWidth - 28; padding: 16; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { material: window.material; radius: 18; emphasis: 0.4 }
        contentItem: Column {
            spacing: 10
            Text { text: "运行权限"; color: "#ecf0f4"; font.pixelSize: 15; font.weight: Font.DemiBold }
            Repeater { model: [{key:"sandbox",label:"沙箱运行",hint:"工作区内读写，系统命令关闭"}, {key:"manual",label:"手动批准",hint:"每次工具执行前由你确认"}, {key:"full",label:"完全访问",hint:"不限工作区，保留工具拒绝规则"}]
                Column { required property var modelData; width: parent.width; spacing: 6
                    GlassButton { material: window.material; width: parent.width; text: modelData.label; selected: workbench.permission === modelData.key; enabled: !controller.busy && !workbench.busy && workbench.approvals.length === 0; onClicked: {workbench.setPermission(modelData.key); permissionPopup.close()} }
                    Text { width: parent.width; text: modelData.hint; wrapMode: Text.Wrap; color: "#99a6b5"; font.pixelSize: 11 }
                }
            }
            Text { width: parent.width; text: "沙箱采用 Keystone 工具与路径限制。权限不替代网站登录和连接器授权。"; wrapMode: Text.Wrap; color: "#7f8b9b"; font.pixelSize: 11 }
        }
    }
    Rectangle {
        visible: workbench.approvals.length > 0
        x: window.sideWidth + 20; y: 82; width: window.mainRight - x - 20; height: window.height - y - 30
        color: "#f019212b"; radius: 20
        ColumnLayout {
            anchors.fill: parent; anchors.margins: 24; spacing: 16
            Text { text: "等待你批准"; color: "#edf1f5"; font.pixelSize: 22; font.weight: Font.DemiBold }
            Text { Layout.fillWidth: true; text: workbench.approvals.length ? workbench.approvals[0].title : ""; color: "#c4d2df"; font.pixelSize: 16; wrapMode: Text.Wrap }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                TextArea { readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap; color: "#b8c3cf"; font.pixelSize: 13; text: workbench.approvals.length ? workbench.approvals[0].detail : ""; background: null }
            }
            RowLayout {
                Layout.fillWidth: true
                Text { text: "仅批准当前操作"; color: "#919faf"; font.pixelSize: 12 }
                Item { Layout.fillWidth: true }
                GlassButton { material: window.material; text: "拒绝"; onClicked: workbench.resolveApproval(workbench.approvals[0].id, false) }
                GlassButton { material: window.material; text: "批准本次"; selected: true; onClicked: workbench.resolveApproval(workbench.approvals[0].id, true) }
            }
        }
    }
    Connections {
        target: workbench
        function onPermissionRequested() { permissionPopup.open() }
        function onModelSetupRequested() { onboarding.showSetup() }
        function onModelEditRequested(rowId) { modelEditor.openFor(rowId) }
        function onHistoryRequested(rows) { codingHistory.rows = rows; codingHistory.open() }
    }
    Popup {
        id: codingHistory; property var rows: []
        parent: Overlay.overlay; anchors.centerIn: parent; width: Math.min(600, window.width - 60); height: Math.min(520, window.height - 80)
        modal: true; focus: true; padding: 20
        background: GlassSurface { material: window.material; radius: 20 }
        contentItem: ColumnLayout {
            Text { text: "Coding 对话历史"; color: "#edf1f5"; font.pixelSize: 18 }
            ListView {
                Layout.fillWidth: true; Layout.fillHeight: true; model: codingHistory.rows; clip: true; spacing: 7
                delegate: GlassButton { required property var modelData; width: ListView.view.width; material: window.material; text: modelData.stamp + " · " + modelData.label; onClicked: {workbench.codingAction("restore", modelData.path); codingHistory.close()} }
            }
            GlassButton { material: window.material; text: "关闭"; onClicked: codingHistory.close() }
        }
    }
    Popup {
        id: exitPopup; objectName: "exitPopup"; parent: Overlay.overlay; anchors.centerIn: parent
        width: 420; padding: 24; modal: true; focus: true
        background: GlassSurface { material: window.material; radius: 20 }
        contentItem: Column {
            spacing: 18
            Text { text: "退出工作台？"; color: "#eef2f6"; font.pixelSize: 20 }
            Text { width: parent.width; text: workbench.hasUnsaved ? "仍有未保存的编辑或设置。退出会丢弃这些改动，并停止正在运行的任务。" : "仍有任务在运行。退出会请求停止当前任务。"; wrapMode: Text.Wrap; color: "#acb8c6"; font.pixelSize: 14 }
            Row { spacing: 10
                GlassButton { material: window.material; text: "继续工作"; onClicked: exitPopup.close() }
                GlassButton { material: window.material; text: "确认退出"; onClicked: {window.exitConfirmed = true; window.close()} }
            }
        }
    }
    Shortcut { sequence: "Ctrl+N"; enabled: window.chatVisible && !reviseMessage.opened && !onboarding.opened; onActivated: controller.newSession() }
    Shortcut { sequence: "Ctrl+K"; enabled: window.chatVisible && !reviseMessage.opened && !onboarding.opened; onActivated: {search.forceActiveFocus(); search.selectAll()} }
    Shortcut { sequence: "Escape"; enabled: controller.busy && !options.opened && !settings.opened && !wallpaperSettings.opened && !searchPanel.opened && !modelMenu.opened && !effortPicker.opened && !modelEditor.opened && !onboarding.opened && !localToolSetup.opened && !mediaModels.opened && !toolCatalog.opened; onActivated: controller.stop() }
    Connections {
        target: controller
        function onDraftChanged() { if (editor.text !== controller.draft) editor.text = controller.draft }
        function onFocusComposer() { window.focusWorkComposer(); window.followTail = true; messages.positionViewAtEnd() }
        function onStreamAdvanced() { if (window.followTail) Qt.callLater(messages.positionViewAtEnd) }
    }
    Component.onCompleted: {
        if (setup.needsGuide && (typeof smokeMode === "undefined" || !smokeMode)) onboarding.showGuide()
        else focusWorkComposer()
        Qt.callLater(refreshBackdrop)
        Qt.callLater(controller.refreshConnection)
    }
}
