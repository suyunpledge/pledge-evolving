import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: market
    objectName: "page_tools"
    color: Theme.bgFallback
    radius: 12
    PageSnapshot { id: source; pageKey: "tools" }
    readonly property var catalog: source.data
    property string section: "all"
    property bool managing: false
    property bool showResult: false
    component MarketField: TextField {
        id: field
        color: Theme.textPrimary; placeholderTextColor: Theme.textMuted; selectByMouse: true
        Accessible.name: placeholderText
        background: Rectangle {
            color: "#18212d"; radius: 6; border.width: 1
            border.color: field.activeFocus ? Theme.focusRing : Theme.divider
        }
    }
    readonly property string resultText: catalog.result || ""
    onResultTextChanged: showResult = !!resultText
    function invoke(key, id = "") {
        workbench.pageAction("tools", key, JSON.stringify({id: id, path: directory.text, capabilities: capabilities.text}))
        if (key === "inspect" || managing) showResult = true
    }
    readonly property var matchingRows: {
        const query = search.text.trim().toLowerCase()
        return (catalog.rows || []).filter(row => {
            const seat = row.id.startsWith("seat:")
            return (section === "seats" ? seat : !seat &&
                    (section === "all" || (section === "installed" ? row.installed : row.state === "available"))) &&
                   (!query || [row.id, row.title, row.subtitle].join(" ").toLowerCase().includes(query))
        })
    }
    ColumnLayout {
        anchors.fill: parent; anchors.margins: 14; spacing: 8
        RowLayout {
            Layout.fillWidth: true
            Text { text: "插件市场"; color: Theme.textPrimary; font.pixelSize: 20; font.weight: Font.DemiBold }
            Item { Layout.fillWidth: true }
            IdeButton { text: "刷新在线目录"; enabled: !workbench.busy; onClicked: market.invoke("sync") }
            IdeButton { text: "管理"; selected: market.managing; onClicked: market.managing = !market.managing }
        }
        MarketField {
            id: search; objectName: "marketSearch"; Layout.fillWidth: true
            placeholderText: "搜索插件名称、功能或标识"
        }
        RowLayout {
            Repeater {
                model: [{key:"all", label:"全部插件"}, {key:"installed", label:"已安装"},
                        {key:"available", label:"可安装"}, {key:"seats", label:"运行席位"}]
                IdeButton { required property var modelData; text: modelData.label; selected: market.section === modelData.key
                    onClicked: market.section = modelData.key }
            }
            Item { Layout.fillWidth: true }
            Text { text: market.matchingRows.length + " 项"; color: Theme.textMuted; font.pixelSize: 12 }
        }
        ColumnLayout {
            visible: market.managing; Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: market.catalog.hint || ""; color: Theme.textMuted; wrapMode: Text.Wrap; font.pixelSize: 12 }
            RowLayout {
                MarketField { id: directory; Layout.fillWidth: true; placeholderText: "本地插件目录" }
                IdeButton { text: "导入插件"; enabled: !!directory.text.trim() && !workbench.busy; onClicked: market.invoke("installDir") }
            }
            MarketField { id: capabilities; Layout.fillWidth: true; placeholderText: "授权能力，用逗号分隔（留空使用全部声明能力）" }
            Flow {
                Layout.fillWidth: true; spacing: 4
                Repeater {
                    model: (market.catalog.actions || []).filter(a => !["filter","installDir","sync"].includes(a.key))
                    IdeButton { required property var modelData; text: modelData.label; enabled: !workbench.busy; onClicked: market.invoke(modelData.key) }
                }
            }
        }
        ListView {
            id: list; objectName: "marketList"; Layout.fillWidth: true; Layout.fillHeight: true; clip: true
            reuseItems: true; cacheBuffer: 80; spacing: 4
            model: market.matchingRows
            ScrollBar.vertical: AppScrollBar {}
            delegate: Rectangle {
                id: entry; objectName: "marketEntry"; required property var modelData
                width: Math.max(0, list.width - 16); height: details.implicitHeight + 16
                color: "#1a2330"; radius: 7
                ColumnLayout {
                    id: details; x: 10; y: 8; width: Math.max(1, parent.width - 20); spacing: 3
                    Text { Layout.fillWidth: true; text: entry.modelData.title; textFormat: Text.PlainText; color: Theme.textPrimary; elide: Text.ElideRight; font.weight: Font.DemiBold }
                    Text { Layout.fillWidth: true; text: entry.modelData.subtitle; textFormat: Text.PlainText; elide: Text.ElideRight; color: Theme.textSecondary; font.pixelSize: 12 }
                    Flow {
                        Layout.fillWidth: true; spacing: 4
                        Repeater {
                            model: entry.modelData.actions
                            IdeButton { required property var modelData; text: modelData.label; enabled: !workbench.busy
                                onClicked: market.invoke(modelData.key, entry.modelData.id) }
                        }
                    }
                }
            }
            Label {
                anchors.centerIn: parent; visible: list.count === 0
                text: search.text ? "没有匹配的插件，试试其他关键词" : "当前分类暂无条目"
                color: Theme.textSecondary
            }
        }
        RowLayout {
            Layout.fillWidth: true; visible: !!market.resultText
            Text { text: "操作结果"; color: Theme.textSecondary; Layout.fillWidth: true }
            IdeButton { text: market.showResult ? "收起" : "展开"; onClicked: market.showResult = !market.showResult }
        }
        AppScrollView {
            Layout.fillWidth: true; Layout.preferredHeight: Math.min(140, market.height * 0.22)
            visible: !!market.resultText && market.showResult; clip: true
            TextArea { text: market.resultText; readOnly: true; selectByMouse: true; color: Theme.textSecondary
                background: Rectangle { color: Theme.bgBase; radius: 6 }
                textFormat: TextEdit.PlainText; wrapMode: TextEdit.Wrap }
        }
    }
}
