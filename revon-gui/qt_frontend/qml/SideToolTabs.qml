import QtQuick

// Browser and Office share one side panel; these tabs pick which one is shown.
Row {
    id: tabs
    required property var material
    spacing: 4
    Repeater {
        model: [{key: "browser", label: "浏览器", glyph: "globe"},
                {key: "office", label: "办公", glyph: "panels-top-left"}]
        GlassButton {
            required property var modelData
            objectName: "sideTool_" + modelData.key
            material: tabs.material; quiet: true; height: 32
            glyph: modelData.glyph; text: modelData.label
            selected: workbench.sideTool === modelData.key
            tip: modelData.key === "office" ? "内置办公：查看和编辑 Work 生成的文档、表格、演示" : "Work 浏览器"
            onClicked: workbench.showSideTool(modelData.key)
        }
    }
}
