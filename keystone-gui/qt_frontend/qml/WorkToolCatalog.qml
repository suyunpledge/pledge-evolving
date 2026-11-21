import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: panel
    objectName: "workToolCatalog"
    required property var material
    required property var service
    required property bool changesAllowed
    required property bool chatMode
    property string category: "全部"
    readonly property var filtered: {
        const term = search.text.trim().toLowerCase()
        return service.rows.filter(row => (category === "全部" || row.category === category)
            && (!term || (row.label + " " + row.id + " " + row.description).toLowerCase().includes(term)))
    }
    anchors.centerIn: parent
    width: Math.min(680, parent.width - 48)
    height: Math.min(630, parent.height - 48)
    padding: 20
    modal: true
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    Overlay.modal: Rectangle { color: "#7705080d" }
    background: GlassSurface { material: panel.material; radius: 24; tint: "#e11c2430" }
    onOpened: { category = "全部"; search.clear(); service.refresh(); search.forceActiveFocus() }

    contentItem: ColumnLayout {
        spacing: 10
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: panel.chatMode ? "Chat 工具" : "Work 工具"; color: "#eef2f7"; font.pixelSize: 20; font.weight: Font.DemiBold }
            GlassButton {
                objectName: "toggleWorkTools"
                material: panel.material; glyph: "blocks"; tip: "Chat 与 Work 共享工具开关"; text: panel.service.enabled ? "调用已开启" : "调用已关闭"
                selected: panel.service.enabled; enabled: panel.changesAllowed
                onClicked: panel.service.setEnabled(!panel.service.enabled)
            }
            GlassButton { material: panel.material; glyph: "x"; tip: "关闭"; onClicked: panel.close() }
        }
        TextField {
            id: search
            objectName: "workToolSearch"
            Layout.fillWidth: true; Layout.preferredHeight: 40
            placeholderText: "搜索名称或工具标识"; color: "#edf1f5"; placeholderTextColor: "#9daab9"
            leftPadding: 12; selectByMouse: true
            background: GlassSurface { material: panel.material; radius: 10; emphasis: search.activeFocus ? 0.65 : 0 }
        }
        Flow {
            Layout.fillWidth: true; spacing: 6
            Repeater {
                model: panel.chatMode ? ["全部", "计算", "图表与办公", "连接器"] : ["全部", "计算", "图表与办公", "文本", "搜索", "文件与代码", "媒体", "连接器", "其他"]
                GlassButton {
                    required property string modelData
                    material: panel.material; text: modelData; height: 30
                    selected: panel.category === modelData; onClicked: panel.category = modelData
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true
            GlassButton { material: panel.material; visible: !panel.chatMode; quiet: true; text: "全部启用"; enabled: panel.changesAllowed && panel.service.enabled; onClicked: panel.service.setAll(true) }
            GlassButton { material: panel.material; visible: !panel.chatMode; quiet: true; text: "全部关闭"; enabled: panel.changesAllowed && panel.service.enabled; onClicked: panel.service.setAll(false) }
            Text {
                Layout.fillWidth: true
                text: panel.service.loading ? "正在读取网关工具…" : panel.service.error
                color: "#b5c0cd"; font.pixelSize: 12; elide: Text.ElideRight
            }
        }
        ListView {
            id: tools
            Layout.fillWidth: true; Layout.fillHeight: true
            model: panel.filtered; clip: true; spacing: 7
            ScrollBar.vertical: ScrollBar {}
            delegate: Item {
                required property var modelData
                width: tools.width - 8; height: 62
                GlassSurface { anchors.fill: parent; material: panel.material; radius: 12; tint: "#25384350" }
                RowLayout {
                    anchors.fill: parent; anchors.margins: 10; spacing: 12
                    ColumnLayout {
                        Layout.fillWidth: true; spacing: 4
                        Text {
                            Layout.fillWidth: true; text: modelData.label; textFormat: Text.PlainText
                            elide: Text.ElideRight; color: "#ebf0f6"; font.pixelSize: 14
                        }
                        Text {
                            Layout.fillWidth: true; text: modelData.description || modelData.id; textFormat: Text.PlainText
                            elide: Text.ElideRight; color: "#a3b0bf"; font.pixelSize: 11
                        }
                    }
                    GlassButton {
                        objectName: "workTool_" + modelData.id
                        material: panel.material; height: 32
                        text: !modelData.available ? "权限不可用" : modelData.enabled && panel.service.enabled ? "已启用" : "已关闭"
                        selected: modelData.available && modelData.enabled && panel.service.enabled
                        enabled: panel.changesAllowed && panel.service.enabled && modelData.available
                        Accessible.name: modelData.label + " · " + text
                        onClicked: panel.service.setTool(modelData.id, !modelData.enabled)
                    }
                }
            }
        }
        Text {
            Layout.fillWidth: true; visible: !panel.service.loading && !panel.filtered.length
            text: "没有匹配的工具"; color: "#a3b0bf"; font.pixelSize: 13
        }
    }
}
