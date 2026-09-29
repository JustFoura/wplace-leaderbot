import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

Item {
  id: root

  property var bar: null
  property var settings: ({})
  property bool panelOpen: false
  readonly property bool opened: panelOpen
  readonly property bool popoutSwitchClosing: false
  readonly property color foreground: bar ? bar.foreground : Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.55)
  readonly property color surface: Color.popups.background
  property string serviceState: "checking"
  property var requests: []
  property int snapshotCount: 0
  readonly property int snapshotLimit: 10000
  readonly property int snapshotPercent: Math.round(Math.max(0, Math.min(snapshotCount, snapshotLimit)) / snapshotLimit * 100)

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function refresh() {
    statusProcess.running = true
    requestProcess.running = true
    snapshotsFile.reload()
  }

  function updateSnapshotCount(raw) {
    try {
      var data = JSON.parse(String(raw || "{}"))
      snapshotCount = data && Array.isArray(data.snapshots) ? data.snapshots.length : 0
    } catch (error) {
      snapshotCount = 0
    }
  }

  function applyRequests(output) {
    var lines = String(output || "").split("\n")
    var found = []
    for (var i = 0; i < lines.length; i++) {
      var match = lines[i].match(/WPLACE_REQUEST path=([^ ]+) status=([0-9]+)/)
      if (match) found.push({ path: match[1], status: Number(match[2]) })
    }
    found.reverse()
    requests = found.slice(0, 5)
  }

  function setServiceState(output) {
    var value = String(output || "").trim()
    serviceState = value === "" ? "unknown" : value
  }

  function toggleService() {
    actionProcess.command = ["systemctl", "--user", serviceState === "active" ? "stop" : "start", "wplace-leaderbot.service"]
    actionProcess.running = true
  }

  function open() {
    panelOpen = true
  }

  function close() {
    panelOpen = false
  }

  function togglePanel() {
    if (panelOpen) close()
    else open()
  }

  function closeForPopoutSwitch() {
    close()
  }

  Process {
    id: statusProcess
    command: ["systemctl", "--user", "is-active", "wplace-leaderbot.service"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.setServiceState(text) }
  }

  Process {
    id: requestProcess
    command: ["journalctl", "--user", "-u", "wplace-leaderbot.service", "--since", "24 hours ago", "-o", "cat", "--no-pager", "-n", "200"]
    stdout: StdioCollector { waitForEnd: true; onStreamFinished: root.applyRequests(text) }
  }

  Process {
    id: actionProcess
    onExited: root.refresh()
  }

  FileView {
    id: snapshotsFile
    path: (Quickshell.env("HOME") || "") + "/wplace-leaderbot/data/snapshots.json"
    watchChanges: true
    printErrors: false
    onLoaded: root.updateSnapshotCount(text())
    onFileChanged: reload()
    onLoadFailed: root.snapshotCount = 0
  }

  Component.onCompleted: refresh()

  Timer {
    interval: 15000
    running: root.panelOpen
    repeat: true
    onTriggered: root.refresh()
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "LB"
    tooltipText: "Wplace Leaderbot"
    active: root.serviceState === "active"
    onPressed: function(buttonCode) {
      if (buttonCode === Qt.LeftButton) {
        if (!root.panelOpen) root.refresh()
        root.togglePanel()
      }
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.panelOpen
    contentWidth: panel.fittedContentWidth(Style.space(340))
    contentHeight: panel.fittedContentHeight(contentColumn.implicitHeight, Style.space(300))

    Column {
      id: contentColumn
      width: panel.contentWidth
      spacing: Style.space(14)

      Row {
        width: parent.width
        spacing: Style.space(10)

        Rectangle {
          width: Style.space(9)
          height: width
          radius: width / 2
          anchors.verticalCenter: parent.verticalCenter
          color: root.serviceState === "active" ? Color.accent : root.dim
        }

        Column {
          width: parent.width - Style.space(130)
          spacing: Style.space(2)
          Text {
            text: "Wplace Leaderbot"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.body
            font.bold: true
          }
          Text {
            text: root.serviceState === "active" ? "Running" : root.serviceState.charAt(0).toUpperCase() + root.serviceState.slice(1)
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
          }
        }

        Rectangle {
          width: Style.space(62)
          height: Style.space(30)
          radius: Style.cornerRadius
          color: root.serviceState === "active"
            ? Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
            : Qt.rgba(Color.accent.r, Color.accent.g, Color.accent.b, 0.24)
          opacity: root.serviceState === "checking" || root.serviceState === "activating" || root.serviceState === "deactivating" ? 0.5 : 1

          Text {
            anchors.fill: parent
            text: root.serviceState === "active" ? "Stop" : "Start"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            horizontalAlignment: Text.AlignHCenter
            verticalAlignment: Text.AlignVCenter
          }

          MouseArea {
            anchors.fill: parent
            enabled: root.serviceState !== "checking" && root.serviceState !== "activating" && root.serviceState !== "deactivating"
            cursorShape: Qt.PointingHandCursor
            onClicked: root.toggleService()
          }
        }
      }

      Rectangle {
        width: parent.width
        height: 1
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
      }

      Column {
        width: parent.width
        spacing: Style.space(8)

        Text {
          text: "RECENT REQUESTS"
          color: root.dim
          font.family: Style.font.family
          font.pixelSize: Style.font.caption
          font.bold: true
          font.letterSpacing: 0.7
        }

        Repeater {
          model: root.requests.length ? root.requests : [{ path: "No recent requests", status: 0 }]
          delegate: Row {
            required property var modelData
            width: parent.width
            spacing: Style.space(10)
            Text {
              width: Style.space(38)
              text: modelData.status ? String(modelData.status) : "—"
              color: modelData.status >= 200 && modelData.status < 300 ? root.foreground : root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              font.bold: true
            }
            Text {
              width: parent.width - Style.space(48)
              text: modelData.path
              color: root.dim
              font.family: Style.font.family
              font.pixelSize: Style.font.caption
              elide: Text.ElideMiddle
            }
          }
        }
      }

      Rectangle {
        width: parent.width
        height: 1
        color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.1)
      }

      Column {
        width: parent.width
        spacing: Style.space(7)

        Row {
          width: parent.width
          Text {
            width: parent.width - snapshotSummary.implicitWidth
            text: "SAVED SNAPSHOTS"
            color: root.dim
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            font.letterSpacing: 0.7
          }
          Text {
            id: snapshotSummary
            text: root.snapshotCount + " / " + root.snapshotLimit + " · " + root.snapshotPercent + "%"
            color: root.foreground
            font.family: Style.font.family
            font.pixelSize: Style.font.caption
            font.bold: true
            horizontalAlignment: Text.AlignRight
          }
        }

        Rectangle {
          width: parent.width
          height: Style.space(6)
          radius: height / 2
          color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.12)

          Rectangle {
            width: parent.width * root.snapshotPercent / 100
            height: parent.height
            radius: parent.radius
            color: Color.accent
            Behavior on width { NumberAnimation { duration: 180; easing.type: Easing.OutCubic } }
          }
        }
      }
    }
  }
}
