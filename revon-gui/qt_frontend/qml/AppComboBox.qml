import QtQuick
import QtQuick.Controls

ComboBox {
    id: control
    popup.contentItem: ListView {
        clip: true
        implicitHeight: contentHeight
        model: control.delegateModel
        currentIndex: control.highlightedIndex
        highlightMoveDuration: 0
        ScrollBar.vertical: AppScrollBar {}
    }
}
