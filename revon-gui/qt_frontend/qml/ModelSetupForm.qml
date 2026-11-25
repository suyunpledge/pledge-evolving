import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: form
    required property var material
    required property var service
    property bool english: false
    property bool advanced: false
    readonly property string vendorId: String(vendors.currentValue || "")
    readonly property bool keyless: !!defaults.keyless
    property var defaults: ({})
    spacing: 9
    enabled: !service.busy

    function reset(providerId, planUrl) {
        vendors.currentIndex = Math.max(0, vendors.indexOfValue(providerId || service.preferredVendor))
        loadVendor()
        if (planUrl) setPlan(planUrl)
    }
    function normalize(address) {
        return address.trim().replace(/\/+$/, "")
    }
    function syncPlan() {
        const address = normalize(urlField.text)
        plans.currentIndex = plans.model.findIndex(plan => normalize(plan.url) === address)
    }
    function setPlan(url) {
        urlField.text = url
        modelField.text = normalize(url) === normalize(defaults.url) ? defaults.model : defaults.exampleModel
        syncPlan()
        keyField.clear()
        service.clearStatus()
    }
    function loadVendor() {
        defaults = service.defaults(vendorId)
        modelField.text = defaults.model || ""
        urlField.text = defaults.url || ""
        temperatureField.text = defaults.temperature || "0.7"
        syncPlan()
        keyField.clear()
        service.clearStatus()
    }
    function testConnection() {
        service.testConnection(vendorId, keyField.text, urlField.text)
    }
    function saveModel() {
        service.saveModel(vendorId, keyField.text, urlField.text, modelField.text, temperatureField.text)
    }
    function clearKey() { keyField.clear() }

    component FieldLabel: Text {
        color: "#acb9c8"; font.pixelSize: 12
    }
    component SetupField: TextField {
        Layout.fillWidth: true
        Layout.preferredHeight: 38
        color: "#edf1f5"; placeholderTextColor: "#8a99ac"
        selectByMouse: true; leftPadding: 12; rightPadding: 12
        background: GlassSurface { material: form.material; radius: 10; tint: "#56212d38"; emphasis: parent.activeFocus ? 0.6 : 0 }
        onTextEdited: form.service.clearStatus()
    }

    FieldLabel { text: form.english ? "Provider" : "模型厂商" }
    ComboBox {
        id: vendors; objectName: "setupVendor"
        Layout.fillWidth: true; Layout.preferredHeight: 38
        model: form.service.vendors; textRole: "name"; valueRole: "id"
        leftPadding: 12
        Accessible.name: form.english ? "Provider" : "模型厂商"
        contentItem: Text { text: vendors.displayText; color: "#edf1f5"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
        background: GlassSurface { material: form.material; radius: 10; tint: "#56212d38" }
        onActivated: form.loadVendor()
    }
    FieldLabel { visible: plans.visible; text: form.english ? "Endpoint preset" : "地区或套餐" }
    ComboBox {
        id: plans; objectName: "setupPlan"
        visible: model.length > 1
        Layout.fillWidth: true; Layout.preferredHeight: 38
        model: form.defaults.plans || []; textRole: "label"; valueRole: "url"
        leftPadding: 12
        displayText: currentIndex >= 0 ? currentText : (form.english ? "Custom endpoint" : "自定义地址")
        Accessible.name: form.english ? "Endpoint preset" : "地区或套餐"
        contentItem: Text { text: plans.displayText; color: "#edf1f5"; verticalAlignment: Text.AlignVCenter; elide: Text.ElideRight }
        background: GlassSurface { material: form.material; radius: 10; tint: "#56212d38" }
        onActivated: form.setPlan(currentValue)
    }
    FieldLabel {
        readonly property string note: plans.currentIndex >= 0 ? plans.model[plans.currentIndex]?.note || "" : ""
        Layout.fillWidth: true
        visible: plans.visible && note.length > 0
        wrapMode: Text.Wrap
        text: note
    }
    FieldLabel {
        id: keyLabel
        visible: !form.keyless; text: "API " + (form.english ? "key" : "密钥")
        Accessible.role: Accessible.StaticText
        Accessible.name: text
        Accessible.labelFor: keyField
    }
    SetupField {
        id: keyField; objectName: "setupKey"; visible: !form.keyless
        echoMode: TextInput.Password
        inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        placeholderText: form.english ? "Enter a key or reuse the saved key for this address" : "填写密钥，或留空使用此地址已保存的密钥"
        Accessible.name: keyLabel.text
        Accessible.labelledBy: keyLabel
    }
    FieldLabel { text: form.english ? "API base URL" : "自定义接口地址" }
    SetupField {
        id: urlField; objectName: "setupEndpoint"
        onTextEdited: form.syncPlan()
        Accessible.name: form.english ? "API base URL" : "自定义接口地址"
        placeholderText: "https://api.example.com/v1"
        inputMethodHints: Qt.ImhUrlCharactersOnly | Qt.ImhNoPredictiveText
    }
    FieldLabel { text: form.english ? "Model ID" : "模型 ID" }
    SetupField {
        id: modelField; objectName: "setupModel"
        Accessible.name: form.english ? "Model ID" : "模型 ID"
        inputMethodHints: Qt.ImhNoPredictiveText
    }
    FieldLabel { visible: form.advanced; text: form.english ? "Temperature (0–2)" : "温度（0–2）" }
    SetupField {
        id: temperatureField; objectName: "setupTemperature"; visible: form.advanced
        Accessible.name: form.english ? "Temperature" : "温度"
        inputMethodHints: Qt.ImhFormattedNumbersOnly
    }
}
