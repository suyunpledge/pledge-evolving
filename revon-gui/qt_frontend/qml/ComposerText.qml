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
    property bool tabNavigates: submitOnEnter
    signal submit()
    clip: true
    boundsBehavior: Flickable.StopAtBounds
    contentWidth: horizontalScroll ? Math.max(width, input.contentWidth + input.rightPadding) : width
    contentHeight: input.height
    ScrollBar.vertical: AppScrollBar {}
    ScrollBar.horizontal: AppScrollBar { policy: field.horizontalScroll ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff }

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
    function scrollToRect(rect) {
        const viewportHeight = height - (ScrollBar.horizontal.visible ? ScrollBar.horizontal.height : 0)
        const viewportWidth = width - input.rightPadding
        if (rect.y < contentY)
            contentY = rect.y
        else if (rect.y + rect.height > contentY + viewportHeight)
            contentY = rect.y + rect.height - viewportHeight
        if (horizontalScroll && rect.x < contentX)
            contentX = rect.x
        else if (horizontalScroll && rect.x + rect.width > contentX + viewportWidth)
            contentX = rect.x + rect.width - viewportWidth
    }
    function revealPosition(position) {
        scrollToRect(input.positionToRectangle(position))
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
        leftPadding: 0; rightPadding: field.ScrollBar.vertical.implicitWidth; topPadding: 4
        bottomPadding: 4 + (field.ScrollBar.horizontal.visible ? field.ScrollBar.horizontal.height : 0)
        background: null
        selectionColor: "#526477"; selectedTextColor: "white"
        onActiveFocusChanged: Qt.callLater(field.refreshInputMethod)
        onCursorRectangleChanged: {
            field.scrollToRect(cursorRectangle)
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
        // Chat composers are single-purpose prompts: Tab must leave the field so
        // keyboard users can reach the model/effort/send controls (no focus trap).
        // Code/document editors (submitOnEnter: false) keep literal tabs.
        function moveFocus(event, forward) {
            if (!field.tabNavigates || inputMethodComposing) {
                event.accepted = false
                return
            }
            const next = input.nextItemInFocusChain(forward)
            if (next && next !== input) next.forceActiveFocus(forward ? Qt.TabFocusReason : Qt.BacktabFocusReason)
            event.accepted = true
        }
        Keys.onTabPressed: event => moveFocus(event, true)
        Keys.onBacktabPressed: event => moveFocus(event, false)
    }
}
