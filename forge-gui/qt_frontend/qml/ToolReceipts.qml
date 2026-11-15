import QtQuick

Column {
    id: card
    objectName: "replyToolReceipts"
    required property var material
    required property var receipts
    property bool expanded: false
    readonly property int completed: receipts.filter(row => row.outcome === "已完成").length
    spacing: 6

    GlassButton {
        objectName: "toggleToolReceipts"
        material: card.material
        glyph: card.expanded ? "chevron-up" : "chevron-down"
        text: "工具调用 · " + card.receipts.length + " 次"
              + (card.completed === card.receipts.length ? " · 全部完成" : " · " + card.completed + " 次完成")
        onClicked: card.expanded = !card.expanded
    }

    Repeater {
        model: card.expanded ? card.receipts : []
        delegate: Item {
            required property var modelData
            width: card.width
            height: detailBody.height + 20
            GlassSurface { anchors.fill: parent; material: card.material; radius: 12; tint: "#222b3743" }
            Column {
                id: detailBody
                x: 12; y: 10; width: parent.width - 24
                spacing: 6
                Text {
                    width: parent.width
                    text: modelData.name + " · " + modelData.outcome
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    color: modelData.outcome === "已完成" ? "#c1d0de" : "#e3bd90"
                    font.pixelSize: 13
                }
                Text {
                    visible: text.length > 0
                    width: parent.width
                    text: modelData.detail
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    maximumLineCount: 10
                    elide: Text.ElideRight
                    color: "#a1adba"
                    font.pixelSize: 12
                }
            }
        }
    }
}
