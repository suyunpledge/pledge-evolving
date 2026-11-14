import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: catalog
    objectName: "providerCatalog"
    required property var material
    property var selected: null
    property bool prepared: false
    function showCatalog(query) {
        if (query || !prepared) search.text = query
        prepared = true
        selected = null
        Qt.callLater(open)
    }
    modal: true; focus: true; padding: 20
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: GlassSurface { material: catalog.material; radius: 22; tint: "#ed19232d" }
    Connections { target: workbench; function onCatalogRequested(query) { catalog.showCatalog(query) } }
    contentItem: ColumnLayout {
        spacing: 12
        RowLayout {
            Text { Layout.fillWidth: true; text: catalog.selected ? catalog.selected.name : "模型厂商"; color: "#eef2f6"; font.pixelSize: 19; font.weight: Font.DemiBold; elide: Text.ElideRight }
            GlassButton { material: catalog.material; visible: !!catalog.selected; glyph: "chevron-left"; text: "厂商"; onClicked: catalog.selected = null }
            GlassButton { material: catalog.material; glyph: "x"; tip: "关闭目录"; onClicked: catalog.close() }
        }
        TextField {
            id: search; objectName: "providerSearch"
            visible: !catalog.selected; Layout.fillWidth: true; Layout.preferredHeight: 38
            placeholderText: "搜索厂商、别名或地址"; color: "#e7edf3"; placeholderTextColor: "#8f9cab"
            selectByMouse: true; leftPadding: 12
            background: GlassSurface { material: catalog.material; radius: 10; tint: "#52202b36" }
        }
        Item {
            Layout.fillWidth: true; Layout.fillHeight: true
            ListView {
                id: vendors; objectName: "providerRows"
                anchors.fill: parent; visible: !catalog.selected
                model: workbench.providers.filter(p => p.search.includes(search.text.trim().toLowerCase()))
                clip: true; spacing: 7; reuseItems: true
                ScrollBar.vertical: ScrollBar { width: 4 }
                delegate: GlassRow {
                    required property var modelData
                    material: catalog.material; width: ListView.view.width
                    text: modelData.name; iconUrl: modelData.icon; glyph: "box"
                    detail: modelData.plans.map(p => p.label).join(" · "); status: modelData.source
                    onClicked: catalog.selected = modelData
                }
            }
            Text { anchors.centerIn: parent; visible: !catalog.selected && vendors.count === 0; text: "没有匹配的厂商"; color: "#a1adbb" }
            ListView {
                anchors.fill: parent; visible: !!catalog.selected
                model: catalog.selected ? catalog.selected.plans : []
                clip: true; spacing: 7; reuseItems: true
                ScrollBar.vertical: ScrollBar { width: 4 }
                delegate: GlassRow {
                    required property var modelData
                    material: catalog.material; width: ListView.view.width
                    text: modelData.label; glyph: "plug"; detail: modelData.url
                    status: modelData.usable ? modelData.wire : "暂不支持"
                    enabled: modelData.usable
                    onClicked: { workbench.useProviderPlan(catalog.selected.key, modelData.label); catalog.close() }
                }
            }
        }
    }
}
