import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 角色模板：选择 / 切换 / 复制并自定义。数据与安全规则在 keystone.agent_templates。
Popup {
    id: panel
    objectName: "rolePicker"
    required property var material
    required property var service
    required property bool changesAllowed
    property string editing: ""
    readonly property var editRow: service.rows.find(row => row.id === editing) || null
    // 位置与尺寸由使用方给出（Overlay 内 anchors.centerIn 会算偏，同 WallpaperSettings）。
    padding: 20
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    Overlay.modal: Rectangle { color: "#7705080d" }
    background: GlassSurface { material: panel.material; radius: 24; tint: "#e11c2430" }
    onOpened: { service.refresh(); editing = "" }

    function beginEdit(id) {
        editing = id
        const row = editRow
        if (!row) return
        nameField.text = row.name
        descriptionField.text = row.description
        guidanceField.text = row.guidance
        startersField.text = row.starters.join("\n")
    }

    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: panel.editing ? "自定义角色" : "选择角色"; color: "#eef2f7"; font.pixelSize: 20; font.weight: Font.DemiBold }
            GlassButton { visible: panel.editing !== ""; material: panel.material; glyph: "chevron-left"; text: "返回"; quiet: true; onClicked: panel.editing = "" }
            GlassButton { material: panel.material; glyph: "x"; tip: "关闭"; onClicked: panel.close() }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#9fabb9"; font.pixelSize: 12
            text: panel.editing ? "修改只影响你的副本，保存在 Keystone 主目录的 agents 文件夹。权限和工具范围沿用来源模板，且不会超过你在左下角设置的执行权限。"
                                : "角色会预设指引、可用工具、权限上限与步数预算。权限只会在你的执行权限之内进一步收窄。"
        }
        Text {
            Layout.fillWidth: true; visible: panel.service.error.length > 0; wrapMode: Text.Wrap
            text: panel.service.error; color: Theme.warning; font.pixelSize: 12
        }

        // -- list -------------------------------------------------------------
        ListView {
            id: roleList
            objectName: "roleList"
            visible: panel.editing === ""
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: 8
            ScrollBar.vertical: ScrollBar { width: 4 }
            model: [{id: "", name: "通用助手", icon: "bot", description: "不使用角色模板，按你的权限与工具设置工作。",
                     builtin: true, permission: "沿用执行权限", maxSteps: 0, active: panel.service.activeId === ""}].concat(panel.service.rows)
            delegate: Item {
                id: entry
                required property var modelData
                readonly property bool custom: modelData.id !== "" && !modelData.builtin
                width: roleList.width; height: 72
                Rectangle {
                    anchors.fill: parent; radius: 16
                    color: entry.modelData.active ? "#2a3f5a78" : entryHover.hovered ? "#1affffff" : "#0dffffff"
                    border.width: 1; border.color: entry.modelData.active ? Theme.accent : "#1effffff"
                }
                HoverHandler { id: entryHover; cursorShape: Qt.PointingHandCursor }
                TapHandler { enabled: panel.changesAllowed; onTapped: { panel.service.select(entry.modelData.id); panel.close() } }
                Glyph {
                    x: 18; anchors.verticalCenter: parent.verticalCenter; width: 22; height: 22
                    name: entry.modelData.icon; ink: entry.modelData.active ? Theme.accent : Theme.textSecondary
                }
                Column {
                    x: 56; anchors.verticalCenter: parent.verticalCenter; spacing: 5
                    width: actions.x - x - 10
                    Row {
                        spacing: 8
                        Text { text: entry.modelData.name; color: "#eef2f6"; font.pixelSize: 14; font.weight: Font.Medium }
                        Text { visible: entry.custom; text: "我的副本"; color: Theme.accent; font.pixelSize: 11; anchors.baseline: parent.children[0].baseline }
                        Text {
                            text: entry.modelData.permission + (entry.modelData.maxSteps ? " · " + entry.modelData.maxSteps + " 步" : "")
                            color: "#8794a3"; font.pixelSize: 11; anchors.baseline: parent.children[0].baseline
                        }
                    }
                    Text { width: parent.width; text: entry.modelData.description; color: "#a8b3c0"; font.pixelSize: 12; elide: Text.ElideRight }
                }
                Row {
                    id: actions
                    anchors.right: parent.right; anchors.rightMargin: 12; anchors.verticalCenter: parent.verticalCenter
                    spacing: 4
                    GlassButton {
                        visible: entry.modelData.id !== ""; material: panel.material; glyph: "copy"; text: "复制"; quiet: true
                        tip: "复制并自定义"; enabled: panel.changesAllowed
                        onClicked: { const id = panel.service.duplicate(entry.modelData.id); if (id) panel.beginEdit(id) }
                    }
                    GlassButton {
                        visible: entry.custom; material: panel.material; glyph: "pencil"; quiet: true
                        tip: "编辑副本"; enabled: panel.changesAllowed; onClicked: panel.beginEdit(entry.modelData.id)
                    }
                    GlassButton {
                        visible: entry.custom; material: panel.material; glyph: "x"; quiet: true
                        tip: "删除副本"; enabled: panel.changesAllowed; onClicked: panel.service.remove(entry.modelData.id)
                    }
                }
            }
        }

        // -- editor -----------------------------------------------------------
        ColumnLayout {
            visible: panel.editing !== ""
            Layout.fillWidth: true; Layout.fillHeight: true; spacing: 8
            Text { text: "名称"; color: "#a9b3bf"; font.pixelSize: 12 }
            TextField {
                id: nameField; objectName: "roleNameField"
                Layout.fillWidth: true; Layout.preferredHeight: 38; maximumLength: 24
                color: "#edf1f5"; leftPadding: 12; selectByMouse: true
                background: GlassSurface { material: panel.material; radius: 10; emphasis: nameField.activeFocus ? 0.65 : 0 }
            }
            Text { text: "一句话介绍"; color: "#a9b3bf"; font.pixelSize: 12 }
            TextField {
                id: descriptionField
                Layout.fillWidth: true; Layout.preferredHeight: 38; maximumLength: 120
                color: "#edf1f5"; leftPadding: 12; selectByMouse: true
                background: GlassSurface { material: panel.material; radius: 10; emphasis: descriptionField.activeFocus ? 0.65 : 0 }
            }
            Text { text: "角色指引"; color: "#a9b3bf"; font.pixelSize: 12 }
            ScrollView {
                Layout.fillWidth: true; Layout.fillHeight: true; clip: true
                background: GlassSurface { material: panel.material; radius: 10 }
                TextArea {
                    id: guidanceField; objectName: "roleGuidanceField"
                    wrapMode: TextEdit.Wrap; selectByMouse: true; background: null
                    color: "#e3eaf1"; font.pixelSize: 13; padding: 10
                }
            }
            Text { text: "起手提示（每行一条，最多 8 条）"; color: "#a9b3bf"; font.pixelSize: 12 }
            ScrollView {
                Layout.fillWidth: true; Layout.preferredHeight: 92; clip: true
                background: GlassSurface { material: panel.material; radius: 10 }
                TextArea {
                    id: startersField
                    wrapMode: TextEdit.Wrap; selectByMouse: true; background: null
                    color: "#e3eaf1"; font.pixelSize: 13; padding: 10
                }
            }
            RowLayout {
                Layout.alignment: Qt.AlignRight
                GlassButton { material: panel.material; text: "取消"; quiet: true; onClicked: panel.editing = "" }
                GlassButton {
                    objectName: "roleSave"
                    material: panel.material; glyph: "save"; text: "保存并使用"; primary: true
                    enabled: panel.changesAllowed && nameField.text.trim().length > 0 && guidanceField.text.trim().length > 0
                    onClicked: {
                        if (panel.service.save(panel.editing, nameField.text, descriptionField.text, guidanceField.text, startersField.text)) {
                            panel.service.select(panel.editing)
                            panel.editing = ""
                        }
                    }
                }
            }
        }
    }
}
