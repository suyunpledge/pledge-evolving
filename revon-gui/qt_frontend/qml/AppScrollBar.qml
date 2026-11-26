import QtQuick
import QtQuick.Templates as T

T.ScrollBar {
    id: bar
    implicitWidth: 14
    implicitHeight: 14
    padding: 3
    z: 5
    interactive: true
    hoverEnabled: true
    visible: policy !== T.ScrollBar.AlwaysOff && size > 0 && size < 1
    minimumSize: Math.min(0.5, 28 / Math.max(1, vertical ? availableHeight : availableWidth))
    Accessible.name: vertical ? qsTr("垂直滚动条") : qsTr("水平滚动条")

    background: Rectangle {
        radius: 7
        color: "#cf202a37"
    }
    contentItem: Rectangle {
        radius: 4
        color: bar.pressed ? Theme.focusRing : bar.hovered ? "#afbed0" : "#8193a8"
    }
}
