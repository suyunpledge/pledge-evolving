import QtQuick
import QtQuick.Window

Window {
    id: probe
    width: 520; height: 340; visible: true; color: "#131924"
    property real strength: 0.0
    property real phase: 0
    property var material: QtObject {
        property var scene: probe.contentItem
        property var raw: captured
        property var blurred: captured
        property bool hardware: true
        property bool solid: false
        property real strength: probe.strength
        property bool reduced: true
        property real epoch: probe.width + probe.height
    }
    Item {
        id: pattern; anchors.fill: parent
        Repeater {
            model: 28
            Rectangle { required property int index; x: index * 22 + probe.phase; width: 11; height: pattern.height; color: index % 2 ? "#8a9eaf" : "#75684f" }
        }
    }
    ShaderEffectSource { id: captured; sourceItem: pattern; hideSource: false; visible: false; live: true }
    GlassSurface { x: 70; y: 60; width: 380; height: 220; radius: 28; material: probe.material }
}
