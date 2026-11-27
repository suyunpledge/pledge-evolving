import QtQuick
import QtQuick.Controls

Text {
    id: footer
    required property var usage
    readonly property int reports: usage.requests || 0
    function number(value) {
        return typeof value === "number" ? value.toLocaleString(Qt.locale(), "f", 0) : "—"
    }
    function amount(key) {
        const count = usage[key + "Reports"] || 0
        return count ? (count < reports ? "≥" : "") + number(usage[key]) : "—"
    }
    readonly property string hit: usage.cacheInput > 0
        ? (100 * usage.cached / usage.cacheInput).toFixed(1) + "%" : "—"
    text: "上下文 " + number(usage.context) + " / " + number(usage.limit) +
          "    本轮 token  输入 " + amount("input") + " · 输出 " + amount("output") +
          "    缓存命中 " + hit + (usage.cacheReports && usage.cacheReports < reports ? "*" : "")
    readonly property string detail: "上下文为最近一次执行请求的输入 / 模型窗口上限。\n" +
        "本轮累计输入 " + amount("input") + "、输出 " + amount("output") +
        " token；包含已完成的规划、执行、沉思和反思请求。\n缓存命中 " +
        number(usage.cached) + " / " + number(usage.cacheInput) + " 个输入 token（" +
        (usage.cacheReports || 0) + " / " + reports + " 次请求报告）。\n— 表示没有数据，≥ 表示部分已知用量。"
    font.pixelSize: 11; color: Theme.textMuted; textFormat: Text.PlainText
    wrapMode: Text.Wrap
    Accessible.name: text
    Accessible.description: detail
    HoverHandler { id: hover }
    ToolTip.visible: hover.hovered || activeFocus
    ToolTip.text: detail
    ToolTip.delay: 350
    activeFocusOnTab: true
}
