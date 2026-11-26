import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ColumnLayout {
    id: history
    required property var material
    required property var service
    spacing: 10
    RowLayout {
        Layout.fillWidth: true
        Text {
            text: "最近的模型请求"; color: Theme.textPrimary; font.pixelSize: 17; font.weight: Font.DemiBold
            Layout.fillWidth: true
        }
        GlassButton { material: history.material; text: "刷新"; quiet: true; onClicked: history.service.refreshHistory() }
    }
    AppListView {
        id: requests; objectName: "providerRequestList"
        Layout.fillWidth: true; Layout.fillHeight: true; clip: true; spacing: 8
        model: history.service.requestHistory
        Text {
            anchors.centerIn: parent; width: parent.width - 24; horizontalAlignment: Text.AlignHCenter
            visible: requests.count === 0; wrapMode: Text.Wrap; color: Theme.textMuted
            text: "暂无请求记录。使用 revon 发起模型请求后，这里会显示实际访问的接口。"
        }
        delegate: Rectangle {
            id: item
            required property var modelData
            width: requests.rowWidth; height: content.implicitHeight + 24; radius: 12
            color: "#21303d"; border.color: Theme.divider
            ColumnLayout {
                id: content
                x: 14; y: 10; width: parent.width - 28; spacing: 4
                RowLayout {
                    Layout.fillWidth: true; spacing: 10
                    Text {
                        Layout.fillWidth: true; textFormat: Text.PlainText; elide: Text.ElideRight
                        text: [item.modelData.providerName, item.modelData.rowLabel || item.modelData.provider_id].filter(Boolean).join("  ·  ")
                        color: Theme.textPrimary; font.pixelSize: 14; font.weight: Font.DemiBold
                    }
                    Text {
                        color: item.modelData.http_status >= 200 && item.modelData.http_status < 300 ? Theme.success : Theme.warning
                        text: item.modelData.http_status ? "最近 HTTP " + item.modelData.http_status : "最近一次未收到 HTTP 状态码"; font.pixelSize: 12
                    }
                }
                component AddressRow: RowLayout {
                    required property string label
                    required property string address
                    Layout.fillWidth: true; spacing: 10
                    Text { Layout.preferredWidth: 66; text: parent.label; color: Theme.textMuted; font.pixelSize: 12 }
                    TextInput {
                        Layout.fillWidth: true; text: parent.address; readOnly: true; selectByMouse: true
                        Accessible.name: parent.label; clip: true; color: Theme.textPrimary; font.pixelSize: 12
                    }
                }
                AddressRow { label: "配置地址"; address: item.modelData.base_url || "" }
                AddressRow { label: "请求地址"; address: item.modelData.request_url || "" }
                Text {
                    color: Theme.textMuted; font.pixelSize: 11
                    text: (item.modelData.source === "gateway" ? "网关" : "直连") + "  ·  共 " + item.modelData.attempts + " 次尝试  ·  "
                        + Qt.formatDateTime(new Date(Number(item.modelData.last_seen) * 1000), "yyyy-MM-dd hh:mm:ss")
                }
            }
        }
    }
}
