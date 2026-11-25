import QtQuick
import QtMultimedia

Item {
    id: layer
    required property var wallpaper
    // False while the window is hidden/minimised or reduced motion is on: playback pauses
    // and the current (or poster) frame stays as a still background.
    required property bool running
    property bool reducedMotion: false
    signal backdropReady()
    readonly property string failure: gif.status === AnimatedImage.Error ? "无法播放已导入的 GIF。"
        : player.error !== MediaPlayer.NoError ? "无法播放已导入的视频，请检查文件或重新导入。" : ""
    readonly property bool videoHasFrame: video.sourceRect.width > 0
    readonly property bool ready: !failure && (gif.status === AnimatedImage.Ready || videoHasFrame
                                               || poster.status === Image.Ready)
    onFailureChanged: if (failure) layer.wallpaper.reportFailure(layer.wallpaper.currentId)
    onReadyChanged: if (ready) layer.backdropReady()
    onVideoHasFrameChanged: if (videoHasFrame) layer.backdropReady()

    opacity: ready ? 1.0 : 0.0
    Behavior on opacity {
        NumberAnimation {
            duration: layer.reducedMotion ? 0 : 260
            easing.type: Easing.OutCubic
        }
    }

    AnimatedImage {
        id: gif; objectName: "gifWallpaper"
        anchors.fill: parent
        source: layer.wallpaper.kind === "gif" ? layer.wallpaper.source : ""
        playing: true; paused: !layer.running
        cache: false; asynchronous: true
        smooth: true
        fillMode: Image.PreserveAspectCrop; clip: true
        onStatusChanged: if (status === AnimatedImage.Ready) layer.backdropReady()
    }
    // Poster frame captured at import: shown until the first decoded frame, and while
    // reduced motion keeps the video from ever starting.
    Image {
        id: poster; objectName: "videoPoster"
        anchors.fill: parent
        visible: layer.wallpaper.kind === "video" && (!layer.videoHasFrame || !layer.running)
        source: layer.wallpaper.kind === "video" ? layer.wallpaper.thumb : ""
        asynchronous: true; cache: false; smooth: true
        fillMode: Image.PreserveAspectCrop; clip: true
        onStatusChanged: if (status === Image.Ready) layer.backdropReady()
    }
    VideoOutput {
        id: video; objectName: "videoWallpaper"
        anchors.fill: parent
        fillMode: VideoOutput.PreserveAspectCrop; clip: true
        visible: layer.wallpaper.kind === "video"
    }
    // No audioOutput is attached, so wallpapers are always silent.
    MediaPlayer {
        id: player; objectName: "wallpaperPlayer"
        source: layer.wallpaper.kind === "video" ? layer.wallpaper.source : ""
        videoOutput: video
        loops: MediaPlayer.Infinite
        function syncPlayback() {
            if (!source.toString() || error !== MediaPlayer.NoError) return
            if (layer.running) play()
            else if (playbackState === MediaPlayer.PlayingState) pause()
        }
        onSourceChanged: Qt.callLater(syncPlayback)
        onMediaStatusChanged: {
            if (mediaStatus === MediaPlayer.LoadedMedia && layer.running)
                Qt.callLater(syncPlayback)
        }
    }
    onRunningChanged: player.syncPlayback()

    // Base dimming layer + subtle top/bottom cinematic readability vignette
    Rectangle {
        anchors.fill: parent
        visible: layer.ready
        color: "#080c12"; opacity: layer.wallpaper.dim
    }
    Rectangle {
        anchors { left: parent.left; right: parent.right; top: parent.top }
        height: Math.min(110, parent.height * 0.16)
        visible: layer.ready
        opacity: Math.min(0.68, 0.14 + layer.wallpaper.dim * 0.40)
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#b8060a10" }
            GradientStop { position: 1.0; color: "#00060a10" }
        }
    }
    Rectangle {
        anchors { left: parent.left; right: parent.right; bottom: parent.bottom }
        height: Math.min(168, parent.height * 0.24)
        visible: layer.ready
        opacity: Math.min(0.75, 0.18 + layer.wallpaper.dim * 0.45)
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#00060a10" }
            GradientStop { position: 1.0; color: "#cc060a10" }
        }
    }
}
