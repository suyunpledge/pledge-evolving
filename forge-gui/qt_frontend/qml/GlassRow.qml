import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AbstractButton {
    id: row
    required property var material
    property string glyph: "plug"
    property url iconUrl: ""
    property string detail: ""
    property string status: ""
    property color statusColor: "#9eacbd"
    property string actionGlyph: "chevron-right"
    implicitHeight: 48
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    padding: 12
    Accessible.name: text + (status ? "，" + status : "")
    background: GlassSurface {
        material: row.material; radius: 12; tint: "#53232f3b"
        emphasis: row.checked ? 0.55 : row.activeFocus ? 0.4 : 0
        hoverAmount: row.hovered ? 1 : 0; pointer: hoverPoint.point.position
    }
    HoverHandler { id: hoverPoint; cursorShape: Qt.PointingHandCursor }
    contentItem: RowLayout {
        spacing: 10
        Image {
            id: brand
            source: row.iconUrl; visible: status === Image.Ready
            Layout.preferredWidth: 20; Layout.preferredHeight: 20; fillMode: Image.PreserveAspectFit
            sourceSize.width: 20 * Screen.devicePixelRatio; sourceSize.height: 20 * Screen.devicePixelRatio
            asynchronous: true
        }
        Glyph { name: row.glyph; visible: !brand.visible; Layout.preferredWidth: 20; Layout.preferredHeight: 18 }
        Text {
            objectName: "glassRowTitle"
            text: row.text.replace(/[\r\n\u2028\u2029]+/g, " ")
            textFormat: Text.PlainText; maximumLineCount: 1
            color: "#e9edf2"; font.pixelSize: 14; font.weight: Font.Medium; elide: Text.ElideRight
            Layout.fillWidth: true; Layout.preferredWidth: 190
        }
        Text {
            objectName: "glassRowDetail"
            text: row.detail.replace(/[\r\n\u2028\u2029]+/g, " ")
            textFormat: Text.PlainText; maximumLineCount: 1
            color: "#99a8b9"; font.pixelSize: 12; elide: Text.ElideRight
            visible: row.width > 360 && !!text; Layout.fillWidth: true; Layout.preferredWidth: 280
        }
        Text { text: row.status; color: row.statusColor; font.pixelSize: 12; elide: Text.ElideRight; Layout.maximumWidth: 120; visible: !!text }
        Glyph { name: row.actionGlyph; Layout.preferredWidth: 15; Layout.preferredHeight: 15 }
    }
    Keys.onReturnPressed: clicked()
}
