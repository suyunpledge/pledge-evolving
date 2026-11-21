import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

AbstractButton {
    id: control
    property string glyph: ""
    property string tip: ""
    property bool selected: false
    property color iconColor: selected ? "#e1ebf9" : "#aeb8c5"
    implicitHeight: 28
    implicitWidth: text ? content.implicitWidth + 18 : 30
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    opacity: enabled ? 1 : 0.45
    Accessible.name: text || tip
    ToolTip.text: tip
    ToolTip.visible: hovered && !!tip
    ToolTip.delay: 500
    background: Rectangle {
        radius: 3
        color: control.down ? "#39414c" : control.selected ? "#303842" : control.hovered ? "#292e35" : "transparent"
        border.width: control.visualFocus ? 1 : 0; border.color: "#72aaf2"
    }
    contentItem: Item {
        implicitWidth: content.implicitWidth
        RowLayout {
            id: content; anchors.centerIn: parent; spacing: 6
            Glyph { visible: !!control.glyph; name: control.glyph; ink: control.iconColor; Layout.preferredWidth: 16; Layout.preferredHeight: 16 }
            Text { visible: !!control.text; text: control.text; color: "#d0d6de"; font.pixelSize: 12 }
        }
    }
    Keys.onReturnPressed: clicked()
}
