import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Rectangle {
    id: side
    required property var viewData
    required property string activePath
    property string mode: "explorer"
    property string treeSelection: ""
    property int ticket: 0
    property var results: []
    property int fileCount: 0
    property bool searching: false
    property bool limited: false
    property string failure: ""
    readonly property string rootPath: viewData.hint || ""
    signal action(string key, var args)
    signal picked(var entry)
    color: "#151b26"; clip: true
    function explorer() {
        mode = "explorer"
        Qt.callLater(() => tree.forceActiveFocus())
    }
    function search(selection) {
        mode = "search"
        if (selection && !/[\r\n]/.test(selection) && selection !== query.text) query.text = selection
        else if (query.text) schedule()
        Qt.callLater(() => { query.forceActiveFocus(); query.selectAll() })
    }
    function schedule() {
        ++ticket
        results = []; fileCount = 0; failure = ""; limited = false
        searching = !!query.text.length
        debounce.stop()
        if (searching) debounce.restart()
        else workbench.queryWorkspace("text", "", ticket, true)
    }
    function escaped(text) {
        return text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    }
    function activateNode(i) {
        const node = tree.model[i]
        if (!node || workbench.busy) return
        treeSelection = node.path
        if (node.directory) action("toggleFolder", {id: node.id})
        else picked(node)
    }
    function restoreSelection() {
        const idx = tree.model.findIndex(row => row.path === treeSelection)
        if (idx >= 0) { tree.currentIndex = idx; tree.positionViewAtIndex(idx, ListView.Contain) }
    }
    function openHit(i) {
        if (results[i] && !workbench.busy) picked(results[i])
    }
    onRootPathChanged: if (rootPath && query) { treeSelection = ""; query.clear(); results = [] }
    onActivePathChanged: if (activePath) { treeSelection = activePath; Qt.callLater(restoreSelection) }
    onVisibleChanged: {
        if (!visible && searching) { debounce.stop(); workbench.queryWorkspace("text", "", ++ticket, true); searching = false }
        else if (visible && mode === "search" && query && query.text) schedule()
    }
    Timer { id: debounce; interval: 180; onTriggered: workbench.queryWorkspace("text", query.text, side.ticket, false) }
    Connections {
        target: workbench
        function onWorkspaceQueryResult(reply) {
            if (reply.kind !== "text" || reply.ticket !== side.ticket || reply.root !== side.rootPath) return
            side.results = reply.rows
            side.fileCount = new Set(reply.rows.map(r => r.path)).size
            side.searching = !reply.done
            side.limited = reply.limited
            side.failure = reply.error
        }
    }
    ColumnLayout {
        anchors.fill: parent; spacing: 0
        RowLayout {
            Layout.fillWidth: true; Layout.preferredHeight: 34; Layout.leftMargin: 12; Layout.rightMargin: 4; spacing: 1
            Text { Layout.fillWidth: true; text: side.mode === "explorer" ? "资源管理器" : "搜索"; color: "#c3c9d1"; font.pixelSize: 12; elide: Text.ElideRight }
            IdeButton { visible: side.mode === "explorer"; glyph: "folder-open"; tip: "选择项目"; enabled: !workbench.busy; onClicked: side.action("chooseProject", {}) }
            IdeButton { visible: side.mode === "explorer"; glyph: "refresh-cw"; tip: "刷新文件树"; enabled: !workbench.busy; onClicked: side.action("refresh", {}) }
            IdeButton { visible: side.mode === "explorer"; glyph: "chevron-up"; tip: "全部折叠"; enabled: !workbench.busy; onClicked: side.action("collapseFolders", {}) }
        }
        Text {
            visible: side.mode === "explorer"; Layout.fillWidth: true; leftPadding: 12; rightPadding: 6; topPadding: 4; bottomPadding: 6
            text: side.rootPath.replace(/\\/g, "/").split("/").pop() || side.rootPath
            color: "#e0e5eb"; font.pixelSize: 12; font.weight: Font.DemiBold; elide: Text.ElideMiddle
        }
        AppListView {
            id: tree; objectName: "codeProjectTree"; visible: side.mode === "explorer"
            Layout.fillWidth: true; Layout.fillHeight: true; clip: true; keyNavigationEnabled: true
            model: side.viewData.rows || []; highlightMoveDuration: 0
            onModelChanged: Qt.callLater(side.restoreSelection)
            Keys.onReturnPressed: side.activateNode(currentIndex)
            Keys.onRightPressed: {
                const node = model[currentIndex]
                if (node && node.directory) {
                    if (!node.expanded) side.activateNode(currentIndex)
                    else if (model[currentIndex + 1]?.depth > node.depth) incrementCurrentIndex()
                }
            }
            Keys.onLeftPressed: {
                const node = model[currentIndex]
                if (!node) return
                if (node.expanded) side.activateNode(currentIndex)
                else for (let i = currentIndex - 1; i >= 0; --i) {
                    if (model[i].depth < node.depth) { currentIndex = i; break }
                }
            }
            delegate: ItemDelegate {
                id: item; required property int index; required property var modelData
                width: tree.rowWidth; height: 25; leftPadding: 8 + 13 * modelData.depth; rightPadding: 6
                // Basic ItemDelegate pads 12px top and bottom; with a 25px row that left a 1px content box,
                // so labels overflowed downward onto the next row's selection. Let the row own the height.
                topPadding: 0; bottomPadding: 0
                Accessible.name: modelData.title; Accessible.role: Accessible.TreeItem
                ToolTip.visible: hovered; ToolTip.delay: 700; ToolTip.text: modelData.id
                background: Rectangle {
                    color: item.modelData.path === side.activePath ? "#2e4056" : item.hovered ? "#2a3037" : "transparent"
                    border.width: tree.activeFocus && item.ListView.isCurrentItem ? 1 : 0; border.color: "#779cc9"
                }
                contentItem: RowLayout {
                    spacing: 4
                    Item { Layout.preferredWidth: 12; Layout.preferredHeight: 12
                        Glyph { anchors.fill: parent; visible: item.modelData.directory; name: item.modelData.expanded ? "chevron-down" : "chevron-right" }
                    }
                    Glyph {
                        Layout.preferredWidth: 15; Layout.preferredHeight: 15
                        name: item.modelData.directory ? (item.modelData.expanded ? "folder-open" : "folder") : /\.(py|qml|tsx?|jsx?|cs|cpp|rs)$/i.test(item.modelData.title) ? "file-code" : "file"
                        ink: item.modelData.directory ? "#c6ab7c" : "#88afd4"
                    }
                    Text { Layout.fillWidth: true; text: item.modelData.title; color: "#d0d6de"; font.pixelSize: 12; elide: Text.ElideRight }
                }
                onClicked: { tree.currentIndex = index; tree.forceActiveFocus(); side.activateNode(index) }
            }
        }
        TextField {
            id: query; objectName: "workspaceSearchInput"; visible: side.mode === "search"
            Layout.fillWidth: true; Layout.leftMargin: 8; Layout.rightMargin: 8; Layout.preferredHeight: 32
            Accessible.name: "工作区全文搜索"; placeholderText: "在文件中搜索"; placeholderTextColor: "#919aa7"; color: "#e1e5eb"
            leftPadding: 8; selectByMouse: true
            background: Rectangle { radius: 2; color: "#2c3037"; border.color: query.activeFocus ? "#649de9" : "#464c55" }
            onTextChanged: side.schedule()
            onAccepted: { if (hits.count) side.openHit(Math.max(0, hits.currentIndex)); else if (text) side.schedule() }
            Keys.onDownPressed: if (hits.count) { hits.forceActiveFocus(); hits.currentIndex = 0 }
        }
        Text {
            objectName: "workspaceSearchSummary"; visible: side.mode === "search"; Layout.fillWidth: true; leftPadding: 10; rightPadding: 6; topPadding: 7; bottomPadding: 7
            text: side.failure || (side.searching ? "正在搜索…" : side.limited ? "显示前 " + hits.count + " 处匹配" : hits.count ? hits.count + " 处匹配，" + side.fileCount + " 个文件" : query.text ? "没有匹配结果" : "")
            elide: Text.ElideRight; color: "#a5afbc"; font.pixelSize: 11
        }
        AppListView {
            id: hits; objectName: "workspaceSearchResults"; visible: side.mode === "search"; Layout.fillWidth: true; Layout.fillHeight: true
            clip: true; model: side.results; keyNavigationEnabled: true; highlightMoveDuration: 0
            section.property: "relative"; section.criteria: ViewSection.FullString
            section.delegate: Rectangle {
                required property string section; width: hits.rowWidth; height: 25; color: "#262a30"
                Text { anchors.fill: parent; leftPadding: 10; rightPadding: 6; text: section; elide: Text.ElideMiddle; color: "#e2e7ee"; font.pixelSize: 12; font.weight: Font.DemiBold; verticalAlignment: Text.AlignVCenter }
            }
            Keys.onReturnPressed: side.openHit(currentIndex)
            delegate: ItemDelegate {
                id: hit; required property int index; required property var modelData
                width: hits.rowWidth; height: 25; leftPadding: 12; rightPadding: 8; topPadding: 0; bottomPadding: 0
                Accessible.name: modelData.relative + " 行 " + modelData.line + " 列 " + modelData.column + " " + modelData.before + modelData.token + modelData.after
                ToolTip.text: Accessible.name; ToolTip.visible: hovered; ToolTip.delay: 650
                background: Rectangle { color: hit.ListView.isCurrentItem ? "#304156" : hit.hovered ? "#292e35" : "transparent" }
                contentItem: RowLayout {
                    Text { text: hit.modelData.line; color: "#94a8c1"; font.pixelSize: 11 }
                    Text { Layout.fillWidth: true; text: side.escaped(hit.modelData.before) + "<b><font color='#f3c785'>" + side.escaped(hit.modelData.token) + "</font></b>" + side.escaped(hit.modelData.after); textFormat: Text.StyledText; color: "#d4dae2"; elide: Text.ElideRight; font.pixelSize: 12 }
                }
                onClicked: { hits.currentIndex = index; side.openHit(index) }
            }
        }
    }
    Rectangle { anchors.right: parent.right; height: parent.height; width: 1; color: "#343940" }
}
