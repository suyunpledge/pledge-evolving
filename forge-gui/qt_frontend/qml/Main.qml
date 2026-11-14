import QtQuick
import QtQuick.Controls
import QtQuick.Layouts
import QtQuick.Dialogs

ApplicationWindow {
    id: window
    objectName: "forgeQuickWindow"
    width: 1280; height: 860
    minimumWidth: workbench.browserVisible ? 1080 : 820; minimumHeight: 620
    visible: typeof smokeMode === "undefined" || !smokeMode
    title: controller.isDemo ? "Forge · Integrated Workbench Preview" : "Forge"
    color: "#11151c"
    font: uiFont
    readonly property real sideWidth: width < 1000 ? 220 : 252
    readonly property real browserPanelWidth: workbench.browserVisible ? Math.min(workbench.browserWidth, Math.max(300, width - sideWidth - 450)) : 0
    readonly property real mainRight: width - browserPanelWidth
    readonly property bool chatVisible: workbench.page === "chat"
    readonly property real columnWidth: Math.min(800, mainRight - sideWidth - 48)
    readonly property real columnX: sideWidth + (mainRight - sideWidth - columnWidth) / 2
    property bool followTail: true
    property bool exitConfirmed: false
    onClosing: event => {
        if (!exitConfirmed && (workbench.hasUnsaved || workbench.busy || controller.busy)) {
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
        property real epoch: window.width + window.height + history.contentY + composer.y + composer.height + options.height + modelMenu.height + modelList.contentY + modelButton.width
    }

    // This is the ONLY background capture. Glass and its foreground controls
    // are siblings, so a surface can never recursively sample itself.
    Item {
        id: scene
        anchors.fill: parent
        ShaderEffect {
            anchors.fill: parent
            visible: gpuEnabled
            property vector2d canvasSize: Qt.vector2d(width, height)
            fragmentShader: "../shaders/ambient.frag.qsb"
        }
        Rectangle { anchors.fill: parent; visible: !gpuEnabled; color: "#151b24" }
        Rectangle { width: window.sideWidth; height: parent.height; color: "#70101318" }
        Rectangle { x: window.sideWidth; width: 1; height: parent.height; color: "#13ffffff" }
        ListView {
            id: messages
            visible: window.chatVisible
            objectName: "conversationList"
            x: window.columnX; y: 90
            width: window.columnWidth; height: window.height - y - 10
            clip: true
            spacing: 26
            topMargin: 18
            bottomMargin: composer.height + 76
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
                width: messages.width
                height: messageBody.height + (roleName === "user" ? 24 : 0)
                Rectangle {
                    visible: message.roleName === "user"
                    anchors.right: parent.right
                    width: Math.min(parent.width - 32, messageText.contentWidth + 32)
                    height: messageBody.height + 24
                    radius: 16
                    color: "#193d495a"
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
                        Text { text: "Forge"; color: "#dfe4eb"; font.weight: Font.DemiBold; font.pixelSize: 13; anchors.verticalCenter: parent.verticalCenter }
                        Text { visible: message.state === "streaming"; text: "正在思考"; color: "#939da9"; font.pixelSize: 12; anchors.verticalCenter: parent.verticalCenter }
                    }
                    TextEdit {
                        id: messageText
                        objectName: "messageBodyText"
                        width: parent.width
                        height: Math.max(20, contentHeight)
                        text: message.textContent || (message.state === "streaming" ? "…" : "")
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
                            if (message.roleName !== "user") controller.formatMessage(textDocument)
                        }
                        onTextChanged: Qt.callLater(polishTypography)
                        Component.onCompleted: Qt.callLater(polishTypography)
                    }
                    Row {
                        visible: message.roleName !== "user" && message.state !== "streaming"
                        spacing: 12
                        Text {
                            text: "复制"; color: copyHover.hovered ? "#e3e7eb" : "#8792a0"; font.pixelSize: 12
                            HoverHandler { id: copyHover; cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: controller.copyText(message.textContent) }
                        }
                        Text { text: message.state === "stopped" ? "已停止" : message.state === "error" ? "回复未完成" : ""; color: "#b9a68a"; font.pixelSize: 12 }
                        Text {
                            visible: message.state === "error" && !controller.busy
                            text: "重试"; color: "#c8d7e5"; font.pixelSize: 12
                            HoverHandler { cursorShape: Qt.PointingHandCursor }
                            TapHandler { onTapped: controller.retry() }
                        }
                    }
                }
            }
        }
        Column {
            visible: window.chatVisible && messages.count === 0
            x: window.columnX + 16
            y: Math.max(155, window.height * 0.27)
            width: window.columnWidth - 32
            spacing: 16
            Image { source: brandUrl; width: 42; height: 42; smooth: true }
            Text { text: "今天想完成什么？"; color: "#f0f1f3"; font.pixelSize: 30; font.weight: Font.DemiBold }
            Text { text: "描述目标，带上上下文，让想法成为下一步。"; color: "#9fa8b4"; font.pixelSize: 15 }
        }
    }
    ShaderEffectSource {
        id: sceneTexture
        sourceItem: scene
        textureSize: Qt.size(Math.ceil(window.width / 2), Math.ceil(window.height / 2))
        live: true; hideSource: false; visible: false
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
    ShaderEffectSource { id: horizontalTexture; sourceItem: horizontalBlur; live: true; visible: false; hideSource: true }
    ShaderEffect {
        id: verticalBlur
        width: horizontalBlur.width; height: horizontalBlur.height
        visible: false
        property var sourceTexture: horizontalTexture
        property vector2d direction: Qt.vector2d(0, 3 / window.height)
        fragmentShader: "../shaders/blur.frag.qsb"
    }
    ShaderEffectSource { id: blurTexture; sourceItem: verticalBlur; live: true; visible: false; hideSource: true }

    Row {
        x: 22; y: 23; spacing: 10
        Image { width: 27; height: 27; source: brandUrl; smooth: true }
        Text { text: "Forge"; color: "#f2f3f5"; font.pixelSize: 21; font.weight: Font.DemiBold; anchors.verticalCenter: parent.verticalCenter }
    }
    GlassButton {
        visible: !workbench.navigationOpen
        material: window.material; x: 18; y: 184; width: window.sideWidth - 36; height: 42
        glyph: "square-pen"; text: "新建对话"; enabled: !controller.busy
        onClicked: { workbench.navigate("chat"); controller.newSession() }
    }
    TextField {
        id: search
        visible: !workbench.navigationOpen
        x: 18; y: 238; width: window.sideWidth - 36; height: 36
        placeholderText: "搜索对话"; placeholderTextColor: "#7f8a99"
        color: "#dbe0e6"; leftPadding: 13; rightPadding: 10
        onTextChanged: window.sessionQuery = text.toLowerCase()
        background: GlassSurface { material: window.material; radius: 11; emphasis: search.activeFocus ? 0.6 : 0; tint: "#18263140" }
    }
    Text { visible: !workbench.navigationOpen; x: 24; y: 298; text: "最近对话"; color: "#7f8998"; font.pixelSize: 11 }
    ListView {
        id: history
        visible: !workbench.navigationOpen
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
        x: 18; y: window.height - 148; width: window.sideWidth - 36; spacing: 8
        GlassButton { objectName: "permissionButton"; material: window.material; width: parent.width; glyph: "shield"; text: workbench.permissionLabel; selected: true; tip: "切换执行权限"; enabled: !controller.busy && !workbench.busy && workbench.approvals.length === 0; onClicked: permissionPopup.open() }
        GlassButton { material: window.material; width: parent.width; glyph: "panels-top-left"; text: "Work 完整功能"; quiet: true; onClicked: controller.openWorkbench("chat") }
        GlassButton { material: window.material; width: parent.width; glyph: "settings-2"; text: "连接与外观"; quiet: true; onClicked: settings.open() }
    }
    Text {
        x: window.sideWidth + 28; y: 29; width: window.mainRight - x - 190
        text: window.chatVisible ? controller.title : workbench.pageTitle; elide: Text.ElideRight; color: "#d8dee6"; font.pixelSize: 14; font.weight: Font.Medium
    }
    Row {
        x: window.mainRight - width - 20; y: 20; spacing: 8
        GlassButton { visible: window.chatVisible; material: window.material; glyph: "pencil"; quiet: true; tip: "重命名对话"; onClicked: { renameField.text = controller.title; renamePopup.open() } }
        GlassButton { material: window.material; glyph: "panel-right"; text: "工作区"; quiet: true; onClicked: controller.openWorkbench("files") }
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
            Flickable {
                id: editorScroll
                width: parent.width
                height: Math.min(Math.max(60, editor.contentHeight + 10), Math.max(60, window.height * 0.19))
                contentWidth: width; contentHeight: editor.height
                clip: true; boundsBehavior: Flickable.StopAtBounds
                TextArea {
                    id: editor
                    objectName: "messageEditor"
                    width: editorScroll.width; height: Math.max(editorScroll.height, implicitHeight)
                    color: "#edf0f4"; placeholderTextColor: "#97a2b0"
                    placeholderText: "向 Forge 描述你的想法…"
                    font.pixelSize: 15
                    wrapMode: TextEdit.Wrap; selectByMouse: true
                    leftPadding: 0; rightPadding: 0; topPadding: 4; bottomPadding: 4
                    background: null
                    selectionColor: "#526477"; selectedTextColor: "white"
                    text: controller.draft
                    onTextChanged: if (controller.draft !== text) controller.draft = text
                    onCursorRectangleChanged: {
                        if (cursorRectangle.y < editorScroll.contentY) editorScroll.contentY = cursorRectangle.y
                        else if (cursorRectangle.y + cursorRectangle.height > editorScroll.contentY + editorScroll.height)
                            editorScroll.contentY = cursorRectangle.y + cursorRectangle.height - editorScroll.height
                    }
                    Keys.onReturnPressed: event => {
                        if (inputMethodComposing || (event.modifiers & Qt.ShiftModifier)) event.accepted = false
                        else { controller.send(); event.accepted = true }
                    }
                    Keys.onEnterPressed: event => {
                        if (inputMethodComposing || (event.modifiers & Qt.ShiftModifier)) event.accepted = false
                        else { controller.send(); event.accepted = true }
                    }
                }
            }
            RowLayout {
                width: parent.width; spacing: 8
                GlassButton { material: window.material; glyph: "paperclip"; tip: "添加文本附件"; enabled: !controller.busy; onClicked: files.open() }
                GlassButton {
                    id: modelButton
                    material: window.material; text: controller.model.length > (window.columnWidth < 550 ? 13 : 24) ? controller.model.slice(0, window.columnWidth < 550 ? 12 : 23) + "…" : controller.model
                    glyph: "box"; enabled: !controller.busy; tip: "切换模型"; onClicked: modelMenu.open()
                }
                GlassButton {
                    id: optionsButton
                    material: window.material; glyph: "sliders-horizontal"; text: window.columnWidth > 600 ? "选项" : ""
                    selected: options.opened; tip: "推理强度与玻璃外观"; onClicked: options.open()
                }
                Item { Layout.fillWidth: true }
                GlassButton {
                    objectName: "sendButton"
                    material: window.material; glyph: controller.busy ? "square" : "arrow-up"
                    width: 38; height: 38; cornerRadius: 19; selected: true
                    enabled: controller.busy || editor.text.trim().length > 0
                    tip: controller.busy ? "停止生成" : "发送消息"
                    onClicked: controller.busy ? controller.stop() : controller.send()
                }
            }
        }
    }
    Text { visible: window.chatVisible; x: composer.x + 4; y: window.height - 27; text: controller.status; color: "#7e8998"; font.pixelSize: 11 }
    Text { visible: window.chatVisible && window.columnWidth > 520; anchors.right: composer.right; y: window.height - 27; text: "Enter 发送  ·  Shift + Enter 换行"; color: "#7e8998"; font.pixelSize: 11 }
    Rectangle {
        visible: window.chatVisible && controller.error.length > 0
        x: composer.x; y: composer.y - height - 12; width: composer.width
        height: errorText.implicitHeight + 24; radius: 12; color: "#ed302d29"
        Text { id: errorText; x: 12; y: 12; width: parent.width - 24; text: controller.error; color: "#e4c49b"; wrapMode: Text.Wrap; font.pixelSize: 12 }
        TapHandler { onTapped: if (!controller.busy) settings.open() }
    }

    Popup {
        id: modelMenu
        focus: true
        parent: Overlay.overlay
        x: Math.min(composer.x + 52, window.width - width - 16)
        y: Math.max(16, composer.y - height - 10)
        width: Math.min(340, window.columnWidth); padding: 12
        height: Math.min(400, controller.models.length * 41 + 60)
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { material: window.material; radius: 20; emphasis: 0.3 }
        onClosed: editor.forceActiveFocus()
        contentItem: Column {
            spacing: 5
            Text { text: "模型"; color: "#9aa7b5"; font.pixelSize: 12; padding: 6 }
            ListView {
                id: modelList
                width: parent.width; height: modelMenu.availableHeight - 30
                model: controller.models; spacing: 5; clip: true
                ScrollBar.vertical: ScrollBar { width: 4 }
                delegate: GlassButton { required property string modelData; material: window.material; width: ListView.view.width; text: modelData.length > 33 ? modelData.slice(0, 32) + "…" : modelData; selected: controller.model === modelData; quiet: true; onClicked: {controller.setModel(modelData); modelMenu.close()} }
            }
        }
    }
    Popup {
        id: options
        focus: true
        objectName: "glassOptions"
        parent: Overlay.overlay
        x: Math.min(composer.x + 170, window.width - width - 20)
        y: Math.max(76, composer.y - height - 12)
        width: 348; padding: 20
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        background: GlassSurface { objectName: "optionsGlass"; material: window.material; radius: 22; emphasis: 0.4 }
        onClosed: editor.forceActiveFocus()
        contentItem: Column {
            spacing: 16
            Text { text: "对话选项"; color: "#eef1f4"; font.pixelSize: 16; font.weight: Font.DemiBold }
            Text { text: "推理强度"; color: "#a9b3bf"; font.pixelSize: 12 }
            Row {
                spacing: 7
                Repeater { model: [{key:"off",label:"关闭"},{key:"low",label:"轻量"},{key:"medium",label:"标准"},{key:"high",label:"深入"}]
                    GlassButton { required property var modelData; material: window.material; text: modelData.label; width: 70; selected: controller.reasoning === modelData.key; enabled: !controller.busy; onClicked: controller.setReasoning(modelData.key) }
                }
            }
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
        id: settings
        parent: Overlay.overlay
        anchors.centerIn: parent
        width: Math.min(440, window.width - 48); padding: 24
        modal: true; focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        Overlay.modal: Rectangle { color: "#6605080d" }
        background: GlassSurface { material: window.material; radius: 24; emphasis: 0.6 }
        onOpened: endpoint.text = controller.gateway
        onClosed: editor.forceActiveFocus()
        contentItem: Column {
            spacing: 16
            Text { text: "连接与外观"; color: "#f0f2f4"; font.pixelSize: 20; font.weight: Font.DemiBold }
            Text { text: "Forge 网关地址"; color: "#a7b3c0"; font.pixelSize: 13 }
            TextField { id: endpoint; width: parent.width; height: 42; color: "#e2e7ec"; selectByMouse: true; leftPadding: 12; background: GlassSurface { material: window.material; radius: 12; emphasis: endpoint.activeFocus ? 0.8 : 0 } }
            Text { width: parent.width; text: "沿用现有 Forge 网关。Coding、工具和连接器均在当前工作台内打开。"; color: "#939eac"; wrapMode: Text.Wrap; font.pixelSize: 12 }
            GlassButton { material: window.material; width: parent.width; text: "应用并连接"; glyph: "plug"; enabled: !controller.busy; onClicked: {controller.setGateway(endpoint.text); settings.close()} }
            GlassButton { material: window.material; width: parent.width; text: "调整玻璃外观"; glyph: "sliders-horizontal"; onClicked: {settings.close(); options.open()} }
            GlassButton { material: window.material; width: parent.width; text: "打开完整工作台"; quiet: true; onClicked: controller.openWorkbench("config") }
            Text { width: parent.width; text: gpuEnabled ? "GPU 渲染 · 原生文本输入" : "兼容渲染 · 玻璃光学效果不可用"; color: "#798797"; font.pixelSize: 11 }
        }
    }
    Popup {
        id: renamePopup
        parent: Overlay.overlay; anchors.centerIn: parent; width: 380; padding: 22; modal: true; focus: true
        background: GlassSurface { material: window.material; radius: 20 }
        onClosed: editor.forceActiveFocus()
        contentItem: Column { spacing: 16
            Text { text: "重命名对话"; color: "#edf0f3"; font.pixelSize: 17 }
            TextField { id: renameField; width: parent.width; color: "#edf0f3"; background: GlassSurface { material: window.material; radius: 10 } onAccepted: {controller.renameSession(text); renamePopup.close()} }
            GlassButton { material: window.material; text: "保存"; width: parent.width; onClicked: {controller.renameSession(renameField.text); renamePopup.close()} }
        }
    }
    FileDialog { id: files; title: "添加文本附件"; nameFilters: ["文本与代码 (*.txt *.md *.py *.js *.ts *.json *.qml *.css *.html)", "所有文件 (*)"]; onAccepted: controller.addAttachment(selectedFile) }
    Column {
        x: 18; y: 82; width: window.sideWidth - 36; spacing: 9
        Row {
            spacing: 8
            GlassButton { material: window.material; width: (parent.parent.width - 8) / 2; text: "Work"; glyph: "message-square"; selected: window.chatVisible; onClicked: workbench.navigate("chat") }
            GlassButton { material: window.material; width: (parent.parent.width - 8) / 2; text: "Coding"; glyph: "code"; selected: workbench.page === "development"; onClicked: workbench.navigate("development") }
        }
        Row {
            spacing: 8
            GlassButton { material: window.material; width: (parent.parent.width - 8) / 2; text: "功能"; glyph: "grid-2x2"; selected: workbench.navigationOpen; onClicked: workbench.toggleNavigation() }
            GlassButton { material: window.material; width: (parent.parent.width - 8) / 2; text: "浏览器"; glyph: "globe"; selected: workbench.browserVisible; onClicked: workbench.toggleBrowser() }
        }
    }
    Flickable {
        visible: workbench.navigationOpen
        x: 18; y: 184; width: window.sideWidth - 36; height: window.height - y - 164
        contentHeight: navigation.height; clip: true; boundsBehavior: Flickable.StopAtBounds
        ScrollBar.vertical: ScrollBar { width: 3 }
        Column {
            id: navigation; width: parent.width; spacing: 7
            Repeater {
                model: [
                    {key:"task", label:"任务", icon:"list-checks"},
                    {key:"files", label:"文件与项目", icon:"folder"},
                    {key:"tools", label:"工具与插件", icon:"blocks"},
                    {key:"config", label:"模型与配置", icon:"settings-2"},
                    {key:"agents", label:"Agents", icon:"users"},
                    {key:"knowledge", label:"知识库", icon:"book-open"},
                    {key:"evolution", label:"演化", icon:"sparkles"},
                    {key:"schedules", label:"定时任务", icon:"calendar-clock"},
                    {key:"connectors", label:"连接器", icon:"plug"},
                    {key:"computer", label:"电脑操作", icon:"monitor"},
                    {key:"preview", label:"代码预览", icon:"file-code"}
                ]
                GlassButton { required property var modelData; material: window.material; width: navigation.width; text: modelData.label; glyph: modelData.icon; quiet: true; selected: workbench.page === modelData.key; onClicked: workbench.navigate(modelData.key) }
            }
        }
    }
    Item {
        id: nativePage; objectName: "nativePage"
        x: window.sideWidth + 12; y: 76; width: window.mainRight - x - 12; height: window.height - y - 12
        visible: !window.chatVisible
        Rectangle { anchors.fill: parent; color: "#11151c"; radius: 16 }
        Rectangle {
            id: hostError; width: parent.width; height: hostErrorText.height + 24
            visible: workbench.nativeWindow !== null && workbench.error !== ""
            color: "#302d29"; radius: 10
            Text { id: hostErrorText; x: 12; y: 12; width: parent.width - 70; text: workbench.error; color: "#e4c49b"; wrapMode: Text.Wrap; font.pixelSize: 12 }
            GlassButton { material: window.material; glyph: "x"; x: parent.width - width - 8; y: 8; tip: "关闭提示"; onClicked: workbench.clearError() }
        }
        Column {
            visible: !workbench.nativeWindow; anchors.centerIn: parent; width: parent.width - 64; spacing: 16
            Text { width: parent.width; text: workbench.error || "正在载入 " + workbench.pageTitle + "…"; color: "#b9c4d0"; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter }
            GlassButton { material: window.material; text: "重新载入"; visible: workbench.error !== ""; anchors.horizontalCenter: parent.horizontalCenter; onClicked: workbench.start_host() }
        }
        WindowContainer {
            id: nativeContainer; objectName: "nativeContainer"
            anchors.fill: parent
            anchors.topMargin: hostError.visible ? hostError.height + 8 : 0
            visible: !window.chatVisible && workbench.nativeWindow !== null && workbench.approvals.length === 0 && !settings.opened && !renamePopup.opened && !exitPopup.opened
            window: workbench.nativeWindow
            onVisibleChanged: if (workbench.nativeWindow) workbench.nativeWindow.visible = visible
        }
    }
    Item {
        id: browserPanel; objectName: "browserPanel"
        x: window.mainRight; width: window.browserPanelWidth; height: parent.height
        visible: workbench.browserVisible
        Loader { anchors.fill: parent; active: workbench.browserLoaded; sourceComponent: Component { BrowserPane { material: window.material } } }
        MouseArea {
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
            Text { width: parent.width; text: "沙箱采用 Forge 工具与路径限制。权限不替代网站登录和连接器授权。"; wrapMode: Text.Wrap; color: "#7f8b9b"; font.pixelSize: 11 }
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
    Connections { target: workbench; function onPermissionRequested() { permissionPopup.open() } }
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
    Shortcut { sequence: "Ctrl+N"; enabled: window.chatVisible; onActivated: controller.newSession() }
    Shortcut { sequence: "Ctrl+K"; enabled: window.chatVisible; onActivated: {search.forceActiveFocus(); search.selectAll()} }
    Shortcut { sequence: "Escape"; enabled: controller.busy && !options.opened && !settings.opened && !modelMenu.opened; onActivated: controller.stop() }
    Connections {
        target: controller
        function onDraftChanged() { if (editor.text !== controller.draft) editor.text = controller.draft }
        function onFocusComposer() { editor.forceActiveFocus(); window.followTail = true; messages.positionViewAtEnd() }
        function onStreamAdvanced() { if (window.followTail) Qt.callLater(messages.positionViewAtEnd) }
    }
    Component.onCompleted: { editor.forceActiveFocus(); Qt.callLater(controller.refreshConnection) }
}
