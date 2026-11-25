import QtQuick
import QtQuick.Controls
import QtQuick.Controls.Material
import QtQuick.Layouts

ApplicationWindow {
    id: win
    width: 1920
    height: 550
    minimumWidth: 960
    minimumHeight: 420
    visible: true
    title: qsTr("Aletheia Deck")
    color: theme.background

    Material.theme: Material.Dark
    Material.accent: theme.accent
    Material.roundedScale: theme.radius

    property bool helpVisible: false
    property int expandedMsgSeq: -1
    property string flaggingCardId: ""
    property bool addIdeaOpen: false
    property int activeDocTab: 1 // 0=Pinboard, 1=Shared Doc, 2=PI Notes, 3=Priorities

    readonly property bool textInputFocused: win.activeFocusItem !== null
        && win.activeFocusItem.cursorPosition !== undefined

    readonly property int smallRadius: Math.min(4, theme.radius)

    function statusColor(kind) {
        if (kind === "running") return theme.accent
        if (kind === "queued") return "#60a5fa"
        if (kind === "shipped") return "#34d399"
        if (kind === "complete") return "#a78bfa"
        return theme.muted
    }

    function shortModel(model) {
        var s = String(model || "").trim()
        if (!s) return ""
        if (s.indexOf("neon") !== -1 || s.indexOf("Neon") !== -1) return "Neon"
        if (s.indexOf("carbon") !== -1 || s.indexOf("Carbon") !== -1) return "Carbon"
        if (s.indexOf("claude") !== -1 || s.indexOf("Claude") !== -1) return "Claude"
        if (s.length > 14) return s.slice(0, 14)
        return s
    }

    function submitCoordinatorMessage() {
        var txt = coordInput.text.trim()
        if (!txt) return
        if (backend.sendToCoordinator(txt, "message")) {
            coordInput.text = ""
        }
    }

    Shortcut {
        sequence: "?"
        context: Qt.ApplicationShortcut
        enabled: !win.textInputFocused
        onActivated: win.helpVisible = !win.helpVisible
    }
    Shortcut {
        sequence: "Escape"
        context: Qt.ApplicationShortcut
        enabled: win.helpVisible
        onActivated: win.helpVisible = false
    }
    Shortcut {
        sequence: "Q"
        context: Qt.ApplicationShortcut
        enabled: !win.textInputFocused
        onActivated: {
            if (backend.workspaceDirty) backend.saveWorkspaceNow()
            Qt.quit()
        }
    }
    Shortcut {
        sequence: "Ctrl+Q"
        context: Qt.ApplicationShortcut
        onActivated: {
            if (backend.workspaceDirty) backend.saveWorkspaceNow()
            Qt.quit()
        }
    }
    Shortcut {
        sequence: "Ctrl+S"
        context: Qt.ApplicationShortcut
        onActivated: backend.saveWorkspaceNow()
    }
    Shortcut {
        sequence: "Ctrl+Return"
        context: Qt.ApplicationShortcut
        onActivated: win.submitCoordinatorMessage()
    }
    Shortcut {
        sequence: "Ctrl+Enter"
        context: Qt.ApplicationShortcut
        onActivated: win.submitCoordinatorMessage()
    }
    Shortcut {
        sequence: "F5"
        context: Qt.ApplicationShortcut
        onActivated: backend.refreshNow()
    }

    Item {
        id: rootItem
        anchors.fill: parent
        LayoutMirroring.enabled: Qt.application.layoutDirection === Qt.RightToLeft
        LayoutMirroring.childrenInherit: true

        ColumnLayout {
            anchors.fill: parent
            spacing: 0

            // Slim 36px Header Bar
            Rectangle {
                Layout.fillWidth: true
                Layout.preferredHeight: 38
                color: theme.surface
                border.width: 0

                Rectangle {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    height: 1
                    color: theme.border
                }

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 10
                    spacing: 10

                    Rectangle {
                        width: 10
                        height: 10
                        radius: 5
                        color: theme.accent
                    }

                    PlainLabel {
                        text: qsTr("Aletheia Deck")
                        font.pixelSize: 14
                        font.bold: true
                        color: theme.foreground
                    }

                    Rectangle {
                        radius: win.smallRadius
                        color: theme.surfaceElevated
                        border.color: theme.border
                        border.width: 1
                        implicitHeight: 24
                        implicitWidth: seqBadgeLabel.implicitWidth + 14

                        PlainLabel {
                            id: seqBadgeLabel
                            anchors.centerIn: parent
                            text: backend.swarmHead > 0
                                ? qsTr("Board #%1 · %2 active").arg(backend.swarmHead).arg(backend.workingCount)
                                : qsTr("Board waiting")
                            font.pixelSize: 11
                            color: theme.muted
                        }
                    }

                    // Compact RTX 3080 lease badge (ONLY active/idle pill, no bloated machine stats)
                    Rectangle {
                        radius: win.smallRadius
                        color: backend.gpuHeld ? Qt.alpha(theme.accent, 0.16) : theme.surfaceElevated
                        border.color: backend.gpuHeld ? theme.accent : theme.border
                        border.width: 1
                        implicitHeight: 24
                        implicitWidth: gpuRow.implicitWidth + 14

                        RowLayout {
                            id: gpuRow
                            anchors.centerIn: parent
                            spacing: 6

                            Rectangle {
                                width: 7
                                height: 7
                                radius: 4
                                color: backend.gpuHeld ? theme.accent : "#6b7280"
                            }

                            PlainLabel {
                                text: backend.gpuHeld
                                    ? qsTr("RTX 3080: Active (%1)").arg(backend.gpuHolder || qsTr("leased"))
                                    : qsTr("RTX 3080: Idle")
                                font.pixelSize: 11
                                font.bold: backend.gpuHeld
                                color: backend.gpuHeld ? theme.accent : theme.muted
                            }
                        }
                    }

                    Item { Layout.fillWidth: true }

                    // Touch-friendly Header Controls (custom pill buttons for crisp zero-clip sizing)
                    Rectangle {
                        implicitHeight: 30
                        implicitWidth: sleepBtnLbl.implicitWidth + 22
                        radius: win.smallRadius
                        color: sleepMa.pressed ? Qt.alpha(theme.accent, 0.25) : theme.surfaceElevated
                        border.color: theme.border
                        border.width: 1

                        PlainLabel {
                            id: sleepBtnLbl
                            anchors.centerIn: parent
                            text: qsTr("Sleep All")
                            font.pixelSize: 12
                            font.bold: true
                            color: theme.foreground
                        }

                        MouseArea {
                            id: sleepMa
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: backend.sleepAllScreens()
                        }
                    }

                    Rectangle {
                        implicitHeight: 30
                        implicitWidth: padBtnLbl.implicitWidth + 22
                        radius: win.smallRadius
                        color: padMa.pressed ? Qt.alpha(theme.accent, 0.25) : theme.surfaceElevated
                        border.color: theme.border
                        border.width: 1

                        PlainLabel {
                            id: padBtnLbl
                            anchors.centerIn: parent
                            text: qsTr("Pad Off")
                            font.pixelSize: 12
                            font.bold: true
                            color: theme.foreground
                        }

                        MouseArea {
                            id: padMa
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: backend.toggleScreenPad()
                        }
                    }

                    Rectangle {
                        implicitHeight: 30
                        implicitWidth: 34
                        radius: win.smallRadius
                        color: win.helpVisible ? theme.accent : (helpMa.pressed ? Qt.alpha(theme.accent, 0.25) : theme.surfaceElevated)
                        border.color: win.helpVisible ? theme.accent : theme.border
                        border.width: 1

                        PlainLabel {
                            anchors.centerIn: parent
                            text: "?"
                            font.pixelSize: 13
                            font.bold: true
                            color: win.helpVisible ? theme.accentForeground : theme.foreground
                        }

                        MouseArea {
                            id: helpMa
                            anchors.fill: parent
                            cursorShape: Qt.PointingHandCursor
                            onClicked: win.helpVisible = !win.helpVisible
                        }
                    }
                }
            }

            // Optional Error / Diagnostic Banner ("Say what's wrong")
            Rectangle {
                visible: backend.statusBanner.length > 0
                Layout.fillWidth: true
                Layout.preferredHeight: visible ? 32 : 0
                color: "#451a1a"

                RowLayout {
                    anchors.fill: parent
                    anchors.leftMargin: 12
                    anchors.rightMargin: 8
                    spacing: 8

                    PlainLabel {
                        Layout.fillWidth: true
                        text: backend.statusBanner
                        color: "#fecaca"
                        font.pixelSize: 12
                        elide: Text.ElideRight
                    }

                    Button {
                        text: qsTr("Dismiss")
                        flat: true
                        implicitHeight: 26
                        font.pixelSize: 11
                        Material.foreground: "#fecaca"
                        onClicked: backend.dismissBanner()
                    }
                }
            }

            // Ultra-Wide 3-Pane Touch-First Workspace (34% / 31% / 35%)
            RowLayout {
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 8
                spacing: 8

                // =============================================================
                // LEFT PANE (~34%) — Live Swarm Board & Worker Roster
                // =============================================================
                Rectangle {
                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    Layout.preferredWidth: 34
                    color: theme.surface
                    border.color: theme.border
                    border.width: 1
                    radius: theme.radius

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            PlainLabel {
                                text: qsTr("Swarm Board")
                                font.pixelSize: 13
                                font.bold: true
                            }

                            Item { Layout.fillWidth: true }

                            PlainLabel {
                                text: qsTr("%n messages", "", backend.filteredMessages.length)
                                font.pixelSize: 11
                                color: theme.muted
                            }
                        }

                        // Horizontal Worker Pill Strip
                        ListView {
                            id: workerStrip
                            Layout.fillWidth: true
                            Layout.preferredHeight: 34
                            orientation: ListView.Horizontal
                            spacing: 6
                            clip: true
                            model: backend.workers

                            delegate: Rectangle {
                                required property var modelData
                                readonly property bool isWorking: modelData.state === "working"
                                height: 32
                                width: workerRow.implicitWidth + 16
                                radius: win.smallRadius
                                color: isWorking ? Qt.alpha(theme.accent, 0.16) : theme.surfaceElevated
                                border.color: isWorking ? theme.accent : theme.border
                                border.width: 1

                                RowLayout {
                                    id: workerRow
                                    anchors.centerIn: parent
                                    spacing: 5

                                    Rectangle {
                                        width: 7
                                        height: 7
                                        radius: 4
                                        color: isWorking ? theme.accent : "#6b7280"

                                        SequentialAnimation on opacity {
                                            running: isWorking
                                            loops: Animation.Infinite
                                            NumberAnimation { from: 1.0; to: 0.35; duration: 750 }
                                            NumberAnimation { from: 0.35; to: 1.0; duration: 750 }
                                        }
                                    }

                                    PlainLabel {
                                        text: "@" + modelData.name
                                        font.pixelSize: 11
                                        font.bold: isWorking
                                        color: isWorking ? theme.foreground : theme.muted
                                    }

                                    PlainLabel {
                                        visible: win.shortModel(modelData.model) !== ""
                                        text: win.shortModel(modelData.model)
                                        font.pixelSize: 10
                                        color: isWorking ? theme.accent : theme.muted
                                    }
                                }
                            }
                        }

                        // Filter Pills (generous 34px touch height)
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            Repeater {
                                model: [
                                    { key: "all", label: qsTr("All") },
                                    { key: "done", label: qsTr("Done") },
                                    { key: "gpu", label: qsTr("GPU") },
                                    { key: "audit", label: qsTr("Audits") }
                                ]

                                delegate: Rectangle {
                                    required property var modelData
                                    readonly property bool selected: backend.messageFilter === modelData.key
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 34
                                    radius: win.smallRadius
                                    color: selected ? theme.accent : theme.surfaceElevated
                                    border.color: selected ? theme.accent : theme.border
                                    border.width: 1

                                    PlainLabel {
                                        anchors.centerIn: parent
                                        text: modelData.label
                                        font.pixelSize: 12
                                        font.bold: selected
                                        color: selected ? theme.accentForeground : theme.foreground
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        preventStealing: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: backend.messageFilter = modelData.key
                                    }
                                }
                            }
                        }

                        // Live Swarm Feed (newest first at top for instant glanceability)
                        ListView {
                            id: boardList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            boundsBehavior: Flickable.StopAtBounds
                            model: backend.filteredMessages

                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            delegate: Rectangle {
                                required property var modelData
                                readonly property bool expanded: win.expandedMsgSeq === modelData.seq
                                width: boardList.width
                                height: msgCol.implicitHeight + 16
                                radius: win.smallRadius
                                color: expanded ? theme.surfaceElevated : Qt.alpha(theme.surfaceElevated, 0.55)
                                border.color: expanded ? theme.accent : theme.border
                                border.width: 1

                                ColumnLayout {
                                    id: msgCol
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: 8
                                    spacing: 4

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 6

                                        PlainLabel {
                                            text: "#" + modelData.seq
                                            font.pixelSize: 11
                                            font.bold: true
                                            color: theme.accent
                                        }

                                        PlainLabel {
                                            text: "@" + modelData.agent
                                            font.pixelSize: 12
                                            font.bold: true
                                            color: theme.foreground
                                        }

                                        Repeater {
                                            model: modelData.tags
                                            delegate: Rectangle {
                                                required property string modelData
                                                radius: Math.min(3, theme.radius)
                                                color: Qt.alpha(theme.accent, 0.18)
                                                implicitHeight: 18
                                                implicitWidth: tagLbl.implicitWidth + 10
                                                PlainLabel {
                                                    id: tagLbl
                                                    anchors.centerIn: parent
                                                    text: modelData.toUpperCase()
                                                    font.pixelSize: 9
                                                    font.bold: true
                                                    color: theme.accent
                                                }
                                            }
                                        }

                                        Item { Layout.fillWidth: true }

                                        PlainLabel {
                                            text: String(modelData.time || "").slice(-8)
                                            font.pixelSize: 10
                                            color: theme.muted
                                        }
                                    }

                                    PlainLabel {
                                        Layout.fillWidth: true
                                        text: expanded ? modelData.text : modelData.preview
                                        font.pixelSize: 12
                                        color: expanded ? theme.foreground : theme.muted
                                        wrapMode: Text.Wrap
                                        maximumLineCount: expanded ? 40 : 2
                                        elide: Text.ElideRight
                                    }
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        win.expandedMsgSeq = (win.expandedMsgSeq === modelData.seq) ? -1 : modelData.seq
                                    }
                                }
                            }
                        }
                    }
                }

                // =============================================================
                // CENTER PANE (~31%) — Interactive Research To-Do & Track Queue
                // =============================================================
                Rectangle {
                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    Layout.preferredWidth: 31
                    color: theme.surface
                    border.color: theme.border
                    border.width: 1
                    radius: theme.radius

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 6

                            PlainLabel {
                                text: qsTr("Track Queue")
                                font.pixelSize: 13
                                font.bold: true
                            }

                            PlainLabel {
                                text: qsTr("%1 live · %2 queued").arg(backend.runningQueueCount).arg(backend.queuedCount)
                                font.pixelSize: 11
                                color: theme.muted
                            }

                            Item { Layout.fillWidth: true }

                            Rectangle {
                                implicitHeight: 34
                                implicitWidth: addIdeaBtnLabel.implicitWidth + 18
                                radius: win.smallRadius
                                color: win.addIdeaOpen ? theme.accent : theme.surfaceElevated
                                border.color: theme.accent
                                border.width: 1

                                PlainLabel {
                                    id: addIdeaBtnLabel
                                    anchors.centerIn: parent
                                    text: qsTr("+ New Track / Idea")
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: win.addIdeaOpen ? theme.accentForeground : theme.accent
                                }

                                MouseArea {
                                    anchors.fill: parent
                                    preventStealing: true
                                    cursorShape: Qt.PointingHandCursor
                                    onClicked: {
                                        win.addIdeaOpen = !win.addIdeaOpen
                                        if (win.addIdeaOpen) ideaField.forceActiveFocus()
                                    }
                                }
                            }
                        }

                        // Expandable + New Track / Idea Quick-Add Bar (SOP-12)
                        Rectangle {
                            visible: win.addIdeaOpen
                            Layout.fillWidth: true
                            Layout.preferredHeight: visible ? 48 : 0
                            radius: win.smallRadius
                            color: theme.surfaceElevated
                            border.color: theme.accent
                            border.width: 1

                            RowLayout {
                                anchors.fill: parent
                                anchors.margins: 4
                                spacing: 6

                                TextField {
                                    id: ideaField
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 40
                                    maximumLength: 1800
                                    placeholderText: qsTr("New hypothesis or track idea (SOP-12)...")
                                    font.pixelSize: 12
                                    color: theme.foreground
                                    Material.roundedScale: win.smallRadius
                                    onAccepted: addIdeaConfirm.clicked()
                                }

                                Button {
                                    id: addIdeaConfirm
                                    text: qsTr("Add")
                                    implicitHeight: 38
                                    Material.background: theme.accent
                                    Material.foreground: theme.accentForeground
                                    Material.roundedScale: win.smallRadius
                                    onClicked: {
                                        if (backend.addTrackIdea(ideaField.text)) {
                                            ideaField.text = ""
                                            win.addIdeaOpen = false
                                        }
                                    }
                                }
                            }
                        }

                        // Interactive Research Queue Cards
                        ListView {
                            id: queueList
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            clip: true
                            spacing: 6
                            boundsBehavior: Flickable.StopAtBounds
                            model: backend.queueCards

                            ScrollBar.vertical: ScrollBar { policy: ScrollBar.AsNeeded }

                            delegate: Rectangle {
                                required property var modelData
                                readonly property bool isFlagging: win.flaggingCardId === modelData.id
                                width: queueList.width
                                height: cardCol.implicitHeight + 16
                                radius: win.smallRadius
                                color: modelData.pinned ? Qt.alpha(theme.accent, 0.10) : theme.surfaceElevated
                                border.color: modelData.pinned ? theme.accent : theme.border
                                border.width: 1

                                ColumnLayout {
                                    id: cardCol
                                    anchors.left: parent.left
                                    anchors.right: parent.right
                                    anchors.top: parent.top
                                    anchors.margins: 8
                                    spacing: 5

                                    RowLayout {
                                        Layout.fillWidth: true
                                        spacing: 6

                                        Rectangle {
                                            radius: Math.min(3, theme.radius)
                                            color: Qt.alpha(win.statusColor(modelData.statusKind), 0.22)
                                            implicitHeight: 20
                                            implicitWidth: idLbl.implicitWidth + 10
                                            PlainLabel {
                                                id: idLbl
                                                anchors.centerIn: parent
                                                text: modelData.id
                                                font.pixelSize: 11
                                                font.bold: true
                                                color: win.statusColor(modelData.statusKind)
                                            }
                                        }

                                        PlainLabel {
                                            Layout.fillWidth: true
                                            text: modelData.status
                                            font.pixelSize: 11
                                            color: win.statusColor(modelData.statusKind)
                                            elide: Text.ElideRight
                                        }

                                        // Touch Action Buttons (>=34px height, >=52px width)
                                        Rectangle {
                                            implicitHeight: 30
                                            implicitWidth: pinLbl.implicitWidth + 14
                                            radius: win.smallRadius
                                            color: modelData.pinned ? theme.accent : theme.surface
                                            border.color: modelData.pinned ? theme.accent : theme.border
                                            border.width: 1

                                            PlainLabel {
                                                id: pinLbl
                                                anchors.centerIn: parent
                                                text: modelData.pinned ? qsTr("Pinned") : qsTr("Pin ↑")
                                                font.pixelSize: 11
                                                font.bold: modelData.pinned
                                                color: modelData.pinned ? theme.accentForeground : theme.foreground
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                preventStealing: true
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: backend.pinQueueCard(modelData.id)
                                            }
                                        }

                                        Rectangle {
                                            implicitHeight: 30
                                            implicitWidth: flagLbl.implicitWidth + 14
                                            radius: win.smallRadius
                                            color: isFlagging ? theme.accent : theme.surface
                                            border.color: isFlagging ? theme.accent : theme.border
                                            border.width: 1

                                            PlainLabel {
                                                id: flagLbl
                                                anchors.centerIn: parent
                                                text: qsTr("Note")
                                                font.pixelSize: 11
                                                color: isFlagging ? theme.accentForeground : theme.foreground
                                            }

                                            MouseArea {
                                                anchors.fill: parent
                                                preventStealing: true
                                                cursorShape: Qt.PointingHandCursor
                                                onClicked: {
                                                    win.flaggingCardId = isFlagging ? "" : modelData.id
                                                }
                                            }
                                        }
                                    }

                                    PlainLabel {
                                        Layout.fillWidth: true
                                        text: modelData.insight
                                        font.pixelSize: 12
                                        color: theme.foreground
                                        wrapMode: Text.Wrap
                                        maximumLineCount: 2
                                        elide: Text.ElideRight
                                    }

                                    PlainLabel {
                                        Layout.fillWidth: true
                                        text: modelData.workers
                                        font.pixelSize: 11
                                        color: theme.muted
                                        elide: Text.ElideRight
                                    }

                                    // Inline Flag/Note Touch Bar
                                    RowLayout {
                                        visible: isFlagging
                                        Layout.fillWidth: true
                                        spacing: 6

                                        TextField {
                                            id: cardNoteField
                                            Layout.fillWidth: true
                                            Layout.preferredHeight: 38
                                            maximumLength: 1800
                                            placeholderText: qsTr("Note for %1 -> Coordinator...").arg(modelData.id)
                                            font.pixelSize: 12
                                            color: theme.foreground
                                            Material.roundedScale: win.smallRadius
                                            onAccepted: sendCardNoteBtn.clicked()
                                        }

                                        Button {
                                            id: sendCardNoteBtn
                                            text: qsTr("Flag")
                                            implicitHeight: 36
                                            Material.background: theme.accent
                                            Material.foreground: theme.accentForeground
                                            Material.roundedScale: win.smallRadius
                                            onClicked: {
                                                if (backend.flagQueueCard(modelData.id, cardNoteField.text)) {
                                                    cardNoteField.text = ""
                                                    win.flaggingCardId = ""
                                                }
                                            }
                                        }
                                    }
                                }
                            }
                        }
                    }
                }

                // =============================================================
                // RIGHT PANE (~35%) — Bi-Directional Shared Workspace + Coordinator Bar
                // =============================================================
                Rectangle {
                    Layout.fillHeight: true
                    Layout.fillWidth: true
                    Layout.preferredWidth: 35
                    color: theme.surface
                    border.color: theme.border
                    border.width: 1
                    radius: theme.radius

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 8
                        spacing: 6

                        // Section Jump Tabs + Save Status
                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 5

                            Repeater {
                                model: [
                                    { idx: 0, label: qsTr("Pinboard") },
                                    { idx: 1, label: qsTr("Shared Doc") },
                                    { idx: 2, label: qsTr("PI Notes") },
                                    { idx: 3, label: qsTr("Priorities") }
                                ]

                                delegate: Rectangle {
                                    required property var modelData
                                    readonly property bool selected: win.activeDocTab === modelData.idx
                                    Layout.preferredHeight: 34
                                    implicitWidth: tabLbl.implicitWidth + 16
                                    radius: win.smallRadius
                                    color: selected ? theme.accent : theme.surfaceElevated
                                    border.color: selected ? theme.accent : theme.border
                                    border.width: 1

                                    PlainLabel {
                                        id: tabLbl
                                        anchors.centerIn: parent
                                        text: modelData.label
                                        font.pixelSize: 12
                                        font.bold: selected
                                        color: selected ? theme.accentForeground : theme.foreground
                                    }

                                    MouseArea {
                                        anchors.fill: parent
                                        preventStealing: true
                                        cursorShape: Qt.PointingHandCursor
                                        onClicked: win.activeDocTab = modelData.idx
                                    }
                                }
                            }

                            Item { Layout.fillWidth: true }

                            PlainLabel {
                                text: backend.workspaceSaveState
                                font.pixelSize: 11
                                color: backend.workspaceDirty ? theme.accent : theme.muted
                            }
                        }

                        // Top (~76%) — Shared Workspace Document Surface
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            Layout.preferredHeight: 76
                            radius: win.smallRadius
                            color: theme.background
                            border.color: theme.border
                            border.width: 1
                            clip: true

                            StackLayout {
                                anchors.fill: parent
                                anchors.margins: 6
                                currentIndex: win.activeDocTab

                                // Tab 0: Coordinator Live Pinboard (read-only live synced)
                                ScrollView {
                                    clip: true
                                    PlainLabel {
                                        width: parent.availableWidth
                                        text: backend.workspacePinboard || qsTr("Coordinator Live Pinboard is empty.")
                                        font.pixelSize: 13
                                        lineHeight: 1.35
                                        wrapMode: Text.Wrap
                                        color: theme.foreground
                                    }
                                }

                                // Tab 1: Shared Canvas (live editable, conflict-safe auto-save)
                                ScrollView {
                                    clip: true
                                    TextArea {
                                        id: canvasEdit
                                        textFormat: TextEdit.PlainText
                                        wrapMode: TextEdit.Wrap
                                        font.pixelSize: 13
                                        color: theme.foreground
                                        placeholderText: qsTr("Write shared notes, sketches, or steering thoughts...")
                                        background: null
                                        Component.onCompleted: text = backend.workspaceCanvas
                                        onTextEdited: backend.setCanvasText(text)
                                        Connections {
                                            target: backend
                                            function onWorkspaceChanged() {
                                                if (canvasEdit.text !== backend.workspaceCanvas)
                                                    canvasEdit.text = backend.workspaceCanvas
                                            }
                                        }
                                    }
                                }

                                // Tab 2: PI Quick Notes & Insights
                                ScrollView {
                                    clip: true
                                    TextArea {
                                        id: piNotesEdit
                                        textFormat: TextEdit.PlainText
                                        wrapMode: TextEdit.Wrap
                                        font.pixelSize: 13
                                        color: theme.foreground
                                        placeholderText: qsTr("Capture quick insights for SOP-12...")
                                        background: null
                                        Component.onCompleted: text = backend.workspacePiNotes
                                        onTextEdited: backend.setPiNotesText(text)
                                        Connections {
                                            target: backend
                                            function onWorkspaceChanged() {
                                                if (piNotesEdit.text !== backend.workspacePiNotes)
                                                    piNotesEdit.text = backend.workspacePiNotes
                                            }
                                        }
                                    }
                                }

                                // Tab 3: Active Track Priorities
                                ScrollView {
                                    clip: true
                                    TextArea {
                                        id: prioritiesEdit
                                        textFormat: TextEdit.PlainText
                                        wrapMode: TextEdit.Wrap
                                        font.pixelSize: 13
                                        color: theme.foreground
                                        placeholderText: qsTr("Active track priority order...")
                                        background: null
                                        Component.onCompleted: text = backend.workspacePriorities
                                        onTextEdited: backend.setPrioritiesText(text)
                                        Connections {
                                            target: backend
                                            function onWorkspaceChanged() {
                                                if (prioritiesEdit.text !== backend.workspacePriorities)
                                                    prioritiesEdit.text = backend.workspacePriorities
                                            }
                                        }
                                    }
                                }
                            }
                        }

                        // Bottom (~24%) — Instant "Write to Coordinator" Touch Bar + Delivery Status
                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 96
                            radius: win.smallRadius
                            color: theme.surfaceElevated
                            border.color: theme.border
                            border.width: 1

                            ColumnLayout {
                                anchors.fill: parent
                                anchors.margins: 6
                                spacing: 4

                                RowLayout {
                                    Layout.fillWidth: true
                                    spacing: 6

                                    TextField {
                                        id: coordInput
                                        Layout.fillWidth: true
                                        Layout.preferredHeight: 42
                                        maximumLength: 1800
                                        placeholderText: qsTr("Write directly to @coordinator (Ctrl+Enter)...")
                                        font.pixelSize: 13
                                        color: theme.foreground
                                        Material.roundedScale: win.smallRadius
                                        onAccepted: win.submitCoordinatorMessage()
                                    }

                                    Rectangle {
                                        implicitHeight: 42
                                        implicitWidth: Math.max(82, sendBtnLbl.implicitWidth + 28)
                                        radius: win.smallRadius
                                        color: sendBtnMa.pressed ? Qt.darker(theme.accent, 1.15) : theme.accent

                                        PlainLabel {
                                            id: sendBtnLbl
                                            anchors.centerIn: parent
                                            text: qsTr("Send")
                                            font.pixelSize: 13
                                            font.bold: true
                                            color: theme.accentForeground
                                        }

                                        MouseArea {
                                            id: sendBtnMa
                                            anchors.fill: parent
                                            preventStealing: true
                                            cursorShape: Qt.PointingHandCursor
                                            onClicked: win.submitCoordinatorMessage()
                                        }
                                    }
                                }

                                // Compact Delivery Receipt Strip (Queued -> Read by Coordinator)
                                ListView {
                                    id: receiptList
                                    Layout.fillWidth: true
                                    Layout.fillHeight: true
                                    clip: true
                                    spacing: 3
                                    model: backend.sentMessages

                                    delegate: RowLayout {
                                        required property var modelData
                                        width: receiptList.width
                                        spacing: 6

                                        Rectangle {
                                            radius: Math.min(3, theme.radius)
                                            color: modelData.delivered ? Qt.alpha("#34d399", 0.20) : Qt.alpha(theme.accent, 0.20)
                                            implicitHeight: 18
                                            implicitWidth: stLbl.implicitWidth + 10

                                            PlainLabel {
                                                id: stLbl
                                                anchors.centerIn: parent
                                                text: modelData.status
                                                font.pixelSize: 10
                                                font.bold: true
                                                color: modelData.delivered ? "#34d399" : theme.accent
                                            }
                                        }

                                        PlainLabel {
                                            Layout.fillWidth: true
                                            text: modelData.text
                                            font.pixelSize: 11
                                            color: theme.muted
                                            elide: Text.ElideRight
                                        }
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }

        // Keyboard Shortcuts Help Overlay (?)
        Rectangle {
            visible: win.helpVisible
            anchors.fill: parent
            color: Qt.rgba(0, 0, 0, 0.72)
            z: 20

            MouseArea {
                anchors.fill: parent
                onClicked: win.helpVisible = false
            }

            Rectangle {
                anchors.centerIn: parent
                width: Math.min(480, parent.width - 40)
                height: helpCol.implicitHeight + 32
                radius: theme.radius
                color: theme.surface
                border.color: theme.accent
                border.width: 1

                ColumnLayout {
                    id: helpCol
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 16
                    spacing: 8

                    RowLayout {
                        Layout.fillWidth: true
                        PlainLabel {
                            text: qsTr("Keyboard Shortcuts")
                            font.pixelSize: 15
                            font.bold: true
                        }
                        Item { Layout.fillWidth: true }
                        Button {
                            text: qsTr("Close")
                            flat: true
                            onClicked: win.helpVisible = false
                        }
                    }

                    Repeater {
                        model: [
                            { key: "?", desc: qsTr("Toggle this shortcut overlay") },
                            { key: "Ctrl+S", desc: qsTr("Save Shared Workspace immediately") },
                            { key: "Ctrl+Enter", desc: qsTr("Send message to @coordinator") },
                            { key: "F5", desc: qsTr("Refresh swarm board & track queue") },
                            { key: "Q / Ctrl+Q", desc: qsTr("Save & quit Aletheia Deck") }
                        ]

                        delegate: RowLayout {
                            required property var modelData
                            Layout.fillWidth: true
                            spacing: 12

                            Rectangle {
                                implicitWidth: 104
                                implicitHeight: 26
                                radius: win.smallRadius
                                color: theme.surfaceElevated
                                border.color: theme.border
                                border.width: 1
                                PlainLabel {
                                    anchors.centerIn: parent
                                    text: modelData.key
                                    font.pixelSize: 12
                                    font.bold: true
                                    color: theme.accent
                                }
                            }

                            PlainLabel {
                                Layout.fillWidth: true
                                text: modelData.desc
                                font.pixelSize: 13
                                color: theme.foreground
                            }
                        }
                    }
                }
            }
        }
    }
}
