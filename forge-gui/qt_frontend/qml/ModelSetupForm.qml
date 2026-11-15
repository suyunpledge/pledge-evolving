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

    function reset() {
        vendors.currentIndex = 0
        loadVendor()
    }
    function loadVendor() {
        defaults = service.defaults(vendorId)
        modelField.text = defaults.model || ""
        urlField.text = defaults.url || ""
        temperatureField.text = defaults.temperature || "0.7"
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
        contentItem: Text { text: vendors.displayText; color: "#edf1f5"; verticalAlignment: Text.AlignVCenter }
        background: GlassSurface { material: form.material; radius: 10; tint: "#56212d38" }
        onActivated: form.loadVendor()
    }
    FieldLabel { visible: !form.keyless; text: "API " + (form.english ? "key" : "密钥") }
    SetupField {
        id: keyField; objectName: "setupKey"; visible: !form.keyless
        echoMode: TextInput.Password
        inputMethodHints: Qt.ImhHiddenText | Qt.ImhSensitiveData | Qt.ImhNoPredictiveText
        placeholderText: form.english ? "Enter a key or reuse the saved key for this address" : "填写密钥，或留空使用此地址已保存的密钥"
        Accessible.name: "API " + (form.english ? "key" : "密钥")
    }
    FieldLabel { text: form.english ? "API base URL" : "自定义接口地址" }
    SetupField {
        id: urlField; objectName: "setupEndpoint"
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
