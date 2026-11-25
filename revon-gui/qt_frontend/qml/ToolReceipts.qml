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
        glyphInk: card.completed === card.receipts.length ? Theme.success : Theme.warning
        text: "工具调用 · " + card.receipts.length + " 次"
              + (card.completed === card.receipts.length ? " · 全部完成" : " · " + card.completed + " 次完成")
        onClicked: card.expanded = !card.expanded
    }

    FontMetrics { id: attachmentMetrics; font.pixelSize: 13 }
    Repeater {
        model: {
            const seen = new Set()
            return card.receipts.filter(row => {
                if (!row.attachment || seen.has(row.attachment)) return false
                seen.add(row.attachment)
                return true
            })
        }
        GlassButton {
            required property var modelData
            objectName: "openToolAttachment"
            readonly property string filename: modelData.attachment.split("/").pop()
            material: card.material; glyph: "file"
            text: attachmentMetrics.elidedText(filename, Qt.ElideMiddle, Math.max(60, card.width - 60))
            Accessible.name: "打开 " + filename
            tip: "打开 " + filename
            onClicked: controller.openAttachment(modelData.attachment)
        }
    }

    Repeater {
        model: card.expanded ? card.receipts : []
        delegate: Item {
            required property var modelData
            readonly property bool ok: modelData.outcome === "已完成"
            width: card.width
            height: detailBody.height + 20
            GlassSurface { anchors.fill: parent; material: card.material; radius: 12; tint: "#222b3743" }
            Column {
                id: detailBody
                x: 12; y: 10; width: parent.width - 24
                spacing: 6
                Row {
                    width: parent.width; spacing: 7
                    Glyph {
                        name: ok ? "circle-check" : "triangle-alert"
                        ink: ok ? Theme.success : Theme.warning
                        width: 15; height: 15
                        anchors.verticalCenter: parent.verticalCenter
                    }
                    Text {
                        width: parent.width - 22
                        text: modelData.name + " · " + modelData.outcome
                        textFormat: Text.PlainText
                        elide: Text.ElideRight
                        color: ok ? Theme.successText : Theme.warningText
                        font.pixelSize: 13
                        anchors.verticalCenter: parent.verticalCenter
                    }
                }
                Text {
                    visible: text.length > 0
                    width: parent.width
                    text: modelData.detail
                    textFormat: Text.PlainText
                    wrapMode: Text.Wrap
                    maximumLineCount: 10
                    elide: Text.ElideRight
                    color: Theme.textSecondary
                    font.pixelSize: 12
                }
            }
        }
    }
}
