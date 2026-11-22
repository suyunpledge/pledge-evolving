import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Column {
    id: panel
    objectName: "toolTimeline"
    required property var steps
    required property bool running
    property bool expanded: false
    property int clockMs: 0
    readonly property var lastStep: steps.length ? steps[steps.length - 1] : null
    readonly property int totalMs: !lastStep ? 0 : lastStep.ms < 0
                                   ? Math.max(clockMs, lastStep.at) : lastStep.at + lastStep.ms
    spacing: 4

    function duration(ms) {
        if (ms < 1000) return ms + " ms"
        if (ms < 60000) return (ms / 1000).toFixed(ms < 10000 ? 1 : 0) + " 秒"
        return Math.floor(ms / 60000) + " 分 " + Math.floor(ms % 60000 / 1000) + " 秒"
    }
    Timer {
        interval: 1000; repeat: true; running: panel.visible && panel.running
        onTriggered: panel.clockMs = controller.turnElapsedMs()
    }
    Button {
        id: toggle
        objectName: "toggleToolTimeline"
        flat: true; implicitHeight: 28; leftPadding: 0; rightPadding: 6
        text: "执行过程 · " + panel.duration(panel.totalMs)
        Accessible.name: (panel.expanded ? "收起" : "展开") + text
        onClicked: panel.expanded = !panel.expanded
        background: Rectangle { radius: 5; color: toggle.hovered || toggle.visualFocus ? Theme.solidSurface : "transparent" }
        contentItem: RowLayout {
            spacing: 5
            Glyph { name: panel.expanded ? "chevron-down" : "chevron-right"; ink: Theme.accent; width: 13; height: 13 }
            Text { text: toggle.text; color: Theme.textSecondary; font.pixelSize: 12 }
        }
    }
    Repeater {
        model: panel.expanded ? panel.steps : (panel.running ? panel.steps.slice(-3) : [])
        delegate: RowLayout {
            required property var modelData
            objectName: "toolTimelineStep"
            readonly property bool active: panel.running && modelData.ms < 0
            width: Math.min(panel.width, 440); spacing: 8
            Rectangle {
                Layout.preferredWidth: 5; Layout.preferredHeight: 5; radius: 3
                color: modelData.phase === "retry" ? Theme.warning : (active ? Theme.accent : Theme.textMuted)
            }
            Text {
                Layout.fillWidth: true; text: modelData.label; textFormat: Text.PlainText
                wrapMode: Text.Wrap; font.pixelSize: 12; color: parent.active ? Theme.textPrimary : Theme.textSecondary
            }
            Text {
                text: panel.duration(parent.active ? Math.max(0, panel.clockMs - modelData.at) : Math.max(0, modelData.ms))
                font.pixelSize: 11; color: Theme.textSecondary
            }
        }
    }
}
