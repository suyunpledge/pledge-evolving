import QtQuick

Item {
    id: backdrop
    property string mode: "work" // "work", "code", "chat"
    property real sideInset: 252
    signal painted()

    onWidthChanged: canvas.requestPaint()
    onHeightChanged: canvas.requestPaint()
    onModeChanged: canvas.requestPaint()
    onSideInsetChanged: canvas.requestPaint()

    Canvas {
        id: canvas
        anchors.fill: parent
        renderStrategy: Canvas.Threaded
        onPainted: backdrop.painted()

        onPaint: {
            const ctx = getContext("2d")
            const w = width
            const h = height
            if (w <= 0 || h <= 0) return
            ctx.clearRect(0, 0, w, h)

            const isCode = backdrop.mode === "code"
            const isWork = backdrop.mode === "work"
            const areaX = backdrop.sideInset
            const areaW = Math.max(200, w - areaX)
            const cx = areaX + areaW * 0.54
            const cy = h * 0.50

            // 1. Delicate sweeping orbital arcs framing the main canvas
            function drawGradientArc(x, y, r, startAngle, endAngle, c0, c1, c2, lineW) {
                const grad = ctx.createLinearGradient(
                    x + Math.cos(startAngle) * r, y + Math.sin(startAngle) * r,
                    x + Math.cos(endAngle) * r, y + Math.sin(endAngle) * r
                )
                grad.addColorStop(0.0, c0)
                grad.addColorStop(0.5, c1)
                grad.addColorStop(1.0, c2)
                ctx.save()
                ctx.beginPath()
                ctx.arc(x, y, r, startAngle, endAngle, false)
                ctx.strokeStyle = grad
                ctx.lineWidth = lineW
                ctx.stroke()
                ctx.restore()
            }

            const baseR = Math.min(areaW, h) * 0.62
            drawGradientArc(
                cx + areaW * 0.14, cy + h * 0.18,
                baseR * 1.15,
                Math.PI * 1.08, Math.PI * 1.62,
                "rgba(140, 190, 255, 0.0)",
                isCode ? "rgba(130, 195, 255, 0.12)" : "rgba(155, 195, 255, 0.105)",
                "rgba(232, 175, 125, 0.0)",
                1.2
            )

            drawGradientArc(
                cx - areaW * 0.12, cy - h * 0.16,
                baseR * 0.92,
                Math.PI * 0.06, Math.PI * 0.52,
                "rgba(165, 150, 240, 0.0)",
                isWork ? "rgba(225, 175, 130, 0.095)" : "rgba(145, 175, 245, 0.105)",
                "rgba(120, 180, 245, 0.0)",
                1.0
            )

            if (isCode) {
                drawGradientArc(
                    cx + areaW * 0.05, cy + h * 0.04,
                    baseR * 0.72,
                    Math.PI * 1.25, Math.PI * 1.85,
                    "rgba(110, 185, 245, 0.0)",
                    "rgba(135, 200, 255, 0.08)",
                    "rgba(135, 200, 255, 0.0)",
                    0.9
                )
            }

            // 2. Soft 4-point starlight glints & tiny constellation nodes
            function drawStarGlint(x, y, radius, colorRgb, alpha) {
                ctx.save()
                // Soft radial halo
                const halo = ctx.createRadialGradient(x, y, 0, x, y, radius * 3.2)
                halo.addColorStop(0.0, `rgba(${colorRgb}, ${alpha * 0.45})`)
                halo.addColorStop(1.0, `rgba(${colorRgb}, 0.0)`)
                ctx.fillStyle = halo
                ctx.beginPath()
                ctx.arc(x, y, radius * 3.2, 0, Math.PI * 2)
                ctx.fill()

                // Crisp 4-point diamond glint
                ctx.fillStyle = `rgba(${colorRgb}, ${alpha})`
                ctx.beginPath()
                const inner = radius * 0.18
                ctx.moveTo(x, y - radius)
                ctx.quadraticCurveTo(x + inner, y - inner, x + radius, y)
                ctx.quadraticCurveTo(x + inner, y + inner, x, y + radius)
                ctx.quadraticCurveTo(x - inner, y + inner, x - radius, y)
                ctx.quadraticCurveTo(x - inner, y - inner, x, y - radius)
                ctx.closePath()
                ctx.fill()
                ctx.restore()
            }

            function drawConstellationNode(x, y, r, colorRgb, alpha) {
                ctx.save()
                ctx.fillStyle = `rgba(${colorRgb}, ${alpha})`
                ctx.beginPath()
                ctx.arc(x, y, r, 0, Math.PI * 2)
                ctx.fill()
                ctx.strokeStyle = `rgba(${colorRgb}, ${alpha * 0.45})`
                ctx.lineWidth = 0.8
                ctx.beginPath()
                ctx.arc(x, y, r * 2.6, 0, Math.PI * 2)
                ctx.stroke()
                ctx.restore()
            }

            const coolRgb = "175, 212, 255"
            const warmRgb = isCode ? "175, 195, 255" : "242, 196, 152"
            const lavenderRgb = "194, 182, 250"

            drawStarGlint(areaX + areaW * 0.84, h * 0.19, 7.5, warmRgb, 0.34)
            drawStarGlint(areaX + areaW * 0.77, h * 0.27, 4.5, coolRgb, 0.26)
            drawStarGlint(areaX + areaW * 0.18, h * 0.74, 6.5, coolRgb, 0.28)
            drawStarGlint(areaX + areaW * 0.26, h * 0.22, 4.8, lavenderRgb, 0.24)
            drawStarGlint(areaX + areaW * 0.68, h * 0.80, 5.2, coolRgb, 0.22)

            drawConstellationNode(areaX + areaW * 0.89, h * 0.25, 2.0, warmRgb, 0.30)
            drawConstellationNode(areaX + areaW * 0.14, h * 0.67, 1.8, coolRgb, 0.26)
            drawConstellationNode(areaX + areaW * 0.34, h * 0.17, 1.6, lavenderRgb, 0.22)
        }
    }
}
