import QtQuick

Image {
    property string name: "message-square"
    property color ink: Theme.textSecondary
    source: controller.iconSource(name, ink.toString())
    sourceSize.width: width * Screen.devicePixelRatio
    sourceSize.height: height * Screen.devicePixelRatio
    width: 18; height: 18
    fillMode: Image.PreserveAspectFit
    smooth: true
}
