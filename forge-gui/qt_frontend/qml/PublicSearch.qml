import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: panel
    objectName: "publicSearchPanel"
    required property var material
    readonly property var service: workbench.search
    anchors.centerIn: parent
    modal: true; focus: true; padding: 20
    background: GlassSurface { material: panel.material; radius: 22; tint: "#f219222c" }
    property string activeSource: service.engine
    readonly property string githubKind: githubType.currentValue || "repositories"

    function start() {
        if (!query.text.trim())
            return
        service.run(query.text.trim(), activeSource, githubKind)
    }

    onOpened: {
        activeSource = service.engine
        service.reset()
        query.forceActiveFocus()
        query.selectAll()
    }
    onClosed: {
        service.cancel()
        if (workbench.page === "chat" && !workbench.browserVisible)
            panel.restoreComposer()
    }
    signal restoreComposer()

    contentItem: ColumnLayout {
        spacing: 11
        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                text: "联网搜索"; color: "#edf2f6"; font.pixelSize: 21; font.weight: Font.DemiBold
            }
            GlassButton { material: panel.material; glyph: "x"; tip: "关闭搜索"; onClicked: panel.close() }
        }
        Flow {
            Layout.fillWidth: true; Layout.preferredHeight: implicitHeight
            spacing: 6
            Repeater {
                model: panel.service.sources
                GlassButton {
                    required property var modelData
                    objectName: "searchSource_" + modelData.id
                    material: panel.material; text: modelData.name
                    selected: panel.activeSource === modelData.id
                    onClicked: {
                        panel.activeSource = modelData.id
                        panel.service.setEngine(modelData.id)
                        if (query.text.trim()) panel.start()
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            TextField {
                id: query; objectName: "publicSearchQuery"
                Layout.fillWidth: true; Layout.preferredHeight: 39
                placeholderText: panel.activeSource === "github" ? "关键词或 GitHub 搜索条件" : "输入搜索词"
                Accessible.name: placeholderText
                maximumLength: 500; selectByMouse: true; leftPadding: 12
                color: "#edf2f6"; placeholderTextColor: "#93a3b5"
                background: GlassSurface { material: panel.material; radius: 10; emphasis: query.activeFocus ? 0.45 : 0 }
                onAccepted: panel.start()
            }
            ComboBox {
                id: githubType; objectName: "githubSearchType"
                visible: panel.activeSource === "github"
                model: [{name: "仓库", key: "repositories"}, {name: "议题", key: "issues"}, {name: "拉取请求", key: "pullrequests"}]
                textRole: "name"; valueRole: "key"
                Accessible.name: "GitHub 搜索类型"
                Layout.preferredWidth: 114; Layout.preferredHeight: 39
                palette.buttonText: "#edf2f6"; palette.base: "#202b36"; palette.text: "#edf2f6"
                background: GlassSurface { material: panel.material; radius: 10 }
                onActivated: if (query.text.trim()) panel.start()
            }
            GlassButton {
                material: panel.material; glyph: panel.service.busy ? "x" : "search"
                text: panel.service.busy ? "停止" : "搜索"
                selected: true; enabled: panel.service.busy || !!query.text.trim()
                onClicked: panel.service.busy ? panel.service.cancel() : panel.start()
            }
        }
        RowLayout {
            Layout.fillWidth: true
            Text {
                Layout.fillWidth: true
                wrapMode: Text.Wrap
                color: panel.service.error ? "#e2ad9f" : "#a5b1bf"; font.pixelSize: 12
                text: panel.service.busy ? "正在搜索…" : panel.service.error
                    || (panel.service.sourceUrl ? panel.service.rows.length + " 条结果" : "无需登录 · 当前来源也用于 Work 对话和浏览器地址栏")
            }
            GlassButton {
                material: panel.material; glyph: "external-link"; text: "打开原站"
                enabled: !!query.text.trim()
                onClicked: {
                    const url = panel.service.address(query.text, panel.activeSource, panel.githubKind)
                    if (url) {
                        workbench.openBrowser(url)
                        panel.close()
                    }
                }
            }
        }
        ListView {
            objectName: "publicSearchResults"
            Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; spacing: 7; reuseItems: true
            model: panel.service.rows
            ScrollBar.vertical: ScrollBar { width: 4 }
            delegate: GlassRow {
                required property var modelData
                material: panel.material; width: ListView.view.width
                glyph: modelData.engine === "github" ? "code" : "globe"
                text: modelData.title; detail: modelData.snippet || modelData.url
                actionGlyph: "external-link"
                onClicked: { workbench.openBrowser(modelData.url); panel.close() }
            }
        }
    }
}
