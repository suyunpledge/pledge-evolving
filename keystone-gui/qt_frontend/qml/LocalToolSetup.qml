import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: dialog
    objectName: "localToolSetup"
    required property var material
    required property var service
    modal: true; focus: true; padding: 22
    closePolicy: Popup.CloseOnEscape
    onClosed: if (service.requestKind) service.respond("")
    Overlay.modal: Rectangle { color: "#9905080d" }
    background: GlassSurface { material: dialog.material; radius: 22; tint: "#ed19232d" }

    Connections {
        target: dialog.service
        function onChanged() {
            if (dialog.service.requestKind && !dialog.visible) dialog.open()
            else if (!dialog.service.requestKind && dialog.visible) dialog.close()
        }
    }

    contentItem: ColumnLayout {
        spacing: 12
        Text {
            Layout.fillWidth: true
            text: "启用 ComfyUI"
            color: "#edf2f7"; font.pixelSize: 22; font.weight: Font.DemiBold
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: "启动已有的本机 ComfyUI（端口 8188），或下载官方便携版。下载后仍需为工作流准备模型。"
            color: "#aab6c4"; font.pixelSize: 13
        }
        Repeater {
            model: [
                {key:"nvidia", title:"下载 NVIDIA 便携版", detail:"约 2.1 GB"},
                {key:"amd", title:"下载 AMD 便携版", detail:"约 1.7 GB"},
                {key:"cpu", title:"下载 CPU 便携版", detail:"约 2.1 GB · 使用同一官方包"}
            ]
            delegate: GlassRow {
                required property var modelData
                visible: dialog.service.canDownload
                Layout.fillWidth: true; material: dialog.material
                text: modelData.title; detail: modelData.detail; glyph: "arrow-down"
                objectName: "localToolChoice_" + modelData.key
                onClicked: dialog.service.respond(modelData.key)
            }
        }
        RowLayout {
            Layout.fillWidth: true
            GlassButton {
                objectName: "localToolCancel"; material: dialog.material
                text: "取消"; onClicked: dialog.service.respond("")
            }
            Item { Layout.fillWidth: true }
            GlassButton {
                objectName: "localToolExisting"
                material: dialog.material; glyph: "plug"; text: "已启动，重新检测"
                onClicked: dialog.service.respond("existing")
            }
        }
    }
}
