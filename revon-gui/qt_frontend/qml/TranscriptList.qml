import QtQuick
import QtQuick.Controls

ListView {
    id: list
    property bool followTail: true
    readonly property real textWidth: width - (rail.visible ? rail.width + 3 : 0)
    boundsBehavior: Flickable.StopAtBounds
    function followLatest() {
        if (followTail && !rail.pressed) positionViewAtEnd()
    }
    onContentHeightChanged: Qt.callLater(followLatest)
    onHeightChanged: Qt.callLater(followLatest)
    onMovementStarted: followTail = false
    onMovementEnded: if (!rail.pressed) followTail = atYEnd
    onCountChanged: if (!count) followTail = true
    ScrollBar.vertical: AppScrollBar {
        id: rail
        onPressedChanged: {
            // Measuring newly visible delegates can shift the end during a drag.
            const remainingTrack = (1 - position - size) * availableHeight
            list.followTail = !pressed && (list.atYEnd || remainingTrack <= bottomPadding)
            if (list.followTail) Qt.callLater(list.followLatest)
        }
    }
}
