#pragma once

#include <QFileSystemWatcher>
#include <QObject>
#include <QSet>
#include <QStringList>
#include <QTimer>
#include <QVariantList>

struct WorkspaceSections {
    QString preamble;
    QString pinboardHeader = QStringLiteral("## Coordinator Live Pinboard");
    QString pinboard;
    QString canvasHeader = QStringLiteral("## Shared Canvas (Edit Live — Both PI & Coordinator)");
    QString canvas;
    QString piNotesHeader = QStringLiteral("## PI Quick Notes & Insights");
    QString piNotes;
    QString prioritiesHeader = QStringLiteral("## Active Track Priorities");
    QString priorities;
    QList<QPair<QString, QString>> extraSections;
};

class DeckBackend : public QObject {
    Q_OBJECT

    Q_PROPERTY(QString swarmTitle READ swarmTitle NOTIFY feedChanged)
    Q_PROPERTY(int swarmHead READ swarmHead NOTIFY feedChanged)
    Q_PROPERTY(QString feedUpdatedUtc READ feedUpdatedUtc NOTIFY feedChanged)
    Q_PROPERTY(QVariantList workers READ workers NOTIFY feedChanged)
    Q_PROPERTY(int workingCount READ workingCount NOTIFY feedChanged)
    Q_PROPERTY(QString messageFilter READ messageFilter WRITE setMessageFilter NOTIFY filterChanged)
    Q_PROPERTY(QVariantList filteredMessages READ filteredMessages NOTIFY feedChanged)
    Q_PROPERTY(bool gpuHeld READ gpuHeld NOTIFY feedChanged)
    Q_PROPERTY(QString gpuHolder READ gpuHolder NOTIFY feedChanged)

    Q_PROPERTY(QVariantList queueCards READ queueCards NOTIFY queueChanged)
    Q_PROPERTY(int runningQueueCount READ runningQueueCount NOTIFY queueChanged)
    Q_PROPERTY(int queuedCount READ queuedCount NOTIFY queueChanged)
    Q_PROPERTY(int shippedQueueCount READ shippedQueueCount NOTIFY queueChanged)

    Q_PROPERTY(QString workspacePinboard READ workspacePinboard NOTIFY workspaceChanged)
    Q_PROPERTY(QString workspaceCanvas READ workspaceCanvas NOTIFY workspaceChanged)
    Q_PROPERTY(QString workspacePiNotes READ workspacePiNotes NOTIFY workspaceChanged)
    Q_PROPERTY(QString workspacePriorities READ workspacePriorities NOTIFY workspaceChanged)
    Q_PROPERTY(QString workspaceSaveState READ workspaceSaveState NOTIFY workspaceSaveStateChanged)
    Q_PROPERTY(bool workspaceDirty READ workspaceDirty NOTIFY workspaceSaveStateChanged)

    Q_PROPERTY(QVariantList sentMessages READ sentMessages NOTIFY inboxChanged)
    Q_PROPERTY(QString statusBanner READ statusBanner NOTIFY statusBannerChanged)

public:
    struct Paths {
        QString stateDir;
        QString cartographyDir;
        QString displayBin;
    };

    explicit DeckBackend(QObject *parent = nullptr);
    explicit DeckBackend(const Paths &paths, QObject *parent = nullptr);

    QString swarmTitle() const { return m_swarmTitle; }
    int swarmHead() const { return m_swarmHead; }
    QString feedUpdatedUtc() const { return m_feedUpdatedUtc; }
    QVariantList workers() const { return m_workers; }
    int workingCount() const { return m_workingCount; }
    QString messageFilter() const { return m_messageFilter; }
    void setMessageFilter(const QString &filter);
    QVariantList filteredMessages() const { return m_filteredMessages; }
    bool gpuHeld() const { return m_gpuHeld; }
    QString gpuHolder() const { return m_gpuHolder; }

    QVariantList queueCards() const { return m_queueCards; }
    int runningQueueCount() const { return m_runningQueueCount; }
    int queuedCount() const { return m_queuedCount; }
    int shippedQueueCount() const { return m_shippedQueueCount; }

    QString workspacePinboard() const { return m_sections.pinboard; }
    QString workspaceCanvas() const { return m_sections.canvas; }
    QString workspacePiNotes() const { return m_sections.piNotes; }
    QString workspacePriorities() const { return m_sections.priorities; }
    QString workspaceSaveState() const { return m_workspaceSaveState; }
    bool workspaceDirty() const { return m_workspaceDirty; }

    QVariantList sentMessages() const { return m_sentMessages; }
    QString statusBanner() const { return m_statusBanner; }

    Q_INVOKABLE void setCanvasText(const QString &text);
    Q_INVOKABLE void setPiNotesText(const QString &text);
    Q_INVOKABLE void setPrioritiesText(const QString &text);
    Q_INVOKABLE bool saveWorkspaceNow();

    Q_INVOKABLE bool sendToCoordinator(const QString &text, const QString &kind = QStringLiteral("message"));
    Q_INVOKABLE void pinQueueCard(const QString &queueId);
    Q_INVOKABLE bool flagQueueCard(const QString &queueId, const QString &note);
    Q_INVOKABLE bool addTrackIdea(const QString &ideaText);

    Q_INVOKABLE void sleepAllScreens();
    Q_INVOKABLE void toggleScreenPad();
    Q_INVOKABLE void refreshNow();
    Q_INVOKABLE void dismissBanner();

    static WorkspaceSections parseWorkspaceMarkdown(const QString &markdown);
    static QString serializeWorkspaceMarkdown(const WorkspaceSections &sections);
    static QVariantList parseQueueMarkdown(const QString &markdown, const QStringList &pinnedOrder);

signals:
    void feedChanged();
    void filterChanged();
    void queueChanged();
    void workspaceChanged();
    void workspaceSaveStateChanged();
    void inboxChanged();
    void statusBannerChanged();

private:
    void init();
    void rearmWatcher();
    void reloadFeed();
    void reloadQueue();
    void reloadWorkspace(bool allowOverwriteEditable);
    void reloadInboxAndAck();
    void loadPinnedPrefs();
    void savePinnedPrefs();
    void rebuildFilteredMessages();
    void setStatusBanner(const QString &msg);
    static bool atomicWriteFile(const QString &path, const QByteArray &data, QString *errorOut = nullptr);

    QString m_stateDir;
    QString m_cartographyDir;
    QString m_displayBin;
    QString m_feedPath;
    QString m_ackPath;
    QString m_prefsPath;
    QString m_workspacePath;
    QString m_inboxPath;
    QString m_queuePath;

    QFileSystemWatcher m_watcher;
    QTimer m_watchDebounce;
    QTimer m_pollTimer;
    QTimer m_saveDebounce;

    QString m_swarmTitle = QStringLiteral("Aletheia Swarm");
    int m_swarmHead = 0;
    QString m_feedUpdatedUtc;
    QVariantList m_workers;
    int m_workingCount = 0;
    QVariantList m_allMessages;
    QVariantList m_filteredMessages;
    QString m_messageFilter = QStringLiteral("all");
    bool m_gpuHeld = false;
    QString m_gpuHolder;

    QStringList m_pinnedIds;
    QVariantList m_queueCards;
    int m_runningQueueCount = 0;
    int m_queuedCount = 0;
    int m_shippedQueueCount = 0;

    WorkspaceSections m_sections;
    QByteArray m_lastWorkspaceHash;
    QString m_workspaceSaveState = QStringLiteral("Saved");
    bool m_workspaceDirty = false;
    bool m_canvasDirty = false;
    bool m_piNotesDirty = false;
    bool m_prioritiesDirty = false;

    QVariantList m_sentMessages;
    QString m_statusBanner;
};
