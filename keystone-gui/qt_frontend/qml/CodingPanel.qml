import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Item {
    id: pane
    objectName: "codingGlassPanel"
    required property var material
    readonly property var stateData: workbench.coding
    property string tab: "chat"
    property bool followTail: true
    signal permissionRequested()
    signal plannerKeyRequested()
    readonly property var planningModes: [
        {value: "none", label: "直接回答"}, {value: "low", label: "简要规划"},
        {value: "medium", label: "标准规划"}, {value: "high", label: "深入规划"}
    ]
    readonly property var modelRoles: [
        {value:"default", label:"默认编程"}, {value:"simple", label:"简单任务"},
        {value:"complex", label:"复杂任务"}, {value:"review", label:"代码复审"},
        {value:"subagent", label:"子 Agent 默认模型"}
    ]
    function run(action, value = "") { workbench.codingAction(action, value) }
    function focusComposer() {
        if (visible) {
            composer.editor.forceActiveFocus()
            composer.refreshInputMethod()
        }
    }
    onVisibleChanged: if (visible) Qt.callLater(focusComposer)
    Connections {
        target: workbench
        function onCodingDraftChanged() {
            if (composer.text !== workbench.codingDraft)
                composer.text = workbench.codingDraft
        }
        function onCodingFocusRequested() { pane.focusComposer() }
    }

    GlassSurface { anchors.fill: parent; material: pane.material; radius: 24; tint: "#1a263441" }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 13; spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Glyph { name: "bot"; Layout.preferredWidth: 19; Layout.preferredHeight: 19 }
            Text { text: "Coding Agent"; color: "#f1f3f7"; font.pixelSize: 16; font.weight: Font.DemiBold }
            Item { Layout.fillWidth: true }
            GlassButton { material: pane.material; glyph: "square-pen"; tip: "新对话"; quiet: true; enabled: !pane.stateData.busy; onClicked: { pane.tab = "chat"; pane.run("new") } }
            GlassButton { material: pane.material; glyph: "clock-3"; tip: "历史对话"; quiet: true; enabled: !pane.stateData.busy; onClicked: pane.run("history") }
            GlassButton { material: pane.material; glyph: "sliders-horizontal"; tip: "Coding 选项"; selected: choices.opened; onClicked: choices.showChoices("settings") }
        }
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: pane.stateData.project || "尚未打开项目"; color: "#a5b0bf"; font.pixelSize: 12; elide: Text.ElideMiddle }
            GlassButton {
                material: pane.material; glyph: "shield"; text: workbench.permissionLabel
                height: 30; Layout.preferredHeight: 30; selected: true
                enabled: !workbench.busy && !controller.busy && workbench.approvals.length === 0
                onClicked: pane.permissionRequested()
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 5
            Repeater {
                model: [{key:"chat", label:"对话"}, {key:"tasks", label:"任务"},
                        {key:"agents", label:"子 Agent"}, {key:"events", label:"事件"}]
                GlassButton {
                    required property var modelData
                    material: pane.material; text: modelData.label
                    Layout.fillWidth: true; Layout.preferredHeight: 32
                    selected: pane.tab === modelData.key; quiet: !selected
                    onClicked: pane.tab = modelData.key
                }
            }
        }
        Item {
            Layout.fillHeight: true; Layout.fillWidth: true; Layout.minimumHeight: 80
            ListView {
                id: transcript
                anchors.fill: parent; visible: pane.tab === "chat"
                model: workbench.codingMessages; clip: true; spacing: 12
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar { width: 4 }
                onMovementStarted: pane.followTail = false
                onMovementEnded: pane.followTail = atYEnd
                onContentHeightChanged: if (pane.followTail) Qt.callLater(positionViewAtEnd)
                delegate: Item {
                    required property string roleName
                    required property string textContent
                    required property string stamp
                    readonly property bool fromUser: roleName === "你" || roleName === "user"
                    width: transcript.width; height: bubble.height + 3
                    Item {
                        id: bubble
                        x: fromUser ? 24 : 1; width: parent.width - 25
                        height: messageContent.height + 22
                        GlassSurface { anchors.fill: parent; material: pane.material; radius: 14; emphasis: fromUser ? 0.48 : 0; tint: "#20334356" }
                        Column {
                            id: messageContent
                            x: 12; y: 11; width: parent.width - 24; spacing: 6
                            RowLayout {
                                width: parent.width
                                Text { text: fromUser ? "你" : roleName === "assistant" ? "Coding Agent" : roleName; color: "#e2e9f0"; font.pixelSize: 12; font.weight: Font.DemiBold }
                                Item { Layout.fillWidth: true }
                                Text { text: stamp; color: "#8593a5"; font.pixelSize: 11 }
                            }
                            TextEdit {
                                width: parent.width; height: contentHeight
                                text: textContent; textFormat: fromUser ? TextEdit.PlainText : TextEdit.MarkdownText
                                font.pixelSize: 14; color: "#dce3ec"
                                readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                                onLinkActivated: link => {
                                    if (link.startsWith("https://") || link.startsWith("http://")) workbench.openBrowser(link)
                                    else pane.run("openFile", link)
                                }
                            }
                        }
                    }
                }
                footer: Column {
                    width: transcript.width; spacing: 10
                    TodoList { width: parent.width; items: pane.stateData.todos || []; running: !!pane.stateData.taskRunning }
                    Repeater {
                        model: pane.stateData.changes || []
                        Item {
                            required property var modelData
                            width: parent.width - 2; height: changeContent.height + 20
                            GlassSurface { anchors.fill: parent; material: pane.material; radius: 14; tint: "#25314556" }
                            Column {
                                id: changeContent; x: 10; y: 10; width: parent.width - 20; spacing: 6
                                Text { text: modelData.restored ? "已回退本次修改" : modelData.count ? "已修改 " + modelData.count + " 个文件" : "工作区差异"; color: "#e1e8ef"; font.pixelSize: 13 }
                                Text { width: parent.width; text: modelData.files.join(" · "); elide: Text.ElideRight; color: "#a4b1bf"; font.pixelSize: 12; visible: !!text }
                                Flow {
                                    width: parent.width; spacing: 5
                                    GlassButton { material: pane.material; glyph: "code"; text: "查看修改"; enabled: !pane.stateData.busy; onClicked: pane.run("diff", modelData.id) }
                                    GlassButton { material: pane.material; glyph: "undo-2"; text: "回退"; visible: modelData.canRevert; enabled: !pane.stateData.busy; onClicked: pane.run("revert", modelData.id) }
                                }
                            }
                        }
                    }
                }
            }
            Flickable {
                anchors.fill: parent; clip: true
                visible: pane.tab === "chat" && transcript.count === 0 && !(pane.stateData.changes || []).length
                contentWidth: width; contentHeight: welcome.height + 24
                Column {
                    id: welcome; x: 10; y: 12; width: parent.width - 20; spacing: 10
                    Text { width: parent.width; text: pane.stateData.ready ? "开始一个任务" : "正在载入 Coding…"; color: "#edf1f6"; font.pixelSize: 20; font.weight: Font.DemiBold }
                    Text { width: parent.width; text: pane.stateData.locked || "描述要完成的工作，或从下方选一个任务模板。"; color: "#a3b0bf"; font.pixelSize: 13; wrapMode: Text.Wrap }
                    Repeater {
                        model: [{label:"解释项目结构", icon:"book-open"}, {label:"找出并修复测试", icon:"clipboard-check"}, {label:"添加一个功能", icon:"plus"}]
                        GlassButton {
                            required property var modelData; required property int index
                            material: pane.material; width: parent.width
                            text: modelData.label; glyph: modelData.icon; enabled: !!pane.stateData.ready && !pane.stateData.locked
                            onClicked: pane.run("template", String(index))
                        }
                    }
                }
            }
            ScrollView {
                id: details; anchors.fill: parent; visible: pane.tab !== "chat"; clip: true
                contentWidth: availableWidth
                Column {
                    width: details.availableWidth
                    TodoList { width: parent.width; items: pane.tab === "tasks" ? (pane.stateData.todos || []) : []; running: !!pane.stateData.taskRunning }
                    TextArea {
                        width: parent.width
                        text: pane.tab === "tasks" ? pane.stateData.tasks || "暂无任务"
                            : pane.tab === "agents" ? (pane.stateData.agents || []).map(a => a.name + "  ·  " + a.status + "\n" + a.output).join("\n\n") || "暂无子 Agent"
                            : pane.stateData.events || "暂无事件"
                        color: "#cbd5e1"; font.pixelSize: 13; padding: 10
                        readOnly: true; selectByMouse: true; wrapMode: TextEdit.Wrap
                        background: null
                    }
                }
            }
        }
        Item {
            id: composerCard
            Layout.fillWidth: true; Layout.preferredHeight: composerBody.height + 20
            GlassSurface { anchors.fill: parent; material: pane.material; radius: 18; emphasis: composer.editor.activeFocus ? 0.45 : 0; tint: "#143e4654" }
            Column {
                id: composerBody; x: 10; y: 10; width: parent.width - 20; spacing: 8
                Flow {
                    width: parent.width; spacing: 5; visible: (pane.stateData.contexts || []).length > 0
                    Repeater {
                        model: pane.stateData.contexts || []
                        GlassButton { required property string modelData; required property int index; material: pane.material; glyph: "x"; text: modelData.length > 20 ? modelData.slice(0, 17) + "…" : modelData; onClicked: pane.run("removeContext", String(index)) }
                    }
                }
                ComposerText {
                    id: composer
                    width: parent.width; height: Math.min(104, Math.max(54, editor.contentHeight + 8))
                    editorName: "codingMessageEditor"; placeholder: "输入任务描述…"
                    text: workbench.codingDraft
                    onTextChanged: workbench.setCodingDraft(text)
                    onSubmit: if (text.trim() && pane.stateData.ready && !pane.stateData.loading) { pane.tab = "chat"; pane.run("send") }
                }
                RowLayout {
                    width: parent.width; spacing: 5
                    GlassButton { material: pane.material; glyph: "paperclip"; tip: "附加当前编辑器"; enabled: !!pane.stateData.hasDocument; onClicked: pane.run("attach") }
                    GlassButton { material: pane.material; glyph: "box"; text: pane.width > 340 ? (pane.stateData.model || "继承配置").slice(0, 11) : ""; tip: "切换 Coding 模型"; onClicked: choices.showChoices("model") }
                    GlassButton { material: pane.material; glyph: "sliders-horizontal"; tip: "事前规划"; text: pane.width > 390 ? (pane.stateData.planningPolicy === "auto" ? "自动规划" : (pane.planningModes.find(m => m.value === pane.stateData.planning) || pane.planningModes[0]).label) : ""; onClicked: choices.showChoices("planning") }
                    Item { Layout.fillWidth: true }
                    GlassButton { objectName: "continueCoding"; material: pane.material; text: "继续"; tip: "继续已停止的任务"
                        visible: !!pane.stateData.canResume && !pane.stateData.busy; onClicked: pane.run("resume") }
                    GlassButton { material: pane.material; glyph: "square"; tip: "停止任务并清除队列"; visible: !!pane.stateData.busy || !!pane.stateData.queue; onClicked: pane.run("cancel") }
                    GlassButton {
                        material: pane.material; glyph: "arrow-up"; primary: true
                        cornerRadius: 19; enabled: !!pane.stateData.ready && !pane.stateData.loading && composer.text.trim().length > 0 && !composer.editor.inputMethodComposing
                        tip: pane.stateData.busy ? "排队发送" : "发送任务"
                        onClicked: composer.submit()
                    }
                }
            }
        }
        Text {
            Layout.fillWidth: true; elide: Text.ElideRight; color: "#9caaba"; font.pixelSize: 11
            text: (pane.stateData.queue ? "排队 " + pane.stateData.queue + "/3 · " : "") + (pane.stateData.status || "Enter 发送 · Shift+Enter 换行")
        }
    }

    Popup {
        id: choices; objectName: "codingOptions"
        parent: Overlay.overlay
        x: pane.x + (pane.width - width) / 2
        y: pane.y + Math.max(8, (pane.height - height) / 2)
        width: Math.min(420, pane.width - 16); padding: 16
        height: Math.min(pane.height - 16, optionList.contentHeight + 32)
        modal: true; focus: true
        property string kind: "settings"
        property string roleModel: "default"
        function showChoices(value) { controller.planning.refresh(); kind = value; open() }
        onClosed: pane.focusComposer()
        background: GlassSurface { material: pane.material; radius: 22; tint: "#ed18212b" }
        contentItem: ListView {
            id: optionList
            spacing: 5; clip: true
            ScrollBar.vertical: ScrollBar { width: 4 }
            model: choices.kind === "planning" ? [
                    {value:"auto", label:"自动判断", action:"planningPolicy", selected:pane.stateData.planningPolicy === "auto"},
                    {value:"manual", label:"手动开启", action:"planningPolicy", selected:pane.stateData.planningPolicy === "manual"},
                    ...(pane.stateData.planningPolicy === "manual" ? pane.planningModes.map(m => ({value:m.value, label:"下个任务 · " + m.label, action:"planning", selected:pane.stateData.planning === m.value})) : [])]
                : choices.kind === "model" ? (pane.stateData.models || []).map(m => ({value: m, label: m, action: "model", selected: pane.stateData.model === m}))
                : choices.kind === "planner" ? controller.planning.options.map(m => ({
                      value: m.value, label: m.label, action: "planner", selected: controller.planning.model === m.value}))
                : choices.kind === "role" ? [
                    {value:"default", label:"默认"}, {value:"simple", label:"简单任务"},
                    {value:"complex", label:"复杂任务"}, {value:"review", label:"代码复审"}
                ].map(m => ({value: m.value, label: m.label, action:"role", selected: pane.stateData.role === m.value}))
                : choices.kind === "roles" ? pane.modelRoles.map(m => ({
                    label: m.label + " · " + ((pane.stateData.roleModels || {})[m.value] || "继承配置"),
                    submenu: "roleModel", role: m.value}))
                : choices.kind === "roleModel" ? (pane.stateData.models || []).map(m => ({
                    label: m, value: choices.roleModel + "\n" + m, action: "roleModel",
                    selected: (pane.stateData.roleModels || {})[choices.roleModel] === m}))
                : [{label:"事前规划", submenu:"planning"}, {label:"执行模型", submenu:"model"}, {label:"规划模型", submenu:"planner"},
                   ...(controller.planning.endpoint && !controller.isDemo ? [{label:"独立规划密钥", action:"plannerKey"}] : []), {label:"任务角色", submenu:"role"},
                   {label:pane.stateData.simple ? "切换到专业视图" : "切换到简洁视图", action:"density"},
                   {label:"复审", action:"review"}, {label:"运行测试", action:"tests"}, {label:"重试任务", action:"retry"},
                   {label:"创建子 Agent", action:"delegate"}, {label:"模型角色设置", submenu:"roles"}, {label:"审批记录", action:"approvals"}]
            delegate: GlassButton {
                required property var modelData
                material: pane.material; width: ListView.view.width; height: 37
                text: modelData.label.length > 32 ? modelData.label.slice(0, 29) + "…" : modelData.label
                selected: !!modelData.selected
                glyph: modelData.submenu ? "chevron-right" : modelData.selected ? "check" : ""
                onClicked: {
                    if (modelData.submenu) {
                        if (modelData.role) choices.roleModel = modelData.role
                        choices.kind = modelData.submenu
                    }
                    else if (modelData.action === "planner") { controller.planning.save("model", modelData.value); choices.close() }
                    else if (modelData.action === "planningPolicy") {
                        pane.run("planningPolicy", modelData.value)
                        if (modelData.value === "auto") choices.close()
                    }
                    else if (modelData.action === "plannerKey") { choices.close(); pane.plannerKeyRequested() }
                    else { pane.run(modelData.action, modelData.value || ""); choices.close() }
                }
            }
        }
    }
}
