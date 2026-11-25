import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: preview
    required property string imageSource
    required property string observationId
    required property var proposedPoint
    required property bool canInput
    required property var material
    signal clickRequested(string observation, int x, int y)

    property var picked: null
    readonly property var point: picked && picked.id === observationId ? picked : proposedPoint
    onObservationIdChanged: picked = null

    Image {
        id: picture
        objectName: "computerPreviewImage"
        Layout.fillWidth: true
        Layout.preferredHeight: 260
        source: preview.imageSource
        fillMode: Image.PreserveAspectFit
        cache: false
        readonly property real imageLeft: (width - paintedWidth) / 2
        readonly property real imageTop: (height - paintedHeight) / 2
        TapHandler {
            enabled: preview.canInput && !!preview.observationId && picture.status === Image.Ready
            onTapped: (eventPoint) => {
                const x = (eventPoint.position.x - picture.imageLeft) / picture.paintedWidth
                const y = (eventPoint.position.y - picture.imageTop) / picture.paintedHeight
                if (x >= 0 && x < 1 && y >= 0 && y < 1)
                    preview.picked = {id: preview.observationId, x: Math.floor(x * picture.sourceSize.width),
                                      y: Math.floor(y * picture.sourceSize.height)}
            }
        }
        Rectangle {
            visible: !!preview.point && picture.status === Image.Ready
            width: 16; height: 16; radius: 8
            x: picture.imageLeft + ((preview.point?.x ?? 0) + 0.5) * picture.paintedWidth / Math.max(1, picture.sourceSize.width) - width / 2
            y: picture.imageTop + ((preview.point?.y ?? 0) + 0.5) * picture.paintedHeight / Math.max(1, picture.sourceSize.height) - height / 2
            color: "transparent"; border.color: "black"; border.width: 3
            Rectangle { anchors.fill: parent; anchors.margins: 2; radius: 8; color: "transparent"; border.color: "white" }
        }
    }
    RowLayout {
        Layout.fillWidth: true
        Text {
            Layout.fillWidth: true
            text: preview.point ? "截图落点 (" + preview.point.x + ", " + preview.point.y + ")"
                : preview.canInput ? "在预览中选择要点击的位置" : "窗口截图预览"
            color: Theme.textSecondary; font.pixelSize: 12; wrapMode: Text.Wrap
        }
        GlassButton {
            objectName: "confirmPreviewPoint"
            material: preview.material
            text: "点击选中位置"
            visible: preview.canInput
            enabled: !!preview.point && !!preview.observationId && picture.status === Image.Ready
            onClicked: preview.clickRequested(preview.observationId, preview.point.x, preview.point.y)
        }
    }
}
