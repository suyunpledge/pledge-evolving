import QtQuick
import QtMultimedia

Item {
    id: layer
    required property var wallpaper
    // False while the window is hidden/minimised or reduced motion is on: playback pauses
    // and the current (or poster) frame stays as a still background.
    required property bool running
    readonly property string failure: gif.status === AnimatedImage.Error ? "无法播放已导入的 GIF。"
        : player.error !== MediaPlayer.NoError ? "无法播放已导入的视频，请检查文件或重新导入。" : ""
    onFailureChanged: if (failure) layer.wallpaper.reportFailure(layer.wallpaper.currentId)

    AnimatedImage {
        id: gif; objectName: "gifWallpaper"
        anchors.fill: parent
        source: layer.wallpaper.kind === "gif" ? layer.wallpaper.source : ""
        playing: true; paused: !layer.running
        cache: false; asynchronous: true
        fillMode: Image.PreserveAspectCrop; clip: true
    }
    // Poster frame captured at import: shown until the first decoded frame, and while
    // reduced motion keeps the video from ever starting.
    Image {
        id: poster; objectName: "videoPoster"
        anchors.fill: parent
        visible: layer.wallpaper.kind === "video"
        source: visible ? layer.wallpaper.thumb : ""
        asynchronous: true; cache: false
        fillMode: Image.PreserveAspectCrop; clip: true
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
    Rectangle {
        anchors.fill: parent
        visible: !layer.failure && (gif.status === AnimatedImage.Ready || video.sourceRect.width > 0
                                    || poster.status === Image.Ready)
        color: "#080c12"; opacity: layer.wallpaper.dim
    }
}
