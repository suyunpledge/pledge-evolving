import QtQuick
import QtQuick.Controls

ScrollView {
    id: view
    rightPadding: rail.implicitWidth
    ScrollBar.vertical: AppScrollBar {
        id: rail
        parent: view
        x: view.width - width
        y: view.topPadding
        height: view.availableHeight
    }
    ScrollBar.horizontal: AppScrollBar {
        parent: view
        x: view.leftPadding
        y: view.height - height
        width: view.availableWidth
    }
}
