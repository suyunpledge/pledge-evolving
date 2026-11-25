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
            const areaX = isCode ? 0 : backdrop.sideInset
            const areaW = Math.max(240, w - areaX)
            const cx = areaX + areaW * 0.54
            const cy = h * 0.50

            // 1. Delicate sweeping celestial orbital arc
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

            const baseR = Math.min(areaW, h) * 0.64
            drawGradientArc(
                cx + areaW * 0.16, cy + h * 0.20,
                baseR * 1.18,
                Math.PI * 1.10, Math.PI * 1.60,
                "rgba(120, 225, 220, 0.0)",
                isCode ? "rgba(125, 215, 240, 0.095)" : "rgba(145, 210, 245, 0.085)",
                "rgba(185, 165, 250, 0.0)",
                1.1
            )

            // 2. Whisper-thin shooting star streak in the upper sky
            function drawShootingStar(x1, y1, x2, y2, alpha) {
                const grad = ctx.createLinearGradient(x1, y1, x2, y2)
                grad.addColorStop(0.0, "rgba(210, 232, 255, 0.0)")
                grad.addColorStop(0.75, `rgba(210, 232, 255, ${alpha * 0.45})`)
                grad.addColorStop(1.0, `rgba(240, 248, 255, ${alpha})`)
                ctx.save()
                ctx.beginPath()
                ctx.moveTo(x1, y1)
                ctx.lineTo(x2, y2)
                ctx.strokeStyle = grad
                ctx.lineWidth = 1.15
                ctx.lineCap = "round"
                ctx.stroke()
                ctx.restore()
            }

            drawShootingStar(
                areaX + areaW * 0.72, h * 0.10,
                areaX + areaW * 0.83, h * 0.175,
                0.22
            )

            // 3. Faint constellation filaments (upper-right & lower-left sky)
            function drawConstellation(points, colorRgb, lineAlpha) {
                ctx.save()
                ctx.strokeStyle = `rgba(${colorRgb}, ${lineAlpha})`
                ctx.lineWidth = 0.75
                ctx.beginPath()
                for (let i = 0; i < points.length; i++) {
                    const pt = points[i]
                    if (i === 0) ctx.moveTo(pt[0], pt[1])
                    else ctx.lineTo(pt[0], pt[1])
                }
                ctx.stroke()
                for (const pt of points) {
                    ctx.fillStyle = `rgba(${colorRgb}, ${lineAlpha * 3.8})`
                    ctx.beginPath()
                    ctx.arc(pt[0], pt[1], pt[2] || 1.4, 0, Math.PI * 2)
                    ctx.fill()
                }
                ctx.restore()
            }

            const upperConstellation = [
                [areaX + areaW * 0.76, h * 0.25, 1.4],
                [areaX + areaW * 0.82, h * 0.20, 1.8],
                [areaX + areaW * 0.88, h * 0.23, 1.5],
                [areaX + areaW * 0.92, h * 0.16, 1.3]
            ]
            drawConstellation(upperConstellation, isWork ? "230, 212, 195" : "180, 218, 255", 0.065)

            const leftConstellation = [
                [areaX + areaW * 0.10, h * 0.28, 1.3],
                [areaX + areaW * 0.16, h * 0.22, 1.7],
                [areaX + areaW * 0.23, h * 0.25, 1.4]
            ]
            drawConstellation(leftConstellation, "178, 205, 255", 0.055)

            // 4. Soft 4-point celestial starlight glints
            function drawStarGlint(x, y, radius, colorRgb, alpha) {
                ctx.save()
                const halo = ctx.createRadialGradient(x, y, 0, x, y, radius * 3.4)
                halo.addColorStop(0.0, `rgba(${colorRgb}, ${alpha * 0.42})`)
                halo.addColorStop(1.0, `rgba(${colorRgb}, 0.0)`)
                ctx.fillStyle = halo
                ctx.beginPath()
                ctx.arc(x, y, radius * 3.4, 0, Math.PI * 2)
                ctx.fill()

                ctx.fillStyle = `rgba(${colorRgb}, ${alpha})`
                ctx.beginPath()
                const inner = radius * 0.16
                ctx.moveTo(x, y - radius)
                ctx.quadraticCurveTo(x + inner, y - inner, x + radius, y)
                ctx.quadraticCurveTo(x + inner, y + inner, x, y + radius)
                ctx.quadraticCurveTo(x - inner, y + inner, x - radius, y)
                ctx.quadraticCurveTo(x - inner, y - inner, x, y - radius)
                ctx.closePath()
                ctx.fill()
                ctx.restore()
            }

            const iceRgb = "182, 224, 255"
            const auroraMintRgb = "140, 232, 222"
            const warmStarRgb = isCode ? "190, 212, 255" : "248, 214, 174"
            const violetRgb = "205, 192, 255"

            drawStarGlint(areaX + areaW * 0.82, h * 0.20, 7.0, warmStarRgb, 0.34)
            drawStarGlint(areaX + areaW * 0.16, h * 0.22, 5.2, violetRgb, 0.28)
            drawStarGlint(areaX + areaW * 0.89, h * 0.36, 4.4, iceRgb, 0.24)
            drawStarGlint(areaX + areaW * 0.18, h * 0.72, 5.8, auroraMintRgb, 0.26)
            drawStarGlint(areaX + areaW * 0.68, h * 0.78, 4.6, iceRgb, 0.22)
        }
    }
}
