import QtQuick
import QtMultimedia

// 启动动画位：默认是品牌标志淡入（约 1.4 s），可换成视频 / 动图 / QML（见 launch.py）。
// 只用 Animator，渲染线程上运行，主界面在 GUI 线程加载时也不卡。
// 主窗口一出第一帧（launch.ready）就淡出；点击或 Esc 跳过。
Item {
    id: root
    objectName: "launchRoot"
    focus: true
    Keys.onEscapePressed: launch.skip()
    Component.onCompleted: forceActiveFocus()

    opacity: 1
    OpacityAnimator on opacity {
        running: launch.ready
        from: 1; to: 0; duration: launch.fadeMs
    }

    Rectangle {
        anchors.fill: parent
        radius: 26
        color: "#ee101820"
        border.color: "#33ffffff"; border.width: 1
    }

    // ---------------------------------------------------------- built-in fade
    Item {
        id: mark
        objectName: "launchBuiltin"
        anchors.fill: parent
        visible: launch.kind === "builtin"
        Image {
            id: logo
            anchors.horizontalCenter: parent.horizontalCenter
            y: parent.height / 2 - height + 6
            width: 72; height: 72; sourceSize: Qt.size(144, 144)
            source: launch.brandUrl; smooth: true
            opacity: 0; scale: 0.86
            OpacityAnimator on opacity { running: mark.visible; from: 0; to: 1; duration: 620; easing.type: Easing.OutCubic }
            ScaleAnimator on scale { running: mark.visible; from: 0.86; to: 1; duration: 820; easing.type: Easing.OutCubic }
        }
        Text {
            id: word
            anchors { horizontalCenter: parent.horizontalCenter; top: logo.bottom; topMargin: 16 }
            text: "Keystone"; color: "#eef2f6"
            font.pixelSize: 26; font.weight: Font.DemiBold; font.letterSpacing: 1.5
            opacity: 0
            SequentialAnimation on opacity {
                running: mark.visible
                PauseAnimation { duration: 380 }
                OpacityAnimator { target: word; from: 0; to: 1; duration: 560; easing.type: Easing.OutCubic }
            }
        }
        Text {
            id: hint
            anchors { horizontalCenter: parent.horizontalCenter; top: word.bottom; topMargin: 10 }
            text: "正在启动…"; color: "#8d9aa8"; font.pixelSize: 12
            opacity: 0
            SequentialAnimation on opacity {
                running: mark.visible
                PauseAnimation { duration: 820 }
                OpacityAnimator { target: hint; from: 0; to: 1; duration: launch.duration - 820 }
            }
        }
    }

    // ------------------------------------------------------- replacement art
    Loader {
        objectName: "launchCustom"
        anchors.fill: parent
        anchors.margins: 1
        active: launch.kind !== "builtin"
        sourceComponent: launch.kind === "qml" ? qmlArt : launch.kind === "video" ? videoArt
                         : launch.kind === "animated" ? animatedArt : stillArt
    }
    Component { id: qmlArt; Loader { source: launch.source } }
    Component {
        id: videoArt
        Item {
            MediaPlayer { id: player; source: launch.source; videoOutput: output; Component.onCompleted: play() }
            VideoOutput { id: output; anchors.fill: parent; fillMode: VideoOutput.PreserveAspectFit }
        }
    }
    Component { id: animatedArt; AnimatedImage { source: launch.source; fillMode: Image.PreserveAspectFit; playing: true } }
    Component {
        id: stillArt
        Image {
            source: launch.source; fillMode: Image.PreserveAspectFit; sourceSize: Qt.size(width * 2, height * 2)
            opacity: 0
            OpacityAnimator on opacity { from: 0; to: 1; duration: 600 }
        }
    }

    MouseArea { anchors.fill: parent; onClicked: launch.skip(); cursorShape: Qt.PointingHandCursor }
}
