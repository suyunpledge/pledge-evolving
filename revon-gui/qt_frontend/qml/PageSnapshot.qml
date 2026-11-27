import QtQml

QtObject {
    id: snapshot
    required property string pageKey
    property var data: ({})
    function refresh() { data = workbench.pageSnapshot(pageKey) }
    onPageKeyChanged: if (pageKey) refresh()
    // An asynchronous Loader can receive page data while its children are being constructed.
    Component.onCompleted: refresh()
    readonly property Connections updates: Connections {
        target: workbench
        function onPageUpdated(key) {
            if (key === snapshot.pageKey)
                snapshot.refresh()
        }
    }
}
