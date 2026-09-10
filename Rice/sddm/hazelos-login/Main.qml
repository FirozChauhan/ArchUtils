/*
 *  hazelos-login — the SDDM theme for HAZEL.OS
 *  ─────────────────────────────────────────────────────────────────────────────
 *  Concept:  a black void. The Kufic banner هازل.أوإس hangs dead-center; the
 *            password field is invisible until the first keystroke materialises
 *            it (dashes, no cursor, no label, no borders). Enter logs the
 *            hardcoded user into Hyprland. Wrong password → shake + "nope".
 *
 *  Files:    Main.qml (this), theme.conf (tunable knobs), metadata.desktop.
 *  Deploy:   symlink into /usr/share/sddm/themes/ + [Theme] Current=hazelos-login
 *            NOTE: the greeter runs as user 'sddm' — a home-dir-hosted theme
 *            needs `setfacl -m u:sddm:x /home/asrar` to stay reachable.
 *
 *  Qt 6.11 greeter landmines this file is built around (all hit in the field):
 *    • Assigning a font GROUP then a font PART later  → hard reject.
 *      Use one style only per item (individual font.x: assignments used here).
 *    • Property names that don't exist (e.g. TextInput.cursorWidth) → silent
 *      fallback to the embedded theme. Ground truth: plugins.qmltypes, not memory.
 *    • A visible:false or zero-size ListView never instantiates delegates.
 */

import QtQuick

Rectangle {
    id: root
    color: "black"

    // ══════════════════════════════════════════════════════════════════════
    //  CONFIG — keys come from theme.conf; every one has a hardcoded
    //  fallback so the theme still renders if the conf is mangled.
    //  (Bindings read config.X statically — never wrap these in a helper
    //   function, or change-notification on the property map gets lost.)
    // ══════════════════════════════════════════════════════════════════════
    property string user:     config.user          !== undefined && config.user !== "" ? config.user : "asrar"
    property int    fieldW:   config.inputWidth    !== undefined ? Number(config.inputWidth)     : 660
    property int    fSize:    config.fontPixelSize !== undefined ? Number(config.fontPixelSize)  : 28
    property string family:   config.fontFamily    !== undefined ? config.fontFamily             : ""
    property color  accent:   config.accentColor   !== undefined ? config.accentColor            : "#d8dee9"
    property color  dim:      config.dimColor      !== undefined ? config.dimColor               : "#3b4148"
    property color  fail:     config.failColor     !== undefined ? config.failColor              : "#bf616a"
    property bool   logoOn:   config.showLogo      !== undefined ? config.showLogo == "true"     : true
    property int    logoCell: config.logoCell      !== undefined ? Number(config.logoCell)       : 11
    property int    logoGap:  config.logoGap       !== undefined ? Number(config.logoGap)        : 28

    // ══════════════════════════════════════════════════════════════════════
    //  LOGIN STATE MACHINE
    //    idle ──(Enter, non-empty)──▶ busy ──▶ session starts (or:)
    //    busy ──(loginFailed)──▶ failed: text cleared, field hides again,
    //                            shake plays, "nope" persists until next keystroke
    // ══════════════════════════════════════════════════════════════════════
    property int  sessionIndex: sessionModel ? sessionModel.lastIndex : 0
    property bool busy:   false
    property bool failed: false

    function doLogin() {
        if (busy || field.text.length === 0)
            return
        failed = false
        busy   = true
        sddm.login(root.user, field.text, sessionIndex)
    }

    Connections {
        target: sddm
        function onLoginFailed() {
            busy         = false
            failed       = true
            field.text   = ""                  // field fades back out (opacity rule)
            shake.restart()
            field.forceActiveFocus()           // after restart so focus lands cleanly
        }
        function onLoginSucceeded() {
            busy = true                        // freeze + dim while the session spins up
        }
    }

    // ── session discovery ───────────────────────────────────────────────────
    // Walks the model once at startup and pins sessionIndex to the Hyprland
    // row (name or file match). Deliberately invisible but REALIZED: a zero
    // size or visible:false ListView instantiates nothing.
    ListView {
        x: -100000                            // parked offscreen; opacity is the
        width: 200; height: 200               // real hide — delegates must exist
        opacity: 0
        interactive: false
        model: sessionModel
        delegate: Item {
            Component.onCompleted: {
                var haystack = ((model.name || "") + " " + (model.fileName || "")).toLowerCase()
                if (haystack.indexOf("hyprland") !== -1)
                    root.sessionIndex = index
            }
        }
    }

    // ══════════════════════════════════════════════════════════════════════
    //  LOGO — square-Kufic هازل.أوإس as a pixel matrix.
    //  Generated once offline: Noto Kufi Arabic Black → pango (real RTL
    //  shaping) → ink-dilated → 13×60 grid at ~45% coverage. '#' = filled
    //  cell. Rendered as plain Rectangles: zero font dependency at login.
    //  Detached 1-2 cell squares above the stems are the hamzas/dots —
    //  that separation IS the square-Kufic convention, not an artifact.
    //  To change the text: re-run the raster pipeline and paste a new grid.
    // ══════════════════════════════════════════════════════════════════════
    property var logoArt: [
        "..........................###...............................",
        "..........................###...............................",
        "................#.........###..........#....#...#...........",
        "...............###........###.........###...##.###..#######.",
        "...............###........###.........###......###..########",
        "............##.###..#####.###.........###...##.###..####.###",
        ".....##.###.##.###.######.###.........###..###.###.######.##",
        ".#...##.###.##.###.######.###.....##..###..###.###.######.##",
        "##...#########.###.######.###.##..##..###..###.###.#########",
        "##...#########.###.######.###.##.###..###..###.#############",
        "###.#####..###..##...####.##..##.###..###..###..############",
        "#######........###.######.........######.#####..............",
        ".#####.........###.#####...........#####.####..............."
    ]
    readonly property int logoRows: logoArt.length
    readonly property int logoCols: logoArt[0].length

    // ══════════════════════════════════════════════════════════════════════
    //  LAYOUT — one stage, centered on both axes.
    //  Geometry NEVER changes at runtime (reveal is opacity-only, hints are
    //  Column-invisible items), so nothing on screen ever shifts. The stage
    //  must state its height (col.height) or centerIn centers a zero-height
    //  box and the whole composition sits low — bitten by exactly this.
    // ══════════════════════════════════════════════════════════════════════
    Item {
        id: stage
        anchors.centerIn: parent
        width:  root.fieldW
        height: col.height

        Column {
            id: col
            width: parent.width
            spacing: 0                        // gaps are explicit spacer items

            // ── 1. banner ──────────────────────────────────────────────────
            Item {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.logoOn          // only knob-touchable hide; layout stays
                width:  root.logoCols * root.logoCell
                height: root.logoRows * root.logoCell

                Repeater {
                    model: root.logoRows * root.logoCols
                    delegate: Rectangle {
                        required property int index
                        readonly property int col: index % root.logoCols
                        readonly property int row: Math.floor(index / root.logoCols)
                        visible: root.logoArt[row].charAt(col) === "#"
                        x: col * root.logoCell
                        y: row * root.logoCell
                        width:  root.logoCell
                        height: root.logoCell
                        color:  root.accent
                    }
                }
            }

            Item { width: 1; height: root.logoGap }      // banner ↔ field

            // ── 2. password field (invisible until typed into) ────────────
            Item {
                width:  parent.width
                height: field.height

                TextInput {
                    id: field
                    width:  parent.width
                    height: implicitHeight    // explicit: avoids implicit-height
                                              // feedback into the stage binding
                    focus: true
                    enabled: !root.busy

                    // the reveal: keys still reach an opacity-0 TextInput,
                    // but NOT a visible:false one — do not "simplify" this.
                    opacity: text.length > 0 || root.busy ? 1 : 0
                    Behavior on opacity {
                        NumberAnimation { duration: 140; easing.type: Easing.OutCubic }
                    }

                    // masking: dashes, fully hidden while typing, and a caret
                    // that cannot exist (empty delegate = nothing to render,
                    // even if a repaint re-enables visibility)
                    echoMode: TextInput.Password
                    passwordCharacter: "-"
                    cursorVisible: false
                    cursorDelegate: Item { width: 0; height: 0 }

                    horizontalAlignment: TextInput.AlignHCenter
                    color: root.failed ? root.fail     // error tints the dashes
                         : root.busy   ? root.dim      // frozen while logging in
                         :               root.accent
                    selectionColor:    root.dim
                    selectedTextColor: root.accent

                    font.family:       root.family !== "" ? root.family : Qt.application.font.family
                    font.pixelSize:    root.fSize
                    font.weight:       Font.Light
                    font.letterSpacing: 12

                    onAccepted: root.doLogin()
                    // starting a retry clears the error state (but clearing the
                    // text on failure keeps length 0, so "nope" survives that)
                    onTextChanged: if (text.length > 0) root.failed = false
                }
            }

            Item { width: 1; height: 14 }                // field ↔ hint

            // ── 3. micro-hint: the only text besides the banner ───────────
            Text {
                anchors.horizontalCenter: parent.horizontalCenter
                visible: root.failed || (typeof keyboard !== "undefined" && keyboard.capsLock)
                text: root.failed   ? "nope"
                    : (typeof keyboard !== "undefined" && keyboard.capsLock) ? "caps lock" : ""
                color: root.failed ? root.fail : root.dim
                opacity: 0.8
                font.family:        field.font.family
                font.pixelSize:     10
                font.letterSpacing: 3
            }
        }

        // failure shake — decays inward, always parks at x=0
        SequentialAnimation {
            id: shake
            NumberAnimation { target: col; property: "x"; to: -8; duration: 40 }
            NumberAnimation { target: col; property: "x"; to:  8; duration: 40 }
            NumberAnimation { target: col; property: "x"; to: -4; duration: 40 }
            NumberAnimation { target: col; property: "x"; to:  0; duration: 40 }
        }
    }

    // ── focus helpers: the field should never need clicking, but honour it ──
    MouseArea {
        anchors.fill: parent
        onClicked: field.forceActiveFocus()
    }
    Component.onCompleted: field.forceActiveFocus()
}
