import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

// 桌面宠物设置：显示 / 隐藏、选择宠物、改名、打开自定义宠物包文件夹。
Popup {
    id: settings
    objectName: "petSettings"
    required property var material
    required property var pets
    padding: 22; modal: true; focus: true
    Overlay.modal: Rectangle { color: "#6605080d" }
    background: GlassSurface { material: settings.material; radius: 22; tint: "#ef19232d" }
    onAboutToShow: { pets.reload(); nameField.text = pets.current.name || "" }

    contentItem: ColumnLayout {
        spacing: 14
        RowLayout {
            Layout.fillWidth: true
            Text { text: "桌面宠物"; color: Theme.textPrimary; font.pixelSize: 19; font.weight: Font.DemiBold; Layout.fillWidth: true }
            Switch {
                id: shown
                objectName: "petEnabled"
                checked: settings.pets.enabled
                onToggled: settings.pets.setEnabled(checked)
                Accessible.name: "显示桌面宠物"
            }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.textSecondary; font.pixelSize: 12
            text: "宠物浮在桌面上：Agent 工作时思考，完成时开心，出错或等待批准时担心。拖动可移动，点击打开 Keystone，右键有更多选项。"
        }
        GridView {
            id: grid
            objectName: "petGrid"
            Layout.fillWidth: true
            Layout.preferredHeight: Math.min(2, Math.ceil(count / 3)) * cellHeight
            cellWidth: Math.floor(width / 3); cellHeight: 124
            clip: true; interactive: contentHeight > height
            model: settings.pets.packs
            delegate: AbstractButton {
                id: tile
                required property var modelData
                readonly property bool current: modelData.id === settings.pets.currentId
                width: grid.cellWidth - 8; height: grid.cellHeight - 8
                hoverEnabled: true; focusPolicy: Qt.StrongFocus
                Accessible.name: modelData.name + (current ? "（当前）" : "")
                onClicked: { settings.pets.select(modelData.id); nameField.text = settings.pets.current.name }
                background: Rectangle {
                    radius: 12; color: tile.hovered ? "#22ffffff" : "#14ffffff"
                    border.width: tile.current ? 2 : tile.visualFocus ? 1 : 0
                    border.color: tile.current ? Theme.accent : "#55ffffff"
                }
                contentItem: Item {
                    Item {
                        id: thumb
                        anchors { horizontalCenter: parent.horizontalCenter; top: parent.top; topMargin: 8 }
                        width: 72; height: 72; clip: true
                        Image {
                            source: tile.modelData.kind === "animated" ? "" : tile.modelData.preview
                            width: thumb.width * tile.modelData.columns
                            height: thumb.height * Math.ceil(tile.modelData.frames / tile.modelData.columns)
                            sourceSize: Qt.size(width, height)
                            cache: true
                        }
                        AnimatedImage {
                            anchors.fill: parent; playing: false; fillMode: Image.PreserveAspectFit
                            source: tile.modelData.kind === "animated" ? tile.modelData.preview : ""
                        }
                    }
                    Text {
                        anchors { horizontalCenter: parent.horizontalCenter; bottom: parent.bottom; bottomMargin: 6 }
                        width: parent.width - 8; horizontalAlignment: Text.AlignHCenter; elide: Text.ElideRight
                        text: tile.modelData.name + (tile.modelData.custom ? " · 自定义" : !tile.modelData.builtin ? " · 宠物包" : "")
                        color: Theme.textPrimary; font.pixelSize: 12
                    }
                }
            }
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            Text { text: "名字"; color: Theme.textSecondary; font.pixelSize: 13 }
            TextField {
                id: nameField
                objectName: "petName"
                Layout.fillWidth: true; maximumLength: 24; selectByMouse: true
                placeholderText: "给当前宠物起个名字"
                color: Theme.textPrimary; placeholderTextColor: Theme.textMuted; leftPadding: 10
                background: GlassSurface { material: settings.material; radius: 10; emphasis: nameField.activeFocus ? 0.8 : 0 }
                onEditingFinished: settings.pets.rename(settings.pets.currentId, text)
            }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: Theme.textMuted; font.pixelSize: 11
            text: "自定义：在宠物包文件夹里新建子文件夹（如 my-cat），放入 pet.json 和图片；子文件夹与内置宠物同名（crab / robot / whale）即替换该宠物的形象。格式见 qt_frontend/pets/README.md。"
        }
        Text {
            objectName: "petError"
            visible: !!settings.pets.error
            Layout.fillWidth: true; wrapMode: Text.Wrap; textFormat: Text.PlainText
            text: settings.pets.error; color: Theme.dangerText; font.pixelSize: 12
        }
        RowLayout {
            Layout.fillWidth: true; spacing: 8
            GlassButton { material: settings.material; text: "打开宠物包文件夹"; glyph: "folder"; onClicked: Qt.openUrlExternally(settings.pets.packFolder()) }
            GlassButton { material: settings.material; text: "重新载入"; onClicked: settings.pets.reload() }
            Item { Layout.fillWidth: true }
            GlassButton { material: settings.material; text: "完成"; onClicked: settings.close() }
        }
    }
}
