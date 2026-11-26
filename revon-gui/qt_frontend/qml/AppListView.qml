import QtQuick
import QtQuick.Controls

ListView {
    readonly property real rowWidth: Math.max(0, width - rail.implicitWidth - 2)
    ScrollBar.vertical: AppScrollBar { id: rail }
}
