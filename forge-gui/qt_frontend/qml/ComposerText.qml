import QtQuick
import QtQuick.Controls

Flickable {
    id: field
    property alias editor: input
    property alias text: input.text
    property alias placeholder: input.placeholderText
    property string editorName: ""
    property bool submitOnEnter: true
    property bool horizontalScroll: false
    signal submit()
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    contentWidth: horizontalScroll ? Math.max(width, input.contentWidth) : width
    contentHeight: input.height

    // Resize and scroll can move the cursor without changing its local rect.
    readonly property point sceneOrigin: {
        for (let item = input; item; item = item.parent) {
            const geometry = [item.x, item.y, item.width, item.height]
        }
        return input.mapToItem(null, 0, 0)
    }
    function refreshInputMethod() {
        const win = field.Window.window
        if (input.activeFocus && input.visible && win && win.active)
            controller.syncInputMethod(input.mapToItem(null, 0, 0), input.width, input.height)
    }
    onSceneOriginChanged: Qt.callLater(refreshInputMethod)
    Connections {
        target: field.Window.window
        function onXChanged() { Qt.callLater(field.refreshInputMethod) }
        function onYChanged() { Qt.callLater(field.refreshInputMethod) }
        function onVisibilityChanged() { Qt.callLater(field.refreshInputMethod) }
        function onActiveChanged() { Qt.callLater(field.refreshInputMethod) }
    }

    TextArea {
        id: input
        objectName: field.editorName
        width: field.width
        height: Math.max(field.height, implicitHeight)
        color: "#edf0f4"; placeholderTextColor: "#97a2b0"
        font.pixelSize: 15
        wrapMode: field.horizontalScroll ? TextEdit.NoWrap : TextEdit.Wrap; selectByMouse: true
        leftPadding: 0; rightPadding: 0; topPadding: 4; bottomPadding: 4
        background: null
        selectionColor: "#526477"; selectedTextColor: "white"
        onActiveFocusChanged: Qt.callLater(field.refreshInputMethod)
        onCursorRectangleChanged: {
            if (cursorRectangle.y < field.contentY)
                field.contentY = cursorRectangle.y
            else if (cursorRectangle.y + cursorRectangle.height > field.contentY + field.height)
                field.contentY = cursorRectangle.y + cursorRectangle.height - field.height
            Qt.callLater(field.refreshInputMethod)
        }
        function enter(event) {
            if (!field.submitOnEnter || inputMethodComposing || (event.modifiers & Qt.ShiftModifier)) {
                event.accepted = false
            } else {
                field.submit()
                event.accepted = true
            }
        }
        Keys.onReturnPressed: event => enter(event)
        Keys.onEnterPressed: event => enter(event)
    }
}
