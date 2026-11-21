import QtQuick
import QtQuick.Controls
import QtQuick.Dialogs
import QtQuick.Layouts

Popup {
    id: settings
    objectName: "wallpaperSettings"
    required property var material
    required property var wallpaper
    required property bool reduced
    readonly property bool animated: wallpaper.kind === "gif" || wallpaper.kind === "video"
    padding: 22; modal: true; focus: true
    Overlay.modal: Rectangle { color: "#6605080d" }
    background: GlassSurface { material: settings.material; radius: 22; tint: "#ef19232d" }

    FileDialog {
        id: picker
        property bool dynamicFile: false
        title: dynamicFile ? "导入动态壁纸" : "导入静态壁纸"
        nameFilters: dynamicFile ? ["动态壁纸 (*.gif *.mp4 *.webm *.mov *.m4v)"]
                                 : ["静态壁纸 (*.png *.jpg *.jpeg *.webp *.bmp)"]
        onAccepted: settings.wallpaper.importFile(selectedFile, dynamicFile)
    }

    component Tile: AbstractButton {
        id: tile
        property string itemId: ""
        property string label: ""
        property string kind: ""
        property url preview: ""
        property bool current: false
        property bool confirming: false
        implicitHeight: 112
        hoverEnabled: true
        focusPolicy: Qt.StrongFocus
        enabled: !settings.wallpaper.busy
        Accessible.name: label + (current ? "（当前）" : "")
        onClicked: settings.wallpaper.select(itemId)
        onHoveredChanged: if (!hovered) confirming = false
        contentItem: Item {
            Rectangle {
                id: frame
                anchors { left: parent.left; right: parent.right; top: parent.top }
                height: 78; radius: 10; clip: true
                color: "#182230"
                border.width: tile.current ? 2 : tile.visualFocus || tile.hovered ? 1 : 0
                border.color: tile.current ? Theme.accent : "#55ffffff"
                // Built-in background: a small aurora swatch instead of a file preview.
                gradient: tile.itemId ? null : aurora
                Gradient {
                    id: aurora
                    orientation: Gradient.Horizontal
                    GradientStop { position: 0; color: "#16233a" }
                    GradientStop { position: 0.55; color: "#24406a" }
                    GradientStop { position: 1; color: "#3a2b46" }
                }
                Image {
                    anchors { fill: parent; margins: frame.border.width }
                    visible: !!tile.itemId && status === Image.Ready
                    source: tile.preview
                    sourceSize: Qt.size(320, 200)
                    asynchronous: true
                    fillMode: Image.PreserveAspectCrop
                }
                Glyph {
                    anchors.centerIn: parent
                    visible: !!tile.itemId && !tile.preview.toString()
                    name: tile.kind === "video" ? "play" : "image"; ink: Theme.textMuted
                }
                Rectangle {
                    visible: tile.kind === "gif" || tile.kind === "video"
                    anchors { left: parent.left; bottom: parent.bottom; margins: 6 }
                    width: badge.implicitWidth + 10; height: 18; radius: 6; color: "#b3080c12"
                    Text { id: badge; anchors.centerIn: parent; text: tile.kind === "gif" ? "GIF" : "视频"; color: "#e6edf5"; font.pixelSize: 10 }
                }
                Rectangle {
                    visible: tile.current
                    anchors { right: parent.right; top: parent.top; margins: 6 }
                    width: 20; height: 20; radius: 10; color: Theme.accent
                    Glyph { anchors.centerIn: parent; width: 12; height: 12; name: "check"; ink: Theme.accentInk }
                }
                AbstractButton {
                    id: remove; objectName: "removeWallpaper"
                    visible: !!tile.itemId && (tile.hovered || tile.confirming || activeFocus)
                    anchors { right: parent.right; bottom: parent.bottom; margins: 5 }
                    height: 22; width: tile.confirming ? confirmText.implicitWidth + 14 : 22
                    hoverEnabled: true
                    Accessible.name: tile.confirming ? "确认删除 " + tile.label : "删除 " + tile.label
                    ToolTip.visible: hovered && !tile.confirming; ToolTip.text: "删除此壁纸"; ToolTip.delay: 500
                    background: Rectangle { radius: 7; color: tile.confirming ? "#d8b03a3a" : remove.hovered ? "#d0202a36" : "#a0080c12" }
                    contentItem: Item {
                        Glyph { anchors.centerIn: parent; width: 12; height: 12; visible: !tile.confirming; name: "x"; ink: "#e6edf5" }
                        Text { id: confirmText; anchors.centerIn: parent; visible: tile.confirming; text: "删除？"; color: "#fff3f1"; font.pixelSize: 11 }
                    }
                    onClicked: {
                        if (tile.confirming) settings.wallpaper.remove(tile.itemId)
                        else tile.confirming = true
                    }
                }
            }
            Text {
                anchors { left: parent.left; right: parent.right; top: frame.bottom; topMargin: 6 }
                text: tile.label; elide: Text.ElideMiddle
                color: tile.current ? Theme.textPrimary : Theme.textSecondary; font.pixelSize: 12
            }
        }
        background: Item {}
    }

    contentItem: ColumnLayout {
        spacing: 14
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: "Agent 壁纸"; color: "#eef2f6"; font.pixelSize: 20; font.weight: Font.DemiBold }
            GlassButton { material: settings.material; glyph: "x"; tip: "关闭壁纸设置"; onClicked: settings.close() }
        }
        Text {
            Layout.fillWidth: true
            text: settings.wallpaper.busy ? "正在导入并生成预览…" : "当前：" + (settings.wallpaper.name || "Keystone 默认背景")
            color: "#bcc8d5"; elide: Text.ElideMiddle; font.pixelSize: 13
        }
        GridView {
            id: grid; objectName: "wallpaperGrid"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(Math.ceil(count / 3) * cellHeight, 2 * cellHeight + 8)
            cellWidth: Math.floor(width / 3); cellHeight: 124
            clip: true; boundsBehavior: Flickable.StopAtBounds
            ScrollBar.vertical: ScrollBar { width: 5; policy: ScrollBar.AsNeeded }
            model: [{ id: "", name: "默认背景", kind: "", thumb: "", source: "" }].concat(settings.wallpaper.items)
            delegate: Tile {
                required property var modelData
                width: grid.cellWidth - 10
                itemId: modelData.id; label: modelData.name; kind: modelData.kind
                // Thumbnails are generated on import; still images fall back to the file itself.
                preview: modelData.thumb || (modelData.kind === "image" ? modelData.source : "")
                current: settings.wallpaper.currentId === modelData.id
            }
        }
        RowLayout {
            Layout.fillWidth: true
            GlassButton {
                material: settings.material; glyph: "image"; text: "导入静态壁纸"
                enabled: !settings.wallpaper.busy
                onClicked: { picker.dynamicFile = false; picker.open() }
            }
            GlassButton {
                material: settings.material; glyph: "play"; text: "导入动态壁纸"
                enabled: !settings.wallpaper.busy
                onClicked: { picker.dynamicFile = true; picker.open() }
            }
        }
        Text {
            Layout.fillWidth: true
            text: "支持 PNG / JPG / WebP / BMP（≤80 MB），GIF 与 MP4 / WebM / MOV 视频（≤1 GB）。动态壁纸静音循环，窗口最小化时暂停。"
            color: Theme.textMuted; font.pixelSize: 11; wrapMode: Text.Wrap
        }
        RowLayout {
            Layout.fillWidth: true; enabled: !!settings.wallpaper.kind
            Text { text: "背景压暗"; color: "#c0cad5"; font.pixelSize: 13 }
            Slider {
                id: dimming; objectName: "wallpaperDim"
                Layout.fillWidth: true; from: 0; to: 0.9; stepSize: 0.01; live: false
                value: settings.wallpaper.dim
                Accessible.name: "背景压暗"
                onMoved: settings.wallpaper.setDim(value)
            }
            Text { text: Math.round(dimming.value * 100) + "%"; color: "#c0cad5"; font.pixelSize: 13 }
        }
        Text {
            Layout.fillWidth: true
            visible: settings.reduced && settings.animated
            text: "已开启减少动态效果，动态壁纸暂停。"; color: "#aab7c6"; font.pixelSize: 12; wrapMode: Text.Wrap
        }
        Text {
            Layout.fillWidth: true; visible: !!text
            text: settings.wallpaper.error
            color: "#e2ad9f"; font.pixelSize: 12; wrapMode: Text.Wrap
        }
    }
}
