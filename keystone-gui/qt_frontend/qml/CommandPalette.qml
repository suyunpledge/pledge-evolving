import QtQuick
import QtQuick.Controls

// 命令面板（Ctrl+K，沿用 Tk 版）：命令、页面、模型、思考强度、角色与最近对话的统一入口。
// items: [{label, group, hint, keywords, icon, run: function}]，打开时由 Main 现取。
Popup {
    id: paletteRoot
    objectName: "commandPalette"
    required property var material
    property var items: []
    property string query: ""
    readonly property var matches: {
        const q = query.trim().toLowerCase()
        const words = q.split(/\s+/).filter(w => w.length > 0)
        return items.filter(item => {
            const hay = (item.label + " " + (item.group || "") + " " + (item.keywords || "") + " " + (item.hint || "")).toLowerCase()
            return words.every(w => hay.indexOf(w) >= 0)
        }).slice(0, 60)
    }
    property int current: 0
    modal: true; focus: true; padding: 12
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    Overlay.modal: Rectangle { color: "#5505080d" }
    background: GlassSurface { material: paletteRoot.material; radius: 20; emphasis: 0.6 }
    onOpened: { query = ""; field.text = ""; current = 0; field.forceActiveFocus() }
    onQueryChanged: current = 0

    function runCurrent() {
        if (current < 0 || current >= matches.length) return
        const item = matches[current]
        close()
        Qt.callLater(item.run)
    }

    contentItem: Column {
        spacing: 8
        TextField {
            id: field
            objectName: "paletteField"
            width: parent.width; height: 42
            placeholderText: "搜索命令、页面、模型、角色或对话…"
            placeholderTextColor: Theme.textMuted; color: Theme.textPrimary
            leftPadding: 38; selectByMouse: true
            background: GlassSurface { material: paletteRoot.material; radius: 12; tint: "#40212d38"; emphasis: 0.5 }
            Glyph { x: 12; anchors.verticalCenter: parent.verticalCenter; name: "search"; ink: Theme.accent; width: 16; height: 16 }
            onTextChanged: paletteRoot.query = text
            Keys.onDownPressed: paletteRoot.current = Math.min(paletteRoot.matches.length - 1, paletteRoot.current + 1)
            Keys.onUpPressed: paletteRoot.current = Math.max(0, paletteRoot.current - 1)
            Keys.onReturnPressed: paletteRoot.runCurrent()
            Keys.onEnterPressed: paletteRoot.runCurrent()
        }
        Text {
            visible: paletteRoot.matches.length === 0
            text: "没有匹配项"; color: Theme.textMuted; font.pixelSize: 12; leftPadding: 10
        }
        ListView {
            id: list
            objectName: "paletteList"
            width: parent.width; height: paletteRoot.availableHeight - field.height - 8
            model: paletteRoot.matches; clip: true; spacing: 2
            currentIndex: paletteRoot.current
            onCurrentIndexChanged: positionViewAtIndex(currentIndex, ListView.Contain)
            ScrollBar.vertical: ScrollBar { width: 4 }
            delegate: AbstractButton {
                id: entry
                required property var modelData
                required property int index
                width: list.width; height: 40; hoverEnabled: true
                // The Enter target must be obvious: selected tint plus an accent bar.
                background: Rectangle {
                    radius: 10
                    color: entry.index === paletteRoot.current ? Theme.selectedTint : entry.hovered ? "#14ffffff" : "transparent"
                    Rectangle {
                        visible: entry.index === paletteRoot.current
                        x: 0; y: 9; width: 3; height: parent.height - 18; radius: 2
                        color: Theme.accent
                    }
                }
                contentItem: Row {
                    spacing: 10; leftPadding: 10
                    Glyph { name: entry.modelData.icon || "sparkles"; ink: Theme.textSecondary; width: 16; height: 16; anchors.verticalCenter: parent.verticalCenter }
                    Text {
                        width: list.width - 190; elide: Text.ElideRight
                        text: entry.modelData.label; color: Theme.textPrimary; font.pixelSize: 13
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        width: 130; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight
                        text: entry.modelData.hint || entry.modelData.group || ""
                        color: Theme.textMuted; font.pixelSize: 11; anchors.verticalCenter: parent.verticalCenter
                    }
                }
                onHoveredChanged: if (hovered) paletteRoot.current = index
                onClicked: { paletteRoot.current = index; paletteRoot.runCurrent() }
            }
        }
    }
}
