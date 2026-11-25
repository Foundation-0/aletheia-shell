#include <QDir>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QQmlApplicationEngine>
#include <QQmlContext>
#include <QQuickStyle>
#include <QQuickWindow>
#include <QTemporaryDir>
#include <QtTest>

#include "backend.h"
#include "theme.h"
#include "translator.h"

class AletheiaDeckTests : public QObject {
    Q_OBJECT

private slots:
    void themeFallsBackAndReadsAccent() {
        QTemporaryDir dir;
        Theme fallback(dir.path());
        QCOMPARE(fallback.accent(), QString("#FFD60A"));
        QCOMPARE(fallback.accentForeground(), QString("black"));

        QDir(dir.path()).mkpath("theme");
        writeFile(dir.path() + "/theme/colors.toml", "accent = \"#112233\"\nbackground = \"#101216\"\n");
        Theme styled(dir.path());
        QCOMPARE(styled.accent(), QString("#112233"));
        QCOMPARE(styled.accentForeground(), QString("white"));
        QCOMPARE(styled.background(), QString("#101216"));
    }

    void themeFollowsHyprlandRounding() {
        QTemporaryDir dir;
        const QString response = dir.filePath("response.json");
        const QString command = dir.filePath("hyprctl");
        writeFile(command, "#!/bin/sh\ncat '" + response.toUtf8() + "'\n");
        QVERIFY(QFile::setPermissions(command, QFile::ReadOwner | QFile::WriteOwner | QFile::ExeOwner));
        writeFile(response, "{\"int\":0}");

        Theme theme(dir.path(), nullptr, command);
        QCOMPARE(theme.radius(), 0);
        writeFile(response, "{\"int\":8}");
        QTRY_COMPARE(theme.radius(), 8);
    }

    void translatorFollowsLanguageAndPlurals() {
        QTemporaryDir dir;
        writeFile(dir.path() + "/language", "da\n");
        Translator tr(dir.path() + "/language", ":/i18n");
        QCOMPARE(tr.language(), QString("da"));
        QCOMPARE(tr.translate("Main", "Sleep All"), QString("Sluk skærme"));
        QCOMPARE(tr.translate("Main", "%n messages", nullptr, 1), QString("%n besked"));
        QCOMPARE(tr.translate("Main", "%n messages", nullptr, 5), QString("%n beskeder"));
    }

    void backendLoadsFeedFiltersAndGpuLease() {
        QTemporaryDir dir;
        const QString stateDir = dir.filePath("state");
        const QString cartoDir = dir.filePath("cartography");
        QDir().mkpath(stateDir);
        QDir().mkpath(cartoDir);

        writeFile(stateDir + "/swarm_feed.json", R"({
            "title": "Test Swarm",
            "head": 3042,
            "workers": [
                {"name": "os-deck", "state": "working", "model": "Gemini 4 Neon", "last_post_seq": 3040},
                {"name": "fractals-probe", "state": "waiting", "model": "Gemini 4 Carbon", "last_post_seq": 3033}
            ],
            "messages": [
                {"seq": 3040, "agent": "os-deck", "time": "15:30:00", "preview": "Phase 1 done", "text": "[done] Phase 1", "tags": ["done"]},
                {"seq": 3041, "agent": "fractals-probe", "time": "15:31:00", "preview": "GPU audit", "text": "RTX 3080 AUDIT-210", "tags": ["gpu", "audit"]}
            ],
            "gpu_lease": {"held": true, "holder": {"mode": "train", "holder_pid": 9999}}
        })");

        DeckBackend backend({stateDir, cartoDir, QString()});
        QCOMPARE(backend.swarmHead(), 3042);
        QCOMPARE(backend.workingCount(), 1);
        QCOMPARE(backend.filteredMessages().size(), 2);
        QVERIFY(backend.gpuHeld());
        QVERIFY(backend.gpuHolder().contains("9999"));

        backend.setMessageFilter("gpu");
        QCOMPARE(backend.filteredMessages().size(), 1);
        QCOMPARE(backend.filteredMessages().first().toMap().value("seq").toInt(), 3041);
    }

    void backendConflictSafeWorkspaceMergeAndInboxAck() {
        QTemporaryDir dir;
        const QString stateDir = dir.filePath("state");
        const QString cartoDir = dir.filePath("cartography");
        QDir().mkpath(stateDir);
        QDir().mkpath(cartoDir);

        const QString docPath = cartoDir + "/SHARED_WORKSPACE.md";
        writeFile(docPath,
            "# Shared\n\n"
            "## Coordinator Live Pinboard\n\n- Old Pin\n\n"
            "## Shared Canvas (Edit Live — Both PI & Coordinator)\n\nInitial text\n\n"
            "## PI Quick Notes & Insights\n\n-\n\n"
            "## Active Track Priorities\n\n1. S110\n");

        DeckBackend backend({stateDir, cartoDir, QString()});
        QCOMPARE(backend.workspacePinboard(), QString("- Old Pin"));
        QCOMPARE(backend.workspaceCanvas(), QString("Initial text"));

        // PI starts editing Shared Canvas (marks dirty, not yet saved)
        backend.setCanvasText("PI live thought in progress");
        QVERIFY(backend.workspaceDirty());

        // Simultaneously Coordinator updates Pinboard on disk!
        writeFile(docPath,
            "# Shared\n\n"
            "## Coordinator Live Pinboard\n\n- Updated Pin from Coordinator!\n\n"
            "## Shared Canvas (Edit Live — Both PI & Coordinator)\n\nInitial text\n\n"
            "## PI Quick Notes & Insights\n\n-\n\n"
            "## Active Track Priorities\n\n1. S110\n");

        backend.refreshNow();
        // Pinboard updates immediately while PI's unsaved Shared Canvas stays untouched!
        QCOMPARE(backend.workspacePinboard(), QString("- Updated Pin from Coordinator!"));
        QCOMPARE(backend.workspaceCanvas(), QString("PI live thought in progress"));

        // Saving merges latest Pinboard + PI's Canvas atomically without clobbering either!
        QVERIFY(backend.saveWorkspaceNow());
        QFile after(docPath);
        QVERIFY(after.open(QIODevice::ReadOnly));
        const QString finalDoc = QString::fromUtf8(after.readAll());
        QVERIFY(finalDoc.contains("- Updated Pin from Coordinator!"));
        QVERIFY(finalDoc.contains("PI live thought in progress"));

        // Inbox send -> Queued -> Ack -> Read by Coordinator
        QVERIFY(backend.sendToCoordinator("Check S111 next", "message"));
        QCOMPARE(backend.sentMessages().size(), 1);
        const QString msgId = backend.sentMessages().first().toMap().value("id").toString();
        QCOMPARE(backend.sentMessages().first().toMap().value("delivered").toBool(), false);

        writeFile(stateDir + "/inbox_ack.json",
                  QStringLiteral("{\"read_ids\":[\"%1\"]}\n").arg(msgId).toUtf8());
        backend.refreshNow();
        QCOMPARE(backend.sentMessages().first().toMap().value("delivered").toBool(), true);

        // Oversized inbox text is bounded safely below 8 KiB
        const QString huge(10000, QLatin1Char('Z'));
        QVERIFY(backend.sendToCoordinator(huge, "message"));
        QVERIFY(backend.sentMessages().first().toMap().value("text").toString().size() <= 1800);
    }

    void backendParsesQueueAndPinsCard() {
        QTemporaryDir dir;
        const QString stateDir = dir.filePath("state");
        const QString cartoDir = dir.filePath("cartography");
        QDir().mkpath(stateDir);
        QDir().mkpath(cartoDir);

        writeFile(cartoDir + "/EXPERIMENTATION_QUEUE.md",
            "| Queue ID | Date | Insight | Protocol | Workers | Status |\n"
            "|---|---|---|---|---|---|\n"
            "| **`EXP-Q01`** | `2026-11-25` | First | `P1` | `@a` | **`COMPLETE`** |\n"
            "| **`EXP-Q11`** | `2026-11-25` | Active | `P2` | `@b` | **`RUNNING LIVE`** |\n"
            "| **`EXP-Q12`** | `2026-11-25` | Next | `P3` | `@c` | **`QUEUED`** |\n");

        DeckBackend backend({stateDir, cartoDir, QString()});
        QCOMPARE(backend.queueCards().size(), 3);
        QCOMPARE(backend.queueCards().first().toMap().value("id").toString(), QString("EXP-Q11"));

        backend.pinQueueCard("EXP-Q12");
        QCOMPARE(backend.queueCards().first().toMap().value("id").toString(), QString("EXP-Q12"));
        QCOMPARE(backend.queueCards().first().toMap().value("pinned").toBool(), true);
    }

    void qmlLoadsCleanlyOffscreen() {
        QQuickStyle::setStyle("Material");
        QTemporaryDir dir;
        Theme theme(dir.path());
        Translator translator(dir.path() + "/language", ":/i18n");
        DeckBackend backend({dir.filePath("state"), dir.filePath("carto"), QString()});

        QQmlApplicationEngine engine;
        engine.rootContext()->setContextProperty("translator", &translator);
        engine.rootContext()->setContextProperty("theme", &theme);
        engine.rootContext()->setContextProperty("backend", &backend);
        engine.load(QUrl("qrc:/Main.qml"));
        QVERIFY(!engine.rootObjects().isEmpty());
        auto *win = qobject_cast<QQuickWindow *>(engine.rootObjects().first());
        QVERIFY(win != nullptr);
    }

private:
    static void writeFile(const QString &path, const QByteArray &contents) {
        QFile file(path);
        QVERIFY(file.open(QIODevice::WriteOnly));
        QCOMPARE(file.write(contents), contents.size());
    }
};

QTEST_MAIN(AletheiaDeckTests)
#include "aletheia-deck_tests.moc"
