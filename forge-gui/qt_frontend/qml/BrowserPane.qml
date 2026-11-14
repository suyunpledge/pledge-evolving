import QtQuick
import QtQuick.Controls
import QtWebEngine

Item {
    id: pane
    required property var material
    property string loadError: ""
    Rectangle { anchors.fill: parent; color: "#151b23" }
    Rectangle { width: 1; height: parent.height; color: "#34404d" }
    WebEngineProfile {
        id: browserProfile
        storageName: controller.isDemo ? "" : "ForgeWork"
        offTheRecord: controller.isDemo
    }
    Glyph { x: 16; y: 23; name: "globe"; width: 17; height: 17 }
    Text { x: 42; y: 22; text: "Work 浏览器"; color: "#ecf0f4"; font.pixelSize: 14; font.weight: Font.Medium }
    GlassButton { material: pane.material; x: parent.width - width - 12; y: 14; glyph: "x"; tip: "收起浏览器，保留页面"; onClicked: workbench.toggleBrowser() }
    Row {
        id: actions; x: 14; y: 62; spacing: 5
        GlassButton { material: pane.material; glyph: "chevron-left"; enabled: web.canGoBack; tip: "后退"; onClicked: web.goBack() }
        GlassButton { material: pane.material; glyph: "chevron-right"; enabled: web.canGoForward; tip: "前进"; onClicked: web.goForward() }
        GlassButton { material: pane.material; glyph: web.loading ? "x" : "refresh-cw"; tip: web.loading ? "停止" : "刷新"; onClicked: web.loading ? web.stop() : web.reload() }
    }
    TextField {
        id: address; objectName: "browserAddress"
        x: actions.x + actions.width + 7; y: actions.y; width: Math.max(80, parent.width - x - 14); height: 36
        text: web.url.toString() === "about:blank" ? "" : web.url.toString()
        placeholderText: "输入网址"; color: "#e5ebf1"; placeholderTextColor: "#8b97a7"
        selectByMouse: true; leftPadding: 10
        background: GlassSurface { material: pane.material; radius: 10 }
        onAccepted: { workbench.openBrowser(text); web.forceActiveFocus() }
    }
    ProgressBar { x: 14; y: 103; width: parent.width - 28; height: 2; from: 0; to: 100; value: web.loadProgress; visible: web.loading }
    Text { id: errorLabel; x: 14; y: 112; width: parent.width - 28; visible: pane.loadError !== ""; text: pane.loadError; color: "#e1b586"; wrapMode: Text.Wrap; font.pixelSize: 12 }
    Item {
        x: 14; y: 112 + (errorLabel.visible ? errorLabel.height + 10 : 0)
        width: parent.width - 28; height: Math.max(100, parent.height - y - 32)
        Rectangle { anchors.fill: parent; color: "#10151c"; radius: 10 }
        WebEngineView {
            id: web; objectName: "workBrowser"
            anchors.fill: parent
            profile: browserProfile
            url: workbench.browserUrl
            backgroundColor: "#10151c"
            settings.localContentCanAccessRemoteUrls: false
            settings.localContentCanAccessFileUrls: false
            settings.javascriptCanAccessClipboard: false
            onNewWindowRequested: request => request.openIn(web)
            onCertificateError: error => { error.rejectCertificate(); pane.loadError = "站点证书验证失败。" }
            onLoadingChanged: info => {
                if (info.status === WebEngineView.LoadFailedStatus) pane.loadError = info.errorString
                else if (info.status === WebEngineView.LoadStartedStatus) pane.loadError = ""
            }
            onFeaturePermissionRequested: (origin, feature) => {
                pane.loadError = "此页面请求设备权限，当前未授予。"
                grantFeaturePermission(origin, feature, false)
            }
            onNavigationRequested: request => {
                const value = request.url.toString()
                if (!(value.startsWith("http://") || value.startsWith("https://") || value === "about:blank"))
                    request.action = WebEngineNavigationRequest.IgnoreRequest
            }
        }
        Column {
            anchors.centerIn: parent; width: parent.width - 36; spacing: 14
            visible: web.url.toString() === "about:blank"
            Text { width: parent.width; text: "网页就在工作台旁边"; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; color: "#e8edf3"; font.pixelSize: 18 }
            Text { width: parent.width; text: "输入网址开始浏览。收起面板后，当前页面仍会保留。"; horizontalAlignment: Text.AlignHCenter; wrapMode: Text.Wrap; color: "#8e9cac"; font.pixelSize: 13 }
        }
    }
    Text { x: 14; y: parent.height - 24; width: parent.width - 28; text: web.title; elide: Text.ElideRight; color: "#8996a6"; font.pixelSize: 11 }
}
