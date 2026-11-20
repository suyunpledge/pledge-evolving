import QtQuick
import QtQuick.Controls

Item {
    id: card
    objectName: "replyChangeCard"
    required property var material
    required property var info
    signal reviewRequested()
    signal revertRequested()
    implicitHeight: body.height + 28
    GlassSurface { anchors.fill: parent; material: card.material; radius: 14; emphasis: 0.1; tint: "#d4182029" }
    Column {
        id: body; x: 14; y: 14; width: parent.width - 28; spacing: 10
        Row {
            spacing: 9
            Glyph { name: card.info.restored ? "check" : "file-code"; ink: card.info.restored ? Theme.success : Theme.accent; width: 17; height: 17 }
            Text { text: card.info.restored ? "已回退本次修改" : "已修改 " + card.info.count + " 个文件"; color: card.info.restored ? Theme.successText : Theme.textPrimary; font.pixelSize: 13; font.weight: Font.DemiBold }
            Text { text: "+" + card.info.added; color: Theme.success; font.pixelSize: 12 }
            Text { text: "−" + card.info.removed; color: Theme.danger; font.pixelSize: 12 }
        }
        Repeater {
            model: (card.info.files || []).slice(0, 4)
            delegate: Row {
                required property var modelData
                width: body.width; spacing: 8
                Glyph { name: "file-text"; ink: Theme.textMuted; width: 15; height: 18 }
                Text { width: Math.max(60, parent.width - 110); text: modelData.name; color: Theme.textSecondary; elide: Text.ElideMiddle; font.pixelSize: 12 }
                Text { text: modelData.added === null ? "" : "+" + modelData.added; color: Theme.success; font.pixelSize: 12 }
                Text { text: modelData.removed === null ? "" : "−" + modelData.removed; color: Theme.danger; font.pixelSize: 12 }
            }
        }
        Text { visible: card.info.count > 4; text: "还有 " + (card.info.count - 4) + " 个文件"; color: "#84909e"; font.pixelSize: 12 }
        Text { visible: card.info.countsKnown === false; text: "部分文件无法统计文本行数"; color: "#84909e"; font.pixelSize: 11 }
        Row {
            spacing: 6
            GlassButton { material: card.material; text: "查看修改"; glyph: "code"; quiet: true; enabled: !controller.busy; onClicked: card.reviewRequested() }
            GlassButton { material: card.material; text: "回退"; glyph: "undo-2"; quiet: true; visible: !card.info.restored; enabled: !controller.busy && card.info.canRevert === true; tip: card.info.unavailable ? "此记录没有可用的回退快照" : "检查差异并回退本次修改"; onClicked: card.revertRequested() }
        }
    }
}
