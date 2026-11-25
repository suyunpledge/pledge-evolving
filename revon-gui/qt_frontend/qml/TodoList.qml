import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Column {
    id: list
    required property var items
    required property bool running
    property bool expanded: true
    readonly property int doneCount: items.filter(item => item.status === "completed").length
    visible: items.length > 0
    spacing: 5
    Button {
        objectName: "toggleTodos"
        flat: true; leftPadding: 0; implicitHeight: 28
        text: "待办清单  " + list.doneCount + " / " + list.items.length + (list.expanded ? "  ▾" : "  ▸")
        Accessible.name: (list.expanded ? "收起" : "展开") + text
        onClicked: list.expanded = !list.expanded
        background: null
        contentItem: Text { text: parent.text; color: Theme.textPrimary; font.pixelSize: 13; font.weight: Font.DemiBold }
    }
    Repeater {
        model: list.expanded ? list.items : []
        delegate: RowLayout {
            required property var modelData
            width: list.width; spacing: 9
            readonly property string stateLabel: ({pending:"待处理", in_progress:list.running ? "进行中" : "未完成",
                                                  completed:"已完成", blocked:"受阻"})[modelData.status] || "待处理"
            Text { text: parent.stateLabel; font.pixelSize: 11
                   color: modelData.status === "blocked" ? Theme.warning : modelData.status === "in_progress" && list.running ? Theme.accent : Theme.textSecondary }
            Text { text: modelData.text; textFormat: Text.PlainText; wrapMode: Text.Wrap
                   Layout.fillWidth: true; font.pixelSize: 13; color: modelData.status === "completed" ? Theme.textSecondary : Theme.textPrimary }
        }
    }
}
