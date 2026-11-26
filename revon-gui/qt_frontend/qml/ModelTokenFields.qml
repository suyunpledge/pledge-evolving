import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: limits
    required property var material
    property bool english: false
    property string legacyInput: ""
    signal edited()
    Layout.fillWidth: true
    spacing: 5

    function load(values) {
        context.text = values?.contextWindow || ""
        output.text = values?.outputTokenBudget || ""
        context.automatic = values?.automaticContext || ""
        output.automatic = values?.automaticOutput || ""
        legacyInput = values?.legacyInput || ""
    }
    function values() {
        return {contextWindow: context.text.trim(), outputTokenBudget: output.text.trim()}
    }
    component LimitRow: ColumnLayout {
        id: row
        required property string fieldName
        required property string label
        required property var presets
        property string automatic: ""
        property alias text: entry.text
        Layout.fillWidth: true; spacing: 4
        Text { text: row.label; color: Theme.textSecondary; font.pixelSize: 12 }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            TextField {
                id: entry; objectName: row.fieldName
                Layout.preferredWidth: 130; Layout.minimumWidth: 84
                Layout.preferredHeight: 36; Layout.alignment: Qt.AlignTop
                selectByMouse: true; leftPadding: 10; rightPadding: 6
                color: Theme.textPrimary; placeholderTextColor: Theme.textMuted
                Accessible.name: row.label; inputMethodHints: Qt.ImhNoPredictiveText
                placeholderText: (limits.english ? "Auto" : "自动") + (row.automatic ? " · " + row.automatic : "")
                background: GlassSurface { material: limits.material; radius: 10; emphasis: entry.activeFocus ? 0.6 : 0 }
                onTextEdited: limits.edited()
            }
            Flow {
                Layout.fillWidth: true; Layout.minimumWidth: 56; Layout.preferredWidth: 1
                Layout.preferredHeight: implicitHeight; spacing: 4
                Repeater {
                    model: row.presets
                    GlassButton {
                        required property int modelData
                        objectName: row.fieldName + "Preset" + modelData
                        material: limits.material; height: 32; width: Math.max(38, implicitWidth - 8)
                        text: modelData === 1048576 ? "1M" : modelData === 384000 ? "384K" : (modelData / 1024) + "K"
                        tip: modelData + " tokens"; Accessible.name: row.label + " " + text
                        selected: entry.text.trim() === String(modelData) || entry.text.trim().toUpperCase() === text
                        Accessible.checkable: true; Accessible.checked: selected
                        onClicked: { entry.text = String(modelData); limits.edited() }
                    }
                }
            }
        }
    }
    LimitRow {
        id: context; fieldName: "contextWindowField"; label: limits.english ? "Context length" : "上下文长度"
        presets: [65536, 131072, 262144, 1048576]
    }
    LimitRow {
        id: output; fieldName: "outputTokenBudgetField"; label: limits.english ? "Maximum output length" : "最大输出长度"
        presets: [4096, 8192, 16384, 32768, 65536, 131072, 384000]
    }
    Text {
        Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 11
        text: limits.english ? "Lengths are in tokens. Input and output share the context window. Automatic output fits the space left after input. Leave blank for automatic."
                             : "单位为 token。输入和输出共用上下文，自动分配的输出不会超过剩余空间。留空使用自动值。"
    }
    Text {
        Layout.fillWidth: true; visible: limits.legacyInput.length > 0
        wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 11
        text: limits.english ? "Previous input limit: " + limits.legacyInput + ". Set a context length to replace it."
                             : "旧版输入上限为 " + limits.legacyInput + "。填写上下文长度并保存即可替换。"
    }
}
