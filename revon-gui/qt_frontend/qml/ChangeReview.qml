import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Basic as Basic
import QtQuick.Layouts

Popup {
    id: review
    objectName: "changeReviewPopup"
    required property var material
    required property var service
    property bool confirmUndo: false
    property alias fileIndex: fileSelector.currentIndex
    readonly property var files: service.data.files || []
    readonly property var currentFile: files.length ? files[Math.max(0, Math.min(fileIndex, files.length - 1))] : ({})
    readonly property var diffLines: (currentFile.diff || "").split("\n")
    modal: true; focus: true; padding: 22
    closePolicy: service.busy ? Popup.NoAutoClose : Popup.CloseOnEscape | Popup.CloseOnPressOutside
    background: GlassSurface { material: review.material; radius: 20; tint: "#ed18212b" }
    onOpened: fileIndex = 0
    Connections {
        target: review.service
        function onOpenRequested() { review.confirmUndo = false; review.open() }
    }
    contentItem: ColumnLayout {
        spacing: 14
        Item {
            Layout.fillWidth: true; Layout.preferredHeight: 48
            Column {
                anchors.left: parent.left; spacing: 5
                Text { text: "本次代码修改"; color: "#edf0f4"; font.pixelSize: 19; font.weight: Font.DemiBold }
                Text { text: review.files.length + " 个文件 · 相对于本次操作前的版本"; color: "#929eac"; font.pixelSize: 12 }
            }
            GlassButton { anchors.right: parent.right; material: review.material; glyph: "x"; tip: "关闭差异"; enabled: !review.service.busy; onClicked: review.close() }
        }
        Basic.ComboBox {
            id: fileSelector
            objectName: "changeFileSelector"
            Layout.fillWidth: true; model: review.files; textRole: "name"
            implicitHeight: 42
            onCurrentIndexChanged: diff.positionViewAtBeginning()
            palette.text: "#dfe5eb"; palette.buttonText: "#dfe5eb"
            palette.base: "#1d2630"; palette.button: "#1d2630"
            background: Rectangle {
                radius: 8; color: "#1d2630"
                border.color: fileSelector.activeFocus ? "#72879c" : "#384552"
            }
            contentItem: Text {
                leftPadding: 12; rightPadding: 36
                text: fileSelector.displayText; color: "#dfe5eb"
                font: fileSelector.font; elide: Text.ElideMiddle; verticalAlignment: Text.AlignVCenter
            }
            delegate: Basic.ItemDelegate {
                required property var modelData
                required property int index
                width: fileSelector.width; height: 42
                highlighted: fileSelector.highlightedIndex === index
                contentItem: Text {
                    text: modelData.name; color: "#e1e7ee"; font: fileSelector.font
                    elide: Text.ElideMiddle; verticalAlignment: Text.AlignVCenter
                }
                background: Rectangle { radius: 5; color: parent.highlighted ? "#354657" : "transparent" }
            }
            popup: Basic.Popup {
                y: fileSelector.height + 4; width: fileSelector.width; padding: 4
                implicitHeight: Math.min(contentItem.implicitHeight + 8, 260)
                background: Rectangle { radius: 9; color: "#202a35"; border.color: "#475566" }
                contentItem: ListView {
                    clip: true; implicitHeight: contentHeight
                    model: fileSelector.popup.visible ? fileSelector.delegateModel : null
                    currentIndex: fileSelector.highlightedIndex
                    ScrollIndicator.vertical: ScrollIndicator {}
                }
            }
            Accessible.name: "修改的文件"
        }
        RowLayout {
            Text { text: review.currentFile.kind === "created" ? "新增文件" : review.currentFile.kind === "deleted" ? "删除文件" : "修改文件"; color: "#9ba8b7"; font.pixelSize: 12 }
            Text { text: review.currentFile.added == null ? "—" : "+" + review.currentFile.added; color: "#84c9a4"; font.pixelSize: 12 }
            Text { text: review.currentFile.removed == null ? "—" : "−" + review.currentFile.removed; color: "#dc9494"; font.pixelSize: 12 }
            Item { Layout.fillWidth: true }
            GlassButton {
                material: review.material; text: "复制备份路径"; quiet: true
                visible: !!review.currentFile.backupPath
                onClicked: controller.copyText(review.currentFile.backupPath)
            }
            GlassButton { material: review.material; text: "复制差异"; quiet: true; enabled: !!review.currentFile.diff; onClicked: controller.copyText(review.currentFile.diff) }
        }
        Text {
            Layout.fillWidth: true; visible: !!review.currentFile.backupPath
            text: "手动恢复时，将此备份复制回原文件位置。\n" + (review.currentFile.backupPath || "")
            textFormat: Text.PlainText; wrapMode: Text.WrapAnywhere; color: "#b8c4d2"; font.pixelSize: 12
        }
        Rectangle {
            Layout.fillWidth: true; Layout.fillHeight: true; radius: 10; color: "#10161d"; clip: true
            ListView {
                id: diff; objectName: "changeDiffLines"
                anchors.fill: parent; anchors.margins: 8; clip: true
                model: review.diffLines
                contentWidth: Math.max(width, Math.min(20000, review.diffLines.reduce((n, line) => Math.max(n, line.length), 0) * 8 + 30))
                flickableDirection: Flickable.AutoFlickDirection
                boundsBehavior: Flickable.StopAtBounds
                ScrollBar.vertical: ScrollBar {}
                ScrollBar.horizontal: ScrollBar {}
                delegate: Rectangle {
                    required property string modelData
                    width: diff.contentWidth; height: 23
                    color: modelData.startsWith("+") && !modelData.startsWith("+++") ? "#2533473c" : modelData.startsWith("-") && !modelData.startsWith("---") ? "#284e272b" : "transparent"
                    TextEdit {
                        x: 8; y: 2; width: parent.width - 16; height: 21
                        text: modelData; textFormat: TextEdit.PlainText; readOnly: true; selectByMouse: true
                        font.family: "Consolas"; font.pixelSize: 13
                        color: modelData.startsWith("+") ? "#a3d9b7" : modelData.startsWith("-") ? "#e4adad" : modelData.startsWith("@@") ? "#8bafcf" : "#bbc5d0"
                        selectionColor: "#405a70"
                    }
                }
            }
            Text {
                anchors.centerIn: parent; width: parent.width - 40; wrapMode: Text.Wrap; horizontalAlignment: Text.AlignHCenter
                visible: review.service.busy || (!review.currentFile.diff && review.files.length > 0)
                text: review.service.busy ? "正在核对文件版本…" : review.currentFile.binary ? "二进制、非 UTF-8 或较大文件，无法显示文本差异。" : "没有可显示的文本差异。"
                color: "#a7b5c5"; font.pixelSize: 13
            }
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; color: "#d1b38e"; font.pixelSize: 12; visible: !!text
            text: review.service.error || (review.files.some(file => file.conflict) ? "部分文件有后续修改，已阻止回退。请保留当前文件并手动合并。" : review.currentFile.truncated ? "差异较长，仅显示前 64 KB；回退仍按完整文件版本校验。" : "")
        }
        Text {
            Layout.fillWidth: true; wrapMode: Text.Wrap; visible: review.confirmUndo; color: "#d9c5a9"; font.pixelSize: 13
            text: "回退以上文件的本次修改？将恢复操作前的内容，并移除本次新增的文件。其他文件不受影响。"
        }
        RowLayout {
            Layout.fillWidth: true
            Text { Layout.fillWidth: true; text: review.service.data.restored ? "✓ 已回退" : "回退前会再次检查文件版本"; color: "#8f9dad"; font.pixelSize: 12 }
            GlassButton { material: review.material; visible: review.confirmUndo; text: "取消"; quiet: true; enabled: !review.service.busy; onClicked: review.confirmUndo = false }
            GlassButton {
                objectName: "confirmRevertButton"
                material: review.material; text: review.confirmUndo ? "确认回退" : "回退本次修改"; glyph: "undo-2"
                visible: !review.service.data.restored
                enabled: review.service.data.canRevert === true && !review.service.busy && !controller.busy
                onClicked: { if (review.confirmUndo) { review.service.revert(); review.confirmUndo = false } else review.confirmUndo = true }
            }
        }
    }
}
