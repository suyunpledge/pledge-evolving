import QtQuick
import QtQuick.Controls
import QtQuick.Window

// 桌面宠物：无边框、透明、置顶的小窗口。可拖动并记住位置，点击打开 Keystone，
// 随 Agent 状态思考 / 开心 / 担心。空闲时只有低频定时器（眨眼、闲逛、入睡），
// 减少动态效果时不走动、不播放帧动画，窗口隐藏时所有定时器暂停。
Window {
    id: pet
    objectName: "petWindow"
    required property var pets
    required property var host
    required property bool reduced
    signal settingsRequested()

    readonly property var info: pets.current
    readonly property int artW: info.width || 96
    readonly property int artH: info.height || 96
    readonly property int bubbleSpace: 34
    // idle / walk / sleep：宠物自己的活动；Agent 情绪优先（映射见 pets.animation_state）。
    property string ambient: "idle"
    property bool blinking: false
    property int walkDirection: 1
    property int walkLeft: 0
    readonly property string animState: pets.animationState(pets.mood, ambient)
    readonly property string artState: blinking && animState === "idle" ? "sleep" : animState
    readonly property var art: info.states ? info.states[artState] : null
    readonly property bool live: visible && visibility !== Window.Minimized && visibility !== Window.Hidden
    readonly property bool moving: live && !reduced
    readonly property bool mirrored: ambient === "walk" && animState === "walk"
                                     && (walkDirection < 0) === (info.facing !== "left")
    // 可选的表情脸（pet.json 的 faces）：思考 / 开心 / 担心 / 睡觉时在身旁弹出，空闲和走路时不显示。
    readonly property var faces: info.faces || ({})
    readonly property int faceSpace: Object.keys(faces).length ? 68 : 0
    readonly property string face: animState !== "idle" && animState !== "walk" ? (faces[animState] || "") : ""

    width: artW + 24 + faceSpace
    height: artH + bubbleSpace
    color: "transparent"
    flags: Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool | Qt.NoDropShadowWindowHint
    transientParent: null
    title: info.name || "桌面宠物"

    function place() {
        const screens = Qt.application.screens
        let px = pets.posX, py = pets.posY, onScreen = false
        if (pets.hasPosition)
            for (let i = 0; i < screens.length; ++i) {
                const s = screens[i], cx = px + width / 2, cy = py + height / 2
                if (cx >= s.virtualX && cx < s.virtualX + s.width && cy >= s.virtualY && cy < s.virtualY + s.height)
                    onScreen = true
            }
        if (!onScreen) {
            const s = screens.length ? screens[0] : { virtualX: 0, virtualY: 0, width: 1280, height: 800 }
            px = s.virtualX + s.width - width - 48
            py = s.virtualY + s.height - height - 96
        }
        x = px; y = py
    }
    function activateHost() {
        if (host.visibility === Window.Minimized || !host.visible) host.showNormal()
        host.raise()
        host.requestActivate()
    }
    function wake() {
        if (ambient === "sleep") ambient = "idle"
        drowsy.restart()
    }
    function stopWalking() {
        if (ambient === "walk") {
            ambient = "idle"
            pets.savePosition(x, y)
        }
        walker.stop()
    }

    onAnimStateChanged: {
        frame.index = 0
        if (animState === "happy" && moving) hop.restart()
        if (pets.mood) Qt.callLater(wake)
    }
    onReducedChanged: if (reduced) stopWalking()
    onLiveChanged: if (!live) stopWalking()
    Component.onCompleted: { place(); drowsy.restart() }

    // ------------------------------------------------------------ ambient clock

    // 眨眼：借用睡眠帧（闭眼）停留 150 ms。
    Timer {
        id: blinkClock
        interval: 3200 + Math.random() * 3000
        repeat: true
        running: pet.moving && pet.animState === "idle"
        onTriggered: { pet.blinking = true; blinkEnd.start(); interval = 3200 + Math.random() * 3000 }
    }
    Timer { id: blinkEnd; interval: 150; onTriggered: pet.blinking = false }

    // 空闲一会儿后在屏幕底部横向走几步。
    Timer {
        id: wander
        interval: 22000 + Math.random() * 26000
        repeat: true
        running: pet.moving && pet.animState === "idle" && !drag.pressed
        onTriggered: {
            interval = 22000 + Math.random() * 26000
            const s = pet.screen
            const minX = s ? s.virtualX : 0, maxX = s ? s.virtualX + s.width - pet.width : pet.x
            pet.walkDirection = pet.x - 140 < minX ? 1 : pet.x + 140 > maxX ? -1 : (Math.random() < 0.5 ? -1 : 1)
            pet.walkLeft = 50 + Math.floor(Math.random() * 60)
            pet.ambient = "walk"
            walker.start()
        }
    }
    Timer {
        id: walker
        interval: 33
        repeat: true
        running: false
        onTriggered: {
            const s = pet.screen
            const next = pet.x + pet.walkDirection * 2
            if (!pet.moving || pet.walkLeft <= 0 || pet.animState !== "walk"
                    || (s && (next < s.virtualX || next + pet.width > s.virtualX + s.width))) {
                pet.stopWalking()
                return
            }
            pet.x = next
            pet.walkLeft -= 1
        }
    }
    // 久无动静就睡着；Agent 有动静或被点击时醒来。
    Timer {
        id: drowsy
        interval: 180000
        running: pet.live && pet.animState === "idle"
        onTriggered: if (!pets.mood) pet.ambient = "sleep"
    }
    // 精灵图翻帧（只在当前状态确有多帧且允许动效时运行）。
    Timer {
        id: frame
        property int index: 0
        interval: Math.max(33, 1000 / Math.max(1, pet.art ? pet.art.fps : 1))
        repeat: true
        running: pet.moving && !!pet.art && pet.art.kind === "sheet" && pet.art.frames > 1
        onTriggered: index = (index + 1) % pet.art.frames
        onRunningChanged: if (!running) index = 0
    }
    // 气泡里的小动画：思考时的点、睡觉时的 Z，2–3 fps。
    Timer {
        id: bubbleClock
        property int tick: 0
        interval: pet.animState === "sleep" ? 900 : 380
        repeat: true
        running: pet.moving && (pet.animState === "think" || pet.animState === "sleep")
        onTriggered: tick = (tick + 1) % 3
    }
    // 担心时每隔一会儿抖一下，而不是一直抖。
    Timer {
        interval: 2600
        repeat: true
        running: pet.moving && pet.animState === "worried"
        triggeredOnStart: true
        onTriggered: shake.restart()
    }

    // ------------------------------------------------------------------- body

    Item {
        id: body
        objectName: "petBody"
        width: pet.artW; height: pet.artH
        x: 12 + pet.faceSpace; y: pet.bubbleSpace
        transform: [
            Scale { origin.x: body.width / 2; xScale: pet.mirrored ? -1 : 1 },
            Translate { id: jitter }
        ]

        Image {
            objectName: "petStill"
            anchors.fill: parent
            visible: !!pet.art && pet.art.kind === "image"
            source: visible ? pet.art.source : ""
            sourceSize: Qt.size(pet.artW, pet.artH)
            fillMode: Image.PreserveAspectFit
            smooth: pet.info.smooth !== false
            cache: true
        }
        AnimatedImage {
            objectName: "petAnimated"
            anchors.fill: parent
            visible: !!pet.art && pet.art.kind === "animated"
            source: visible ? pet.art.source : ""
            playing: visible && pet.moving
            fillMode: Image.PreserveAspectFit
            smooth: pet.info.smooth !== false
        }
        Item {
            anchors.fill: parent
            clip: true
            visible: !!pet.art && pet.art.kind === "sheet"
            Image {
                objectName: "petSheet"
                readonly property int columns: pet.art && pet.art.kind === "sheet" ? pet.art.columns : 1
                readonly property int rows: pet.art && pet.art.kind === "sheet" ? Math.ceil(pet.art.frames / columns) : 1
                source: parent.visible ? pet.art.source : ""
                width: pet.artW * columns; height: pet.artH * rows
                sourceSize: Qt.size(width, height)
                x: -(frame.index % columns) * pet.artW
                y: -Math.floor(frame.index / columns) * pet.artH
                smooth: pet.info.smooth !== false
                cache: true
            }
        }
        SequentialAnimation {
            id: hop
            loops: 2
            NumberAnimation { target: body; property: "y"; to: pet.bubbleSpace - 14; duration: 170; easing.type: Easing.OutQuad }
            NumberAnimation { target: body; property: "y"; to: pet.bubbleSpace; duration: 190; easing.type: Easing.InQuad }
        }
        SequentialAnimation {
            id: shake
            NumberAnimation { target: jitter; property: "x"; to: -3; duration: 50 }
            NumberAnimation { target: jitter; property: "x"; to: 3; duration: 70 }
            NumberAnimation { target: jitter; property: "x"; to: -2; duration: 70 }
            NumberAnimation { target: jitter; property: "x"; to: 0; duration: 50 }
        }
    }

    // ------------------------------------------------------------- the bubble

    Rectangle {
        id: bubble
        objectName: "petBubble"
        readonly property string mark: pet.animState === "think" ? ["·", "··", "···"][bubbleClock.tick]
                                       : pet.animState === "sleep" ? ["z", "z Z", "z Z z"][bubbleClock.tick]
                                       : pet.animState === "worried" ? "!"
                                       : pet.animState === "happy" ? "✓" : ""
        visible: !!mark && !pet.face
        anchors.horizontalCenter: body.horizontalCenter
        anchors.horizontalCenterOffset: pet.artW * 0.22
        y: 2
        width: Math.max(28, label.implicitWidth + 16); height: 24; radius: 12
        color: pet.animState === "worried" ? "#f5ffe2b0" : "#f2f7f9fb"
        border.color: pet.animState === "worried" ? "#d49a2a" : "#7f8c99"
        border.width: 1
        Text {
            id: label
            anchors.centerIn: parent
            text: bubble.mark
            color: pet.animState === "worried" ? "#8a4f00" : pet.animState === "happy" ? "#1d7a46" : "#33404c"
            font.pixelSize: 13; font.weight: Font.DemiBold
        }
    }

    Rectangle {
        objectName: "petFace"
        visible: !!pet.face
        x: 4; y: pet.bubbleSpace + 8
        width: 62; height: 62; radius: 16
        color: "#f7fafc"; border.color: pet.animState === "worried" ? "#d49a2a" : "#8c9aa8"; border.width: 1.5
        Image {
            anchors.fill: parent; anchors.margins: 3
            source: pet.face
            sourceSize: Qt.size(112, 112)
            fillMode: Image.PreserveAspectFit
            smooth: true
        }
    }

    // ------------------------------------------------------------ interaction

    MouseArea {
        id: drag
        objectName: "petMouse"
        anchors.fill: body
        acceptedButtons: Qt.LeftButton | Qt.RightButton
        cursorShape: moved ? Qt.ClosedHandCursor : Qt.PointingHandCursor
        Accessible.role: Accessible.Button
        Accessible.name: (pet.info.name || "桌面宠物") + "，点击打开 Keystone，拖动可移动"
        property point pressAt
        property point origin
        property bool moved: false
        onPressed: mouse => {
            if (mouse.button === Qt.RightButton) {
                menu.popup()
                return
            }
            pressAt = mapToGlobal(mouse.x, mouse.y)
            origin = Qt.point(pet.x, pet.y)
            moved = false
            pet.stopWalking()
            pet.wake()
        }
        onPositionChanged: mouse => {
            if (!(pressedButtons & Qt.LeftButton)) return
            const at = mapToGlobal(mouse.x, mouse.y)
            const dx = at.x - pressAt.x, dy = at.y - pressAt.y
            if (!moved && Math.abs(dx) + Math.abs(dy) > 4) moved = true
            if (moved) { pet.x = origin.x + dx; pet.y = origin.y + dy }
        }
        onReleased: mouse => {
            if (mouse.button !== Qt.LeftButton) return
            if (moved) pets.savePosition(pet.x, pet.y)
            else pet.activateHost()
            moved = false
        }
    }

    Menu {
        id: menu
        objectName: "petMenu"
        popupType: Popup.Window
        MenuItem { text: "打开 Keystone"; onTriggered: pet.activateHost() }
        Menu {
            id: switchMenu
            title: "换一只宠物"
            popupType: Popup.Window
            Instantiator {
                model: pet.pets.packs
                delegate: MenuItem {
                    required property var modelData
                    text: (modelData.id === pet.pets.currentId ? "✓  " : "    ") + modelData.name
                    onTriggered: pet.pets.select(modelData.id)
                }
                onObjectAdded: (index, object) => switchMenu.insertItem(index, object)
                onObjectRemoved: (index, object) => switchMenu.removeItem(object)
            }
        }
        MenuItem { text: "宠物设置…"; onTriggered: { pet.activateHost(); pet.settingsRequested() } }
        MenuSeparator {}
        MenuItem { text: "隐藏桌面宠物"; onTriggered: pet.pets.setEnabled(false) }
    }
}
