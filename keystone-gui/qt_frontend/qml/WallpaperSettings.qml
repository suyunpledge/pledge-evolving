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
                height: 80; radius: 11; clip: true
                color: "#151f2c"
                scale: tile.pressed ? 0.98 : (tile.hovered && !settings.reduced ? 1.015 : 1.0)
                Behavior on scale { NumberAnimation { duration: settings.reduced ? 0 : 140; easing.type: Easing.OutCubic } }
                border.width: tile.current ? 2 : tile.visualFocus ? 2 : tile.hovered ? 1 : 0
                border.color: tile.current ? Theme.accent : tile.visualFocus ? Theme.focusRing : "#66ffffff"
                // Built-in background: a polar aurora + starlight swatch matching ambient.frag.
                gradient: tile.itemId ? null : aurora
                Gradient {
                    id: aurora
                    orientation: Gradient.Vertical
                    GradientStop { position: 0.0; color: "#0c1528" }
                    GradientStop { position: 0.52; color: "#15334d" }
                    GradientStop { position: 1.0; color: "#1a3b44" }
                }
                Canvas {
                    anchors.fill: parent
                    visible: !tile.itemId
                    onPaint: {
                        const ctx = getContext("2d")
                        ctx.clearRect(0, 0, width, height)
                        const g = ctx.createRadialGradient(width * 0.72, height * 0.32, 2, width * 0.65, height * 0.55, width * 0.6)
                        g.addColorStop(0.0, "rgba(110, 228, 215, 0.34)")
                        g.addColorStop(0.55, "rgba(91, 155, 248, 0.18)")
                        g.addColorStop(1.0, "rgba(150, 110, 220, 0.0)")
                        ctx.fillStyle = g
                        ctx.fillRect(0, 0, width, height)
                        ctx.fillStyle = "rgba(240, 248, 255, 0.75)"
                        for (const pt of [[0.22, 0.28, 1.3], [0.78, 0.24, 1.6], [0.58, 0.38, 1.1], [0.35, 0.62, 1.2]]) {
                            ctx.beginPath()
                            ctx.arc(width * pt[0], height * pt[1], pt[2], 0, Math.PI * 2)
                            ctx.fill()
                        }
                    }
                }
                Image {
                    anchors { fill: parent; margins: frame.border.width }
                    visible: !!tile.itemId && status === Image.Ready
                    source: tile.preview
                    sourceSize: Qt.size(320, 200)
                    asynchronous: true; smooth: true
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
                    width: badgeRow.implicitWidth + 10; height: 19; radius: 6
                    color: "#c8080c12"; border.color: "#2affffff"; border.width: 1
                    Row {
                        id: badgeRow; anchors.centerIn: parent; spacing: 3
                        Glyph { visible: tile.kind === "video"; width: 9; height: 9; name: "play"; ink: "#b8d4ff"; anchors.verticalCenter: parent.verticalCenter }
                        Text { id: badge; text: tile.kind === "gif" ? "GIF" : "视频"; color: "#e6edf5"; font.pixelSize: 10; font.weight: Font.Medium; anchors.verticalCenter: parent.verticalCenter }
                    }
                }
                Rectangle {
                    visible: tile.current
                    anchors { right: parent.right; top: parent.top; margins: 6 }
                    width: 21; height: 21; radius: 10.5; color: Theme.accent
                    border.color: "#88ffffff"; border.width: 1
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
            Layout.fillWidth: true; spacing: 10; enabled: !!settings.wallpaper.kind
            opacity: enabled ? 1.0 : 0.48
            Text { text: "背景压暗"; color: "#c0cad5"; font.pixelSize: 13 }
            Slider {
                id: dimming; objectName: "wallpaperDim"
                Layout.fillWidth: true; from: 0; to: 0.9; stepSize: 0.01; live: true
                value: settings.wallpaper.dim
                Accessible.name: "背景压暗"
                onMoved: {
                    if (pressed) settings.wallpaper.previewDim(value)
                    else settings.wallpaper.setDim(value)
                }
                onPressedChanged: if (!pressed) settings.wallpaper.setDim(value)
                background: Rectangle {
                    x: dimming.leftPadding
                    y: dimming.topPadding + dimming.availableHeight / 2 - 4
                    width: dimming.availableWidth; height: 8; radius: 4
                    color: "#26ffffff"; border.color: "#22ffffff"; border.width: 1
                    Rectangle {
                        width: dimming.visualPosition * parent.width; height: parent.height; radius: 4
                        color: Theme.accent; opacity: 0.78
                    }
                }
                handle: Rectangle {
                    x: dimming.leftPadding + dimming.visualPosition * (dimming.availableWidth - width)
                    y: dimming.topPadding + dimming.availableHeight / 2 - height / 2
                    width: 20; height: 20; radius: 10
                    color: dimming.pressed ? "#ffffff" : "#e9eef5"
                    border.color: dimming.visualFocus ? Theme.focusRing : Theme.accent
                    border.width: dimming.visualFocus ? 2 : 1.5
                }
            }
            Text {
                Layout.preferredWidth: 36; horizontalAlignment: Text.AlignRight
                text: Math.round(dimming.value * 100) + "%"
                color: "#d6e0eb"; font.pixelSize: 13
            }
            GlassButton {
                material: settings.material; quiet: true; implicitHeight: 28
                visible: !!settings.wallpaper.kind && Math.abs(settings.wallpaper.dim - 0.52) >= 0.015
                text: "默认"; tip: "恢复 52% 最佳阅读对比度"
                onClicked: settings.wallpaper.setDim(0.52)
            }
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
