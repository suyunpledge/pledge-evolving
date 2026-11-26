import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: field
    required property var material
    required property var definition
    property var value: definition.value
    property bool expanded: !definition.advanced
    objectName: "workbenchField_" + definition.key
    signal edited(var value)
    spacing: 7
    Text { visible: !field.definition.advanced; text: field.definition.label; color: "#b9c5d1"; font.pixelSize: 12; Layout.fillWidth: true; wrapMode: Text.Wrap }
    GlassButton { visible: !!field.definition.advanced; material: field.material; text: field.definition.label; glyph: field.expanded ? "chevron-up" : "chevron-down"; onClicked: field.expanded = !field.expanded }
    Loader {
        Layout.fillWidth: true
        visible: field.expanded
        active: field.expanded
        Layout.preferredHeight: field.definition.kind === "code" ? 230 : field.definition.kind === "multiline" ? 110 : 42
        sourceComponent: field.definition.kind === "bool" ? booleanField : field.definition.kind === "choice" ? choiceField : field.definition.kind === "multiline" || field.definition.kind === "code" ? areaField : textField
    }
    Component {
        id: booleanField
        GlassButton { material: field.material; text: field.value ? "已开启" : "已关闭"; selected: !!field.value; width: 104; onClicked: {field.value = !field.value; field.edited(field.value)} }
    }
    Component {
        id: choiceField
        ComboBox {
            id: choice
            width: parent.width; implicitHeight: 40
            model: (field.definition.options || []).map(o => typeof o === "string" ? {label:o, value:o} : o)
            textRole: "label"; valueRole: "value"
            currentIndex: model.findIndex(o => o.value === field.value)
            onActivated: {field.value = currentValue; field.edited(currentValue)}
            contentItem: Text { text: choice.displayText; color: "#eef2f6"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight; leftPadding: 12; rightPadding: 28; font.pixelSize: 13 }
            background: GlassSurface { material: field.material; radius: 11; emphasis: choice.activeFocus ? 0.7 : 0 }
            popup: Popup {
                y: choice.height + 5; width: choice.width; height: Math.min(300, contentItem.implicitHeight + 12); padding: 6
                background: GlassSurface { material: field.material; radius: 12; tint: "#e3202934" }
                contentItem: ListView { clip: true; implicitHeight: contentHeight; model: choice.popup.visible ? choice.delegateModel : null; currentIndex: choice.highlightedIndex; ScrollBar.vertical: AppScrollBar {} }
            }
            delegate: ItemDelegate {
                width: choice.width - 12; height: 36
                contentItem: Text { text: modelData.label; color: "#e8edf4"; elide: Text.ElideRight; verticalAlignment: Text.AlignVCenter; font.pixelSize: 13 }
                background: GlassSurface { material: field.material; radius: 8; visible: highlighted; emphasis: 0.6 }
                highlighted: choice.highlightedIndex === index
            }
        }
    }
    Component {
        id: textField
        TextField {
            id: input
            objectName: "field_" + field.definition.key
            width: parent.width; implicitHeight: 42; text: String(field.value ?? "")
            color: "#edf1f5"; selectionColor: "#526477"; selectedTextColor: "white"; font.pixelSize: 14
            echoMode: field.definition.kind === "secret" ? TextInput.Password : TextInput.Normal
            selectByMouse: true; leftPadding: 12; rightPadding: 12
            background: GlassSurface { material: field.material; radius: 11; emphasis: input.activeFocus ? 0.65 : 0 }
            onTextEdited: {field.value = text; field.edited(text)}
            readonly property point origin: { for (let p = input; p; p = p.parent) { const g = [p.x, p.y, p.width, p.height] }; return mapToItem(null, 0, 0) }
            function refreshIme() { if (activeFocus) controller.syncInputMethod(mapToItem(null, 0, 0), width, height) }
            onOriginChanged: Qt.callLater(refreshIme)
            onActiveFocusChanged: Qt.callLater(refreshIme)
            onCursorRectangleChanged: Qt.callLater(refreshIme)
        }
    }
    Component {
        id: areaField
        Item {
            width: parent.width; implicitHeight: field.definition.kind === "code" ? 230 : 110
            GlassSurface { anchors.fill: parent; material: field.material; radius: 13; emphasis: area.editor.activeFocus ? 0.65 : 0; tint: "#30202a34" }
            ComposerText {
                id: area; anchors.fill: parent; anchors.margins: 12; submitOnEnter: false
                editorName: "field_" + field.definition.key
                text: String(field.value ?? "")
                editor.font.family: field.definition.kind === "code" ? "Cascadia Code" : uiFont.family
                editor.font.pixelSize: field.definition.kind === "code" ? 13 : 14
                onTextChanged: { if (editor.activeFocus) {field.value = text; field.edited(text)} }
            }
        }
    }
}
