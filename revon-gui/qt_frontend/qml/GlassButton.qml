import QtQuick
import QtQuick.Controls

AbstractButton {
    id: control
    required property var material
    property string glyph: ""
    property bool selected: false
    property bool primary: false
    property bool quiet: false
    property bool flat: false
    // Destructive/irreversible choice: danger-coloured text so it never reads as the default.
    property bool danger: false
    property string tip: ""
    property real cornerRadius: 12
    property color ink: primary ? Theme.accentInk : danger ? Theme.dangerText : selected ? Theme.textPrimary : "#dce3ec"
    property color glyphInk: primary ? Theme.accentInk : danger ? Theme.danger : selected ? Theme.accent : ink
    implicitHeight: 36
    implicitWidth: Math.max(36, content.implicitWidth + 24)
    hoverEnabled: true
    focusPolicy: Qt.StrongFocus
    opacity: enabled ? 1 : 0.42
    Accessible.name: text || tip
    Accessible.role: Accessible.Button
    ToolTip.visible: hovered && tip.length > 0
    ToolTip.text: tip
    ToolTip.delay: 650
    background: Item {
    GlassSurface {
        anchors.fill: parent
        material: control.material
        flat: control.flat
        radius: control.cornerRadius
        tint: control.flat ? "#25292f" : control.primary ? Theme.accentTint : control.selected ? Theme.selectedTint : Theme.glassTint
        visible: !control.quiet || control.hovered || control.selected || control.primary || control.visualFocus
        emphasis: control.primary ? 1.15 : control.selected ? 1 : control.visualFocus ? 0.65 : 0
        hoverAmount: control.hovered || control.down ? 1 : 0
        pointer: hover.point.position
    }
    // Primary actions get a solid fill on top of the glass so they win the
    // preattentive contest against every tinted secondary control.
    Rectangle {
        objectName: "primaryFill"
        anchors.fill: parent
        visible: control.primary && !control.flat
        radius: control.cornerRadius
        color: control.hovered || control.down ? Theme.primaryFillHover : Theme.primaryFill
        border.color: Theme.primaryBorder; border.width: 1
        opacity: 0.92
    }
    // Keyboard focus ring (WCAG 2.4.7): drawn outside the control so it shows on any tint.
    Rectangle {
        objectName: "focusRing"
        visible: control.visualFocus
        anchors.fill: parent; anchors.margins: -3
        radius: control.cornerRadius + 3
        color: "transparent"
        border.color: Theme.focusRing; border.width: Theme.focusRingWidth
    }
    }
    HoverHandler { id: hover; cursorShape: Qt.PointingHandCursor }
    contentItem: Item {
        id: content
        readonly property bool compact: control.width > 0 && control.width < 80 && control.text.length > 0 && control.glyph.length > 0
        implicitWidth: contentRow.implicitWidth
        implicitHeight: 18
        Row {
        id: contentRow
        anchors.centerIn: parent
        spacing: control.text.length > 0 && control.glyph.length > 0 ? (content.compact ? 4 : 8) : 0
        Glyph {
            visible: control.glyph.length > 0
            width: visible ? (content.compact ? 14 : 17) : 0; height: content.compact ? 14 : 17
            anchors.verticalCenter: parent.verticalCenter
            name: control.glyph; ink: control.glyphInk
            glowing: control.selected || control.primary
        }
        Text {
            text: control.text
            color: control.ink
            font.pixelSize: content.compact ? 12 : 13
            anchors.verticalCenter: parent.verticalCenter
        }
        }
    }
    Keys.onReturnPressed: clicked()
}
