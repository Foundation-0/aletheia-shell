import QtQuick

// Enforces Text.PlainText on every dynamic string so untrusted board/markdown
// text is never interpreted as rich HTML/Qt markup.
Text {
    textFormat: Text.PlainText
    color: theme.foreground
    font.pixelSize: 13
    renderType: Text.NativeRendering
}
