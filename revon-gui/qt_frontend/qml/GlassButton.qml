import QtQuick
import QtQuick.Controls

AbstractButton {
    id: control
    required property var material
    property string glyph: ""
    property bool selected: false
    property bool primary: false
    // Hero action in the aurora gradient (at most one or two per screen).
    property bool brand: false
    // List rows: left-aligned content and an indicator rail when selected.
    property bool leftAligned: false
    property bool rail: false
    property bool quiet: false
    property bool flat: false
    // Destructive/irreversible choice: danger-coloured text so it never reads as the default.
    property bool danger: false
    property string tip: ""
    property real maximumTextWidth: -1
    property bool wrapLabel: false
    property real cornerRadius: 12
    property color ink: brand ? "#ffffff" : primary ? Theme.accentInk : danger ? Theme.dangerText : selected ? Theme.textPrimary : "#dce3ec"
    property color glyphInk: brand ? "#ffffff" : primary ? Theme.accentInk : danger ? Theme.danger : selected ? Theme.accent : ink
    implicitHeight: wrapLabel ? Math.max(36, contentRow.implicitHeight + 12) : 36
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
        visible: !control.brand && (!control.quiet || control.hovered || control.selected || control.primary || control.visualFocus)
        emphasis: control.primary ? 1.15 : control.selected ? 1 : control.visualFocus ? 0.65 : 0
        hoverAmount: control.hovered || control.down ? 1 : 0
        pointer: hover.point.position
    }
    // Primary actions get a solid fill on top of the glass so they win the
    // preattentive contest against every tinted secondary control.
    Rectangle {
        objectName: "primaryFill"
        anchors.fill: parent
        visible: control.primary && !control.brand && !control.flat
        radius: control.cornerRadius
        color: control.hovered || control.down ? Theme.primaryFillHover : Theme.primaryFill
        border.color: Theme.primaryBorder; border.width: 1
        opacity: 0.92
    }
    // Brand fill: the aurora gradient with a soft glow underneath.
    Rectangle {
        objectName: "brandGlow"
        visible: control.brand && control.enabled
        anchors.fill: parent; anchors.margins: -4
        radius: control.cornerRadius + 4
        color: "transparent"; border.color: Theme.brandGlow; border.width: 4
        opacity: control.hovered ? 0.9 : 0.5
    }
    Rectangle {
        objectName: "brandFill"
        anchors.fill: parent
        visible: control.brand
        radius: control.cornerRadius
        border.color: Theme.brandBorder; border.width: 1
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0; color: control.hovered || control.down ? Theme.brandStartHover : Theme.brandStart }
            GradientStop { position: 1; color: control.hovered || control.down ? Theme.brandEndHover : Theme.brandEnd }
        }
    }
    // Selected list row: a short aurora rail on the leading edge.
    Rectangle {
        objectName: "selectionRail"
        visible: control.rail && control.selected
        x: 3; width: 3; height: Math.min(20, parent.height - 14); radius: 1.5
        anchors.verticalCenter: parent.verticalCenter
        color: Theme.indicator
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
        anchors.verticalCenter: parent.verticalCenter
        x: control.leftAligned ? 8 : (parent.width - width) / 2
        spacing: control.text.length > 0 && control.glyph.length > 0 ? (content.compact ? 4 : 8) : 0
        Glyph {
            visible: control.glyph.length > 0
            width: visible ? (content.compact ? 14 : 17) : 0; height: content.compact ? 14 : 17
            anchors.verticalCenter: parent.verticalCenter
            name: control.glyph; ink: control.glyphInk
            glowing: control.selected || control.primary || control.brand
        }
        Text {
            objectName: "glassButtonLabel"
            text: control.text
            width: control.maximumTextWidth < 0 ? implicitWidth : Math.min(implicitWidth, control.maximumTextWidth)
            wrapMode: control.wrapLabel ? Text.WrapAnywhere : Text.NoWrap
            elide: control.wrapLabel ? Text.ElideNone : Text.ElideRight
            textFormat: Text.PlainText
            color: control.ink
            font.pixelSize: content.compact ? 12 : 13
            anchors.verticalCenter: parent.verticalCenter
        }
        }
    }
    Keys.onReturnPressed: clicked()
}
