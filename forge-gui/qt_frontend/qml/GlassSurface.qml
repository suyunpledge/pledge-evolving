import QtQuick

Item {
    id: glass
    required property var material
    property real radius: 18
    property real emphasis: 0
    property real hoverAmount: 0
    property point pointer: Qt.point(width / 2, 0)
    property color tint: "#24313b49"
    Behavior on hoverAmount { NumberAnimation { duration: glass.material.reduced ? 0 : 150 } }
    Behavior on emphasis { NumberAnimation { duration: glass.material.reduced ? 0 : 150 } }
    readonly property bool useShader: material.hardware && !material.solid
    // mapToItem alone does not subscribe to ancestor transforms. Read the chain
    // explicitly: Popup reparents its content into Overlay only when it opens.
    readonly property point scenePoint: {
        const epoch = material.epoch
        const transforms = []
        for (let item = glass; item; item = item.parent)
            transforms.push(item.x, item.y, item.width, item.height, item.scale, item.rotation)
        return mapToItem(material.scene, 0, 0)
    }
    Rectangle {
        anchors.fill: parent
        visible: !glass.useShader
        radius: glass.radius
        color: glass.emphasis > 0 ? "#343e4b" : "#252d38"
        border.color: "#68717f"
        border.width: glass.activeFocus ? 2 : 1
    }
    ShaderEffect {
        id: lens
        objectName: "glassShader"
        visible: glass.useShader
        x: -12; y: -12; width: glass.width + 24; height: glass.height + 24
        property var backgroundTexture: glass.material.raw
        property var blurredTexture: glass.material.blurred
        property vector2d surfaceSize: Qt.vector2d(glass.width, glass.height)
        property vector2d sceneSize: Qt.vector2d(glass.material.scene.width, glass.material.scene.height)
        property vector2d sceneOrigin: Qt.vector2d(glass.scenePoint.x, glass.scenePoint.y)
        property vector2d pointerPosition: glass.material.reduced ? Qt.vector2d(glass.width / 2, 0) : Qt.vector2d(glass.pointer.x, glass.pointer.y)
        property color tintColor: glass.tint
        property real cornerRadius: glass.radius
        property real shadowPadding: 12
        property real refractionStrength: 5 + glass.material.strength * 18
        property real frostAmount: 0.85 - glass.material.strength * 0.42
        property real hoverAmount: glass.hoverAmount
        property real selectedAmount: glass.emphasis
        fragmentShader: "../shaders/glass.frag.qsb"
        onStatusChanged: if (status === ShaderEffect.Error) console.error("Glass shader:", log)
    }
}
