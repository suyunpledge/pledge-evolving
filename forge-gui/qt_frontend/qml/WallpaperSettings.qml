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
    property string playbackError: ""
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

    contentItem: ColumnLayout {
        spacing: 14
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: "Agent 壁纸"; color: "#eef2f6"; font.pixelSize: 20; font.weight: Font.DemiBold }
            GlassButton { material: settings.material; glyph: "x"; tip: "关闭壁纸设置"; onClicked: settings.close() }
        }
        Text {
            Layout.fillWidth: true
            text: settings.wallpaper.busy ? "正在导入…" : settings.wallpaper.name || "Forge 默认背景"
            color: "#bcc8d5"; elide: Text.ElideMiddle; font.pixelSize: 13
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
            visible: settings.reduced && (settings.wallpaper.kind === "gif" || settings.wallpaper.kind === "video")
            text: "已开启减少动态效果，动态壁纸暂停。"; color: "#aab7c6"; font.pixelSize: 12; wrapMode: Text.Wrap
        }
        Text {
            Layout.fillWidth: true; visible: !!text
            text: settings.wallpaper.error || settings.playbackError
            color: "#e2ad9f"; font.pixelSize: 12; wrapMode: Text.Wrap
        }
        GlassButton {
            material: settings.material; text: "恢复默认"; glyph: "undo-2"
            enabled: !settings.wallpaper.busy && (!!settings.wallpaper.kind || !!settings.wallpaper.error)
            onClicked: settings.wallpaper.reset()
        }
    }
}
