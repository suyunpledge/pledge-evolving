import QtQuick
import QtQuick.Controls

AbstractButton {
    id: control
    required property var material
    property string glyph: ""
    property bool selected: false
    property bool quiet: false
    property string tip: ""
    property real cornerRadius: 12
    property color ink: "#e1e5e9"
    implicitHeight: 36
    implicitWidth: Math.max(36, content.implicitWidth + 24)
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    opacity: enabled ? 1 : 0.4
    Accessible.name: text || tip
    Accessible.role: Accessible.Button
    ToolTip.visible: hovered && tip.length > 0
    ToolTip.text: tip
    ToolTip.delay: 650
    background: GlassSurface {
        material: control.material
        radius: control.cornerRadius
        visible: !control.quiet || control.hovered || control.selected || control.visualFocus
        emphasis: control.selected ? 1 : control.visualFocus ? 0.65 : 0
        hoverAmount: control.hovered || control.down ? 1 : 0
        pointer: hover.point.position
    }
    HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
    contentItem: Item {
        id: content
        implicitWidth: contentRow.implicitWidth
        implicitHeight: 18
        Row {
        id: contentRow
        anchors.centerIn: parent
        spacing: control.text.length > 0 && control.glyph.length > 0 ? 8 : 0
        Glyph {
            visible: control.glyph.length > 0
            width: visible ? 17 : 0; height: 17
            anchors.verticalCenter: parent.verticalCenter
            name: control.glyph; ink: control.ink
        }
        Text {
            text: control.text
            color: control.ink
            font.pixelSize: 13
            anchors.verticalCenter: parent.verticalCenter
        }
        }
    }
    Keys.onReturnPressed: clicked()
}
