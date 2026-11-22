import QtQuick

Item {
    id: glyph
    property string name: "message-square"
    property color ink: Theme.textSecondary
    property bool glowing: false
    width: 18; height: 18

    Loader {
        anchors.centerIn: parent
        width: glyph.width * 2.1
        height: glyph.height * 2.1
        active: glyph.glowing
        visible: glyph.glowing
        sourceComponent: Canvas {
            property color glowColor: glyph.ink
            onGlowColorChanged: requestPaint()
            onPaint: {
                const ctx = getContext("2d")
                ctx.clearRect(0, 0, width, height)
                const cx = width / 2, cy = height / 2
                const r = Math.min(width, height) / 2
                const g = ctx.createRadialGradient(cx, cy, 1, cx, cy, r)
                g.addColorStop(0.0, Qt.rgba(glowColor.r, glowColor.g, glowColor.b, 0.32))
                g.addColorStop(0.55, Qt.rgba(glowColor.r, glowColor.g, glowColor.b, 0.09))
                g.addColorStop(1.0, Qt.rgba(glowColor.r, glowColor.g, glowColor.b, 0.0))
                ctx.fillStyle = g
                ctx.fillRect(0, 0, width, height)
            }
        }
    }

    Image {
        anchors.fill: parent
        source: controller.iconSource(glyph.name, glyph.ink.toString())
        sourceSize.width: Math.max(1, Math.round(glyph.width * Screen.devicePixelRatio))
        sourceSize.height: Math.max(1, Math.round(glyph.height * Screen.devicePixelRatio))
        fillMode: Image.PreserveAspectFit
        smooth: true
        mipmap: true
    }
}
