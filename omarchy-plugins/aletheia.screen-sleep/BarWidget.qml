import QtQuick
import qs.Commons
import qs.Commons as Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "aletheia.screen-sleep"

  property bool busy: false

  Commons.Translations {
    id: strings
    directory: Qt.resolvedUrl("i18n")
  }

  function sanitizePlainText(raw) {
    return String(raw || "").replace(/[<>]/g, "")
  }

  function triggerCommand(cmd) {
    if (!root.bar || !cmd) return
    root.busy = true
    busyTimer.restart()
    root.bar.run(cmd)
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  Timer {
    id: busyTimer
    interval: 600
    repeat: false
    onTriggered: root.busy = false
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // Nerd Font crescent moon glyph (\uf186): screen sleep (wake on any activity).
    text: "\uf186"
    slotSize: Style.bar.statusSlot
    tooltipText: root.sanitizePlainText(strings.t("tooltip.main"))
    opacity: root.busy ? 0.55 : 1.0

    Behavior on opacity {
      NumberAnimation {
        duration: Style.duration(180)
        easing.type: Easing.OutCubic
      }
    }

    onPressed: function(b) {
      if (b === Qt.RightButton) {
        root.triggerCommand("$HOME/.local/bin/aletheion-display toggle-pad")
      } else if (b === Qt.MiddleButton) {
        root.triggerCommand("$HOME/.local/bin/aletheion-display focus-deck")
      } else {
        root.triggerCommand("$HOME/.local/bin/aletheion-display sleep-all --no-notify")
      }
    }
  }
}
