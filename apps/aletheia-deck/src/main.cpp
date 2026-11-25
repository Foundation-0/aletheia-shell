// aletheia-deck — Bi-directional ScreenPad Plus interactive swarm & research deck for Omarchy.

#include <QGuiApplication>
#include <QIcon>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QUrl>

#include "backend.h"
#include "theme.h"
#include "translator.h"

int main(int argc, char *argv[]) {
    QGuiApplication app(argc, argv);
    app.setApplicationName("aletheia-deck");
    app.setApplicationVersion("0.1.0");

    // Matches the window to aletheia-deck.desktop, so Hyprland (Wayland app_id)
    // and the Omarchy launcher pick up the installed icon and window rules.
    app.setDesktopFileName("aletheia-deck");
    app.setWindowIcon(QIcon::fromTheme("aletheia-deck"));

    QQuickStyle::setStyle("Material");

    // Installed before anything is translated, and every qsTr() binding is
    // re-evaluated live when the Omarchy language changes.
    Translator translator;
    app.installTranslator(&translator);

    Theme theme;
    DeckBackend backend;

    QQmlApplicationEngine engine;
    QObject::connect(&translator, &Translator::changed, &engine, &QQmlEngine::retranslate);
    engine.rootContext()->setContextProperty("translator", &translator);
    engine.rootContext()->setContextProperty("theme", &theme);
    engine.rootContext()->setContextProperty("backend", &backend);
    engine.load(QUrl("qrc:/Main.qml"));
    if (engine.rootObjects().isEmpty())
        return 1;

    return app.exec();
}
