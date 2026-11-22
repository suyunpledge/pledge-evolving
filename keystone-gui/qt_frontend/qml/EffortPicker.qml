import QtQuick
import QtQuick.Controls

// 思考强度：五个吸附档位的滑杆（与 Tk 版一致，末档「沉思」）。只有当前模型可用的档位能选中；
// 模型不支持分档时只剩「默认」与「沉思」，并说明原因。
Popup {
    id: picker
    objectName: "effortPicker"
    required property var material
    required property var service
    readonly property var levels: ["off", "low", "medium", "high", "contemplate"]
    readonly property var labels: ({off: "默认", low: "轻量", medium: "标准", high: "深入", contemplate: "沉思"})
    readonly property var notes: ({
        off: "不发送思考参数，由模型自行决定。",
        low: "响应更快，适合简单问答与改写。",
        medium: "速度与深度均衡，适合日常任务。",
        high: "更充分地推理，适合复杂代码与规划。",
        contemplate: "最高强度推理，并开启 Keystone 任务沉思：任务先多轮思考、收敛后再动手。"
    })
    readonly property var allowed: service.reasoningLevels
    readonly property string current: service.effectiveReasoning
    readonly property bool usable: !service.busy
    focus: true
    width: 340; padding: 18
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: GlassSurface { material: picker.material; radius: 20; emphasis: 0.4 }

    function nearestAllowed(index) {
        let best = picker.allowed[0], gap = 99
        for (let i = 0; i < picker.allowed.length; ++i) {
            const d = Math.abs(picker.levels.indexOf(picker.allowed[i]) - index)
            if (d < gap) { gap = d; best = picker.allowed[i] }
        }
        return best
    }
    function choose(level) {
        if (!picker.usable || picker.allowed.indexOf(level) < 0) return
        picker.service.setReasoning(level)
        track.value = picker.levels.indexOf(picker.current)
    }

    contentItem: Column {
        spacing: 12
        Row {
            width: parent.width; spacing: 8
            Glyph { name: "gauge"; ink: Theme.accent; width: 17; height: 17; anchors.verticalCenter: parent.verticalCenter }
            Text { text: "思考强度"; color: Theme.textPrimary; font.pixelSize: 15; font.weight: Font.DemiBold; anchors.verticalCenter: parent.verticalCenter }
            Item { width: parent.width - 220; height: 1 }
            Text {
                width: 110; horizontalAlignment: Text.AlignRight; elide: Text.ElideRight
                text: picker.service.model; color: Theme.textMuted; font.pixelSize: 12
                anchors.verticalCenter: parent.verticalCenter
            }
        }
        Slider {
            id: track
            objectName: "effortSlider"
            width: parent.width; height: 34
            from: 0; to: 4; stepSize: 1; snapMode: Slider.SnapAlways
            enabled: picker.usable
            value: picker.levels.indexOf(picker.current)
            onMoved: {
                const level = picker.nearestAllowed(Math.round(value))
                if (level !== picker.service.reasoning) picker.service.setReasoning(level)
            }
            onPressedChanged: if (!pressed) value = picker.levels.indexOf(picker.current)
            Accessible.name: "思考强度"
            background: Item {
                x: track.leftPadding; y: track.topPadding + track.availableHeight / 2 - 5
                width: track.availableWidth; height: 10
                GlassSurface { anchors.fill: parent; material: picker.material; radius: 5; tint: "#3a1c2533" }
                Rectangle {
                    height: parent.height; radius: 5
                    width: Math.max(10, track.visualPosition * parent.width)
                    opacity: track.enabled ? 0.9 : 0.25
                    gradient: Gradient {
                        orientation: Gradient.Horizontal
                        GradientStop { position: 0; color: "#4f7fd8" }
                        GradientStop { position: 0.75; color: "#8f6ef0" }
                        GradientStop { position: 1; color: Theme.ember }
                    }
                }
                Repeater {
                    model: picker.levels
                    Rectangle {
                        required property string modelData
                        required property int index
                        readonly property bool ok: picker.allowed.indexOf(modelData) >= 0
                        x: index / 4 * (parent.width - 6); y: 2
                        width: 6; height: 6; radius: 3
                        color: ok ? "#e7edf5" : "#4a5566"; opacity: ok ? 0.85 : 0.6
                    }
                }
            }
            handle: Rectangle {
                x: track.leftPadding + track.visualPosition * (track.availableWidth - width)
                y: track.topPadding + track.availableHeight / 2 - height / 2
                width: 22; height: 22; radius: 11
                color: track.enabled ? "#eef2f7" : "#6d7888"
                border.color: track.pressed ? Theme.accent : "#7f919ca9"; border.width: track.pressed ? 2 : 1
            }
        }
        Row {
            objectName: "effortLabels"
            width: parent.width
            Repeater {
                model: picker.levels
                Text {
                    required property string modelData
                    required property int index
                    readonly property bool ok: picker.allowed.indexOf(modelData) >= 0
                    width: parent.width / 5
                    horizontalAlignment: index === 0 ? Text.AlignLeft : index === 4 ? Text.AlignRight : Text.AlignHCenter
                    text: picker.labels[modelData]
                    color: picker.current === modelData ? (modelData === "contemplate" ? Theme.ember : Theme.textPrimary) : ok ? Theme.textSecondary : Theme.textDim
                    font.pixelSize: 12; font.weight: picker.current === modelData ? Font.DemiBold : Font.Normal
                    font.strikeout: !ok && picker.service.reasoningSupported
                    TapHandler { enabled: parent.ok && picker.usable; onTapped: picker.choose(parent.modelData) }
                    HoverHandler { enabled: parent.ok && picker.usable; cursorShape: Qt.PointingHandCursor }
                }
            }
        }
        Text {
            width: parent.width; wrapMode: Text.Wrap
            text: picker.notes[picker.current] || ""
            color: Theme.textSecondary; font.pixelSize: 12
        }
        Text {
            objectName: "effortHint"
            width: parent.width; wrapMode: Text.Wrap
            visible: text.length > 0
            text: picker.service.reasoningSupported
                  ? (picker.service.reasoning !== picker.current
                     ? "所选「" + picker.labels[picker.service.reasoning] + "」此模型不支持，将按「" + picker.labels[picker.current] + "」发送。" + " " + picker.service.reasoningHint
                     : picker.service.reasoningHint)
                  : picker.service.reasoningHint + (picker.current === "contemplate" ? "「沉思」仍会开启任务沉思。" : "")
            color: picker.service.reasoningSupported ? Theme.textMuted : Theme.warningText; font.pixelSize: 11
        }
    }
}
