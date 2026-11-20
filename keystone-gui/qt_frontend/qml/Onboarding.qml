import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

Popup {
    id: panel
    objectName: "onboardingPopup"
    required property var material
    required property var service
    property bool guide: false
    property int step: 0
    property string language: "zh-CN"
    property string experience: "simple"
    readonly property bool english: language === "en"
    readonly property bool showForm: !guide || step === 2
    property bool finishAfterSave: false
    property string initialProvider: ""
    property string initialUrl: ""
    signal mediaModelsRequested()
    modal: true; focus: true; padding: 24
    closePolicy: guide || service.busy ? Popup.NoAutoClose : Popup.CloseOnEscape
    Overlay.modal: Rectangle { color: "#9905080d" }
    background: GlassSurface { material: panel.material; radius: 24; emphasis: 0.6; tint: "#ed19232d" }

    function showGuide() {
        guide = true
        initialProvider = ""
        initialUrl = ""
        step = 0
        language = service.language
        experience = service.initialDeveloper ? "developer" : "simple"
        Qt.callLater(open)
    }
    function showSetup(providerId, url) {
        guide = false
        initialProvider = providerId || ""
        initialUrl = url || ""
        language = service.language
        experience = service.developer ? "developer" : "simple"
        Qt.callLater(open)
    }
    function complete() {
        if (service.finish(language, experience)) {
            finishAfterSave = false
            close()
        }
    }
    onOpened: form.reset(initialProvider, initialUrl)
    onClosed: { form.clearKey(); finishAfterSave = false }
    Connections {
        target: panel.service
        function onSaved() {
            form.clearKey()
            if (panel.finishAfterSave) panel.complete()
        }
        function onChanged() {
            if (!panel.service.busy && !panel.service.success) panel.finishAfterSave = false
        }
    }

    component GuideChoice: GlassRow {
        id: choice
        status: checked ? (panel.english ? "Selected" : "已选中") : ""
        statusColor: "#c7e2fa"
        actionGlyph: checked ? "circle-check" : ""
        Accessible.role: Accessible.RadioButton
        Accessible.checkable: true
        Accessible.checked: checked

        background: GlassSurface {
            material: choice.material
            radius: 12
            tint: choice.checked ? "#a5375b77" : "#53232f3b"
            emphasis: choice.checked ? 0.9 : choice.activeFocus ? 0.4 : 0
            hoverAmount: choice.hovered ? 1 : 0
            Rectangle {
                objectName: "guideSelectionOutline"
                anchors.fill: parent
                visible: choice.checked || choice.visualFocus
                radius: parent.radius
                color: "transparent"
                border.color: choice.checked ? "#8ebcdb" : "#778ca4"
                border.width: choice.checked ? 2 : 1
            }
        }
    }

    contentItem: ColumnLayout {
        spacing: 14
        Text {
            Layout.fillWidth: true
            text: panel.showForm ? (panel.english ? "Quick model setup" : "一键配置模型")
                  : panel.step === 0 ? (panel.english ? "Welcome to Keystone" : "欢迎使用 Keystone")
                  : (panel.english ? "Choose your workspace mode" : "选择使用模式")
            color: "#f3f6fa"; font.pixelSize: 24; font.weight: Font.DemiBold
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap
            text: panel.showForm ? (panel.english ? "Use the provider’s defaults or enter an OpenAI-compatible API address." : "使用厂商预设，或填写 OpenAI 兼容接口地址。")
                  : panel.step === 0 ? (panel.english ? "Choose the language for setup and workspace navigation." : "选择引导与工作台导航使用的语言。")
                  : (panel.english ? "You can change modes later in settings." : "之后可以在设置中重新选择。")
            color: "#a3b0c0"; font.pixelSize: 13
        }
        ColumnLayout {
            visible: !panel.showForm && panel.step === 0
            Layout.fillWidth: true
            GuideChoice {
                Layout.fillWidth: true; material: panel.material; glyph: "globe"
                objectName: "guideChinese"; text: "简体中文"; checked: panel.language === "zh-CN"
                onClicked: panel.language = "zh-CN"
            }
            GuideChoice {
                Layout.fillWidth: true; material: panel.material; glyph: "globe"
                objectName: "guideEnglish"; text: "English"; checked: panel.english
                onClicked: panel.language = "en"
            }
        }
        ColumnLayout {
            visible: !panel.showForm && panel.step === 1
            Layout.fillWidth: true
            GuideChoice {
                Layout.fillWidth: true; material: panel.material; glyph: "sparkles"
                objectName: "guideSimple"; text: panel.english ? "Standard" : "普通模式"
                detail: panel.english ? "Chat, tasks and everyday tools" : "对话、任务与常用工具"
                checked: panel.experience === "simple"
                onClicked: panel.experience = "simple"
            }
            GuideChoice {
                Layout.fillWidth: true; material: panel.material; glyph: "code"
                objectName: "guideDeveloper"; text: panel.english ? "Developer" : "开发者模式"
                detail: panel.english ? "Coding, evolution and advanced settings" : "Coding、演化与高级模型设置"
                checked: panel.experience === "developer"
                onClicked: panel.experience = "developer"
            }
        }
        ScrollView {
            id: formScroll
            visible: panel.showForm
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(form.implicitHeight, Math.max(180, panel.parent.height - 225))
            contentWidth: availableWidth
            clip: true
            ModelSetupForm {
                id: form
                width: formScroll.availableWidth
                material: panel.material; service: panel.service
                english: panel.english; advanced: panel.experience === "developer"
            }
        }
        Text {
            objectName: "setupStatus"
            Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
            visible: panel.service.busy || text !== ""
            text: panel.service.busy ? (panel.english ? "Working…" : "处理中…") : panel.service.status
            color: panel.service.success ? "#abdac2" : "#eac59d"
            font.pixelSize: 12
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            GlassButton {
                objectName: "guideBack"; material: panel.material; visible: panel.guide && panel.step > 0
                text: panel.english ? "Back" : "上一步"; glyph: "chevron-left"
                enabled: !panel.service.busy
                onClicked: { panel.service.clearStatus(); panel.step-- }
            }
            GlassButton {
                visible: !panel.guide
                material: panel.material; quiet: true; glyph: "image"
                text: panel.english ? "Image and video" : "图片与视频模型"
                enabled: !panel.service.busy
                onClicked: { panel.close(); panel.mediaModelsRequested() }
            }
            Item { Layout.fillWidth: true }
            GlassButton {
                material: panel.material; visible: panel.showForm
                objectName: "setupTest"; enabled: !panel.service.busy
                text: panel.english ? "Test connection" : "测试连接"
                onClicked: form.testConnection()
            }
            GlassButton {
                material: panel.material; visible: panel.showForm
                objectName: "setupDismiss"; enabled: !panel.service.busy
                text: panel.guide ? (panel.english ? "Set up later" : "稍后配置") : (panel.english ? "Close" : "关闭")
                onClicked: panel.guide ? panel.complete() : panel.close()
            }
            GlassButton {
                material: panel.material; selected: true
                objectName: "setupContinue"; enabled: !panel.service.busy
                text: panel.showForm ? (panel.guide ? (panel.english ? "Save and start" : "保存并开始") : (panel.english ? "Save" : "保存"))
                                     : (panel.english ? "Continue" : "下一步")
                onClicked: {
                    if (!panel.showForm) panel.step++
                    else {
                        panel.finishAfterSave = panel.guide
                        form.saveModel()
                    }
                }
            }
        }
    }
}
