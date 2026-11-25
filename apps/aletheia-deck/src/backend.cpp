#include "backend.h"

#include <QCryptographicHash>
#include <QDateTime>
#include <QDir>
#include <QFile>
#include <QFileInfo>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QProcess>
#include <QRegularExpression>
#include <QSaveFile>
#include <QStandardPaths>
#include <QUuid>

namespace {

QString stripMarkdownTokens(QString s) {
    static const QRegularExpression re(QStringLiteral("[*`]+"));
    return s.remove(re).trimmed();
}

QString defaultStateDirectory() {
    const QString xdg = qEnvironmentVariable("XDG_STATE_HOME").trimmed();
    if (!xdg.isEmpty())
        return xdg + QStringLiteral("/aletheia-deck");
    return QDir::homePath() + QStringLiteral("/.local/state/aletheia-deck");
}

QString defaultCartographyDirectory() {
    const QString env = qEnvironmentVariable("ALETHEIA_CARTOGRAPHY_DIR").trimmed();
    if (!env.isEmpty())
        return env;
    return QDir::homePath() + QStringLiteral("/Projects/expedition/cartography");
}

QString defaultDisplayBinary() {
    const QString local = QDir::homePath() + QStringLiteral("/.local/bin/aletheion-display");
    if (QFileInfo::exists(local))
        return local;
    return QStandardPaths::findExecutable(QStringLiteral("aletheion-display"));
}

} // namespace

DeckBackend::DeckBackend(QObject *parent)
    : DeckBackend(Paths{defaultStateDirectory(), defaultCartographyDirectory(), defaultDisplayBinary()}, parent) {}

DeckBackend::DeckBackend(const Paths &paths, QObject *parent)
    : QObject(parent),
      m_stateDir(paths.stateDir.isEmpty() ? defaultStateDirectory() : paths.stateDir),
      m_cartographyDir(paths.cartographyDir.isEmpty() ? defaultCartographyDirectory() : paths.cartographyDir),
      m_displayBin(paths.displayBin) {
    init();
}

void DeckBackend::init() {
    m_feedPath = m_stateDir + QStringLiteral("/swarm_feed.json");
    m_ackPath = m_stateDir + QStringLiteral("/inbox_ack.json");
    m_prefsPath = m_stateDir + QStringLiteral("/deck_prefs.json");
    m_workspacePath = m_cartographyDir + QStringLiteral("/SHARED_WORKSPACE.md");
    m_inboxPath = m_cartographyDir + QStringLiteral("/PI_INBOX.jsonl");
    m_queuePath = m_cartographyDir + QStringLiteral("/EXPERIMENTATION_QUEUE.md");

    QDir().mkpath(m_stateDir);

    m_watchDebounce.setSingleShot(true);
    m_watchDebounce.setInterval(120);
    connect(&m_watcher, &QFileSystemWatcher::fileChanged, this, [this] { m_watchDebounce.start(); });
    connect(&m_watcher, &QFileSystemWatcher::directoryChanged, this, [this] { m_watchDebounce.start(); });
    connect(&m_watchDebounce, &QTimer::timeout, this, &DeckBackend::refreshNow);

    // Fallback poll ensures atomic os.replace swaps are always noticed even if inotify drops a file handle.
    m_pollTimer.setInterval(1500);
    connect(&m_pollTimer, &QTimer::timeout, this, &DeckBackend::refreshNow);
    m_pollTimer.start();

    // Auto-save debounce for Shared Workspace edits (600 ms).
    m_saveDebounce.setSingleShot(true);
    m_saveDebounce.setInterval(600);
    connect(&m_saveDebounce, &QTimer::timeout, this, [this] { saveWorkspaceNow(); });

    loadPinnedPrefs();
    refreshNow();
}

void DeckBackend::rearmWatcher() {
    const QStringList existing = m_watcher.files() + m_watcher.directories();
    if (!existing.isEmpty())
        m_watcher.removePaths(existing);

    QStringList targets{m_stateDir, m_cartographyDir, m_feedPath, m_ackPath, m_workspacePath, m_inboxPath, m_queuePath};
    targets.removeDuplicates();
    for (const QString &p : targets) {
        if (QFileInfo::exists(p))
            m_watcher.addPath(p);
    }
}

void DeckBackend::refreshNow() {
    rearmWatcher();
    reloadFeed();
    reloadQueue();
    reloadWorkspace(false);
    reloadInboxAndAck();
}

void DeckBackend::setStatusBanner(const QString &msg) {
    if (m_statusBanner == msg)
        return;
    m_statusBanner = msg;
    emit statusBannerChanged();
}

void DeckBackend::dismissBanner() {
    setStatusBanner(QString());
}

void DeckBackend::setMessageFilter(const QString &filter) {
    const QString norm = filter.trimmed().toLower();
    if (norm.isEmpty() || norm == m_messageFilter)
        return;
    m_messageFilter = norm;
    emit filterChanged();
    rebuildFilteredMessages();
    emit feedChanged();
}

void DeckBackend::rebuildFilteredMessages() {
    if (m_messageFilter == QLatin1String("all")) {
        m_filteredMessages = m_allMessages;
        return;
    }
    QVariantList out;
    out.reserve(m_allMessages.size());
    for (const QVariant &v : m_allMessages) {
        const QVariantMap m = v.toMap();
        const QStringList tags = m.value(QStringLiteral("tags")).toStringList();
        if (tags.contains(m_messageFilter))
            out.append(m);
    }
    m_filteredMessages = out;
}

void DeckBackend::reloadFeed() {
    QFile file(m_feedPath);
    if (!file.open(QIODevice::ReadOnly))
        return;
    const QJsonDocument doc = QJsonDocument::fromJson(file.readAll());
    if (!doc.isObject())
        return;
    const QJsonObject obj = doc.object();

    m_swarmTitle = obj.value(QStringLiteral("title")).toString(QStringLiteral("Aletheia Swarm"));
    m_swarmHead = obj.value(QStringLiteral("head")).toInt(0);
    m_feedUpdatedUtc = obj.value(QStringLiteral("updated_utc")).toString();

    QVariantList workerList;
    int working = 0;
    const QJsonArray wArr = obj.value(QStringLiteral("workers")).toArray();
    for (const QJsonValue &wv : wArr) {
        const QJsonObject wo = wv.toObject();
        const QString state = wo.value(QStringLiteral("state")).toString(QStringLiteral("waiting"));
        if (state == QLatin1String("working"))
            ++working;
        QVariantMap wm;
        wm.insert(QStringLiteral("name"), wo.value(QStringLiteral("name")).toString());
        wm.insert(QStringLiteral("state"), state);
        wm.insert(QStringLiteral("model"), wo.value(QStringLiteral("model")).toString());
        wm.insert(QStringLiteral("lastPostSeq"), wo.value(QStringLiteral("last_post_seq")).toInt(0));
        wm.insert(QStringLiteral("lastPostTime"), wo.value(QStringLiteral("last_post_time")).toString());
        workerList.append(wm);
    }
    m_workers = workerList;
    m_workingCount = working;

    QVariantList msgList;
    const QJsonArray mArr = obj.value(QStringLiteral("messages")).toArray();
    // Show newest messages first at the top of the ScreenPad feed for zero-scroll glanceability.
    for (int i = mArr.size() - 1; i >= 0; --i) {
        const QJsonObject mo = mArr.at(i).toObject();
        QStringList tags;
        for (const QJsonValue &tv : mo.value(QStringLiteral("tags")).toArray())
            tags.append(tv.toString());
        QVariantMap mm;
        mm.insert(QStringLiteral("seq"), mo.value(QStringLiteral("seq")).toInt(0));
        mm.insert(QStringLiteral("agent"), mo.value(QStringLiteral("agent")).toString());
        mm.insert(QStringLiteral("time"), mo.value(QStringLiteral("time")).toString());
        mm.insert(QStringLiteral("preview"), mo.value(QStringLiteral("preview")).toString());
        mm.insert(QStringLiteral("text"), mo.value(QStringLiteral("text")).toString());
        mm.insert(QStringLiteral("tags"), tags);
        msgList.append(mm);
    }
    m_allMessages = msgList;

    const QJsonObject gpu = obj.value(QStringLiteral("gpu_lease")).toObject();
    m_gpuHeld = gpu.value(QStringLiteral("held")).toBool(false);
    const QJsonValue holderVal = gpu.value(QStringLiteral("holder"));
    if (holderVal.isString()) {
        m_gpuHolder = holderVal.toString();
    } else if (holderVal.isObject()) {
        const QJsonObject ho = holderVal.toObject();
        const QString mode = ho.value(QStringLiteral("mode")).toString();
        const int pid = ho.value(QStringLiteral("holder_pid")).toInt(0);
        const QString tag = ho.value(QStringLiteral("holder")).toString();
        if (!tag.isEmpty())
            m_gpuHolder = tag;
        else if (!mode.isEmpty() && pid > 0)
            m_gpuHolder = QStringLiteral("%1 · PID %2").arg(mode).arg(pid);
        else if (!mode.isEmpty())
            m_gpuHolder = mode;
        else
            m_gpuHolder = QStringLiteral("active");
    } else {
        m_gpuHolder.clear();
    }

    rebuildFilteredMessages();
    emit feedChanged();
}

QVariantList DeckBackend::parseQueueMarkdown(const QString &markdown, const QStringList &pinnedOrder) {
    QVariantList parsed;
    const QStringList lines = markdown.split(QLatin1Char('\n'));
    for (const QString &rawLine : lines) {
        const QString line = rawLine.trimmed();
        if (!line.startsWith(QLatin1Char('|')))
            continue;
        QString trimmed = line;
        if (trimmed.startsWith(QLatin1Char('|')))
            trimmed.remove(0, 1);
        if (trimmed.endsWith(QLatin1Char('|')))
            trimmed.chop(1);
        const QStringList cells = trimmed.split(QLatin1Char('|'));
        if (cells.size() < 6)
            continue;
        const QString qid = stripMarkdownTokens(cells.at(0));
        if (!qid.startsWith(QStringLiteral("EXP-Q")))
            continue;

        const QString statusRaw = stripMarkdownTokens(cells.at(5));
        const QString su = statusRaw.toUpper();
        QString statusKind = QStringLiteral("other");
        int rank = 3;
        if (su.contains(QStringLiteral("RUNNING"))) {
            statusKind = QStringLiteral("running");
            rank = 0;
        } else if (su.contains(QStringLiteral("QUEUED")) || su.contains(QStringLiteral("PREFLIGHT"))) {
            statusKind = QStringLiteral("queued");
            rank = 1;
        } else if (su.contains(QStringLiteral("SHIPPED")) || su.contains(QStringLiteral("LIVE"))) {
            statusKind = QStringLiteral("shipped");
            rank = 2;
        } else if (su.contains(QStringLiteral("COMPLETE"))) {
            statusKind = QStringLiteral("complete");
            rank = 3;
        }

        const int pinIdx = pinnedOrder.indexOf(qid);
        QVariantMap card;
        card.insert(QStringLiteral("id"), qid);
        card.insert(QStringLiteral("date"), stripMarkdownTokens(cells.at(1)));
        card.insert(QStringLiteral("insight"), stripMarkdownTokens(cells.at(2)).left(260));
        card.insert(QStringLiteral("protocol"), stripMarkdownTokens(cells.at(3)).left(140));
        card.insert(QStringLiteral("workers"), stripMarkdownTokens(cells.at(4)).left(120));
        card.insert(QStringLiteral("status"), statusRaw.left(160));
        card.insert(QStringLiteral("statusKind"), statusKind);
        card.insert(QStringLiteral("pinned"), pinIdx >= 0);
        card.insert(QStringLiteral("_pinIdx"), pinIdx);
        card.insert(QStringLiteral("_rank"), rank);
        parsed.append(card);
    }

    std::stable_sort(parsed.begin(), parsed.end(), [](const QVariant &a, const QVariant &b) {
        const QVariantMap ma = a.toMap();
        const QVariantMap mb = b.toMap();
        const int pa = ma.value(QStringLiteral("_pinIdx")).toInt();
        const int pb = mb.value(QStringLiteral("_pinIdx")).toInt();
        if (pa >= 0 && pb >= 0)
            return pa < pb;
        if (pa >= 0)
            return true;
        if (pb >= 0)
            return false;
        return ma.value(QStringLiteral("_rank")).toInt() < mb.value(QStringLiteral("_rank")).toInt();
    });
    return parsed;
}

void DeckBackend::reloadQueue() {
    QFile file(m_queuePath);
    if (!file.open(QIODevice::ReadOnly))
        return;
    const QString text = QString::fromUtf8(file.readAll());
    m_queueCards = parseQueueMarkdown(text, m_pinnedIds);

    int running = 0, queued = 0, shipped = 0;
    for (const QVariant &v : m_queueCards) {
        const QString k = v.toMap().value(QStringLiteral("statusKind")).toString();
        if (k == QLatin1String("running"))
            ++running;
        else if (k == QLatin1String("queued"))
            ++queued;
        else if (k == QLatin1String("shipped") || k == QLatin1String("complete"))
            ++shipped;
    }
    m_runningQueueCount = running;
    m_queuedCount = queued;
    m_shippedQueueCount = shipped;
    emit queueChanged();
}

WorkspaceSections DeckBackend::parseWorkspaceMarkdown(const QString &markdown) {
    WorkspaceSections out;
    const QStringList lines = markdown.split(QLatin1Char('\n'));

    enum class Bucket { Preamble, Pinboard, Canvas, PiNotes, Priorities, Extra };
    Bucket current = Bucket::Preamble;
    QString currentHeader;
    QStringList buf;

    auto flush = [&]() {
        const QString body = buf.join(QLatin1Char('\n')).trimmed();
        switch (current) {
        case Bucket::Preamble:
            out.preamble = body;
            break;
        case Bucket::Pinboard:
            out.pinboardHeader = currentHeader;
            out.pinboard = body;
            break;
        case Bucket::Canvas:
            out.canvasHeader = currentHeader;
            out.canvas = body;
            break;
        case Bucket::PiNotes:
            out.piNotesHeader = currentHeader;
            out.piNotes = body;
            break;
        case Bucket::Priorities:
            out.prioritiesHeader = currentHeader;
            out.priorities = body;
            break;
        case Bucket::Extra:
            out.extraSections.append(qMakePair(currentHeader, body));
            break;
        }
        buf.clear();
    };

    for (const QString &line : lines) {
        if (line.startsWith(QStringLiteral("## "))) {
            flush();
            currentHeader = line.trimmed();
            if (currentHeader.startsWith(QStringLiteral("## Coordinator Live Pinboard")))
                current = Bucket::Pinboard;
            else if (currentHeader.startsWith(QStringLiteral("## Shared Canvas")))
                current = Bucket::Canvas;
            else if (currentHeader.startsWith(QStringLiteral("## PI Quick Notes")))
                current = Bucket::PiNotes;
            else if (currentHeader.startsWith(QStringLiteral("## Active Track Priorities")))
                current = Bucket::Priorities;
            else
                current = Bucket::Extra;
        } else {
            buf.append(line);
        }
    }
    flush();

    if (out.preamble.isEmpty())
        out.preamble = QStringLiteral("# Aletheia Shared Workspace (Live Bi-Directional PI <-> Coordinator)");
    return out;
}

QString DeckBackend::serializeWorkspaceMarkdown(const WorkspaceSections &sections) {
    QStringList chunks;
    chunks.append(sections.preamble.trimmed());
    chunks.append(sections.pinboardHeader + QStringLiteral("\n\n") + sections.pinboard.trimmed());
    chunks.append(sections.canvasHeader + QStringLiteral("\n\n") + sections.canvas.trimmed());
    chunks.append(sections.piNotesHeader + QStringLiteral("\n\n") + sections.piNotes.trimmed());
    chunks.append(sections.prioritiesHeader + QStringLiteral("\n\n") + sections.priorities.trimmed());
    for (const auto &extra : sections.extraSections)
        chunks.append(extra.first + QStringLiteral("\n\n") + extra.second.trimmed());
    return chunks.join(QStringLiteral("\n\n")) + QStringLiteral("\n");
}

void DeckBackend::reloadWorkspace(bool allowOverwriteEditable) {
    QFile file(m_workspacePath);
    if (!file.open(QIODevice::ReadOnly))
        return;
    const QByteArray raw = file.readAll();
    const QByteArray hash = QCryptographicHash::hash(raw, QCryptographicHash::Sha256);
    if (!allowOverwriteEditable && hash == m_lastWorkspaceHash)
        return;

    const WorkspaceSections disk = parseWorkspaceMarkdown(QString::fromUtf8(raw));
    bool changed = false;

    // Always merge Coordinator Live Pinboard, preamble, and extra sections immediately
    // without clobbering any section the PI is currently editing!
    if (m_sections.preamble != disk.preamble || m_sections.pinboard != disk.pinboard ||
        m_sections.pinboardHeader != disk.pinboardHeader || m_sections.extraSections != disk.extraSections) {
        m_sections.preamble = disk.preamble;
        m_sections.pinboardHeader = disk.pinboardHeader;
        m_sections.pinboard = disk.pinboard;
        m_sections.extraSections = disk.extraSections;
        changed = true;
    }

    if (allowOverwriteEditable || !m_canvasDirty) {
        if (m_sections.canvas != disk.canvas || m_sections.canvasHeader != disk.canvasHeader) {
            m_sections.canvasHeader = disk.canvasHeader;
            m_sections.canvas = disk.canvas;
            changed = true;
        }
    }
    if (allowOverwriteEditable || !m_piNotesDirty) {
        if (m_sections.piNotes != disk.piNotes || m_sections.piNotesHeader != disk.piNotesHeader) {
            m_sections.piNotesHeader = disk.piNotesHeader;
            m_sections.piNotes = disk.piNotes;
            changed = true;
        }
    }
    if (allowOverwriteEditable || !m_prioritiesDirty) {
        if (m_sections.priorities != disk.priorities || m_sections.prioritiesHeader != disk.prioritiesHeader) {
            m_sections.prioritiesHeader = disk.prioritiesHeader;
            m_sections.priorities = disk.priorities;
            changed = true;
        }
    }

    m_lastWorkspaceHash = hash;
    if (changed) {
        emit workspaceChanged();
        if (!m_workspaceDirty) {
            m_workspaceSaveState = QStringLiteral("Synced");
            emit workspaceSaveStateChanged();
        }
    }
}

void DeckBackend::setCanvasText(const QString &text) {
    if (text == m_sections.canvas)
        return;
    m_sections.canvas = text;
    m_canvasDirty = true;
    m_workspaceDirty = true;
    m_workspaceSaveState = QStringLiteral("Saving...");
    emit workspaceSaveStateChanged();
    m_saveDebounce.start();
}

void DeckBackend::setPiNotesText(const QString &text) {
    if (text == m_sections.piNotes)
        return;
    m_sections.piNotes = text;
    m_piNotesDirty = true;
    m_workspaceDirty = true;
    m_workspaceSaveState = QStringLiteral("Saving...");
    emit workspaceSaveStateChanged();
    m_saveDebounce.start();
}

void DeckBackend::setPrioritiesText(const QString &text) {
    if (text == m_sections.priorities)
        return;
    m_sections.priorities = text;
    m_prioritiesDirty = true;
    m_workspaceDirty = true;
    m_workspaceSaveState = QStringLiteral("Saving...");
    emit workspaceSaveStateChanged();
    m_saveDebounce.start();
}

bool DeckBackend::atomicWriteFile(const QString &path, const QByteArray &data, QString *errorOut) {
    const QFileInfo fi(path);
    QDir().mkpath(fi.absolutePath());
    QSaveFile out(path);
    if (!out.open(QIODevice::WriteOnly)) {
        if (errorOut)
            *errorOut = out.errorString();
        return false;
    }
    if (out.write(data) != data.size()) {
        out.cancelWriting();
        if (errorOut)
            *errorOut = QStringLiteral("short write");
        return false;
    }
    if (!out.commit()) {
        if (errorOut)
            *errorOut = out.errorString();
        return false;
    }
    return true;
}

bool DeckBackend::saveWorkspaceNow() {
    m_saveDebounce.stop();

    // Conflict-safe merge: re-read latest disk Pinboard & untouched sections before writing atomically
    // so Coordinator Pinboard updates are never lost when the PI auto-saves.
    QFile existing(m_workspacePath);
    if (existing.open(QIODevice::ReadOnly)) {
        const WorkspaceSections disk = parseWorkspaceMarkdown(QString::fromUtf8(existing.readAll()));
        m_sections.preamble = disk.preamble;
        m_sections.pinboardHeader = disk.pinboardHeader;
        m_sections.pinboard = disk.pinboard;
        m_sections.extraSections = disk.extraSections;
        if (!m_canvasDirty)
            m_sections.canvas = disk.canvas;
        if (!m_piNotesDirty)
            m_sections.piNotes = disk.piNotes;
        if (!m_prioritiesDirty)
            m_sections.priorities = disk.priorities;
    }

    const QByteArray payload = serializeWorkspaceMarkdown(m_sections).toUtf8();
    QString err;
    if (!atomicWriteFile(m_workspacePath, payload, &err)) {
        m_workspaceSaveState = QStringLiteral("Save failed");
        emit workspaceSaveStateChanged();
        setStatusBanner(tr("Could not save SHARED_WORKSPACE.md: %1").arg(err));
        return false;
    }

    m_lastWorkspaceHash = QCryptographicHash::hash(payload, QCryptographicHash::Sha256);
    m_workspaceDirty = false;
    m_canvasDirty = false;
    m_piNotesDirty = false;
    m_prioritiesDirty = false;
    m_workspaceSaveState = QStringLiteral("Saved");
    emit workspaceSaveStateChanged();
    emit workspaceChanged();
    return true;
}

void DeckBackend::reloadInboxAndAck() {
    QSet<QString> readIds;
    QFile ackFile(m_ackPath);
    if (ackFile.open(QIODevice::ReadOnly)) {
        const QJsonObject obj = QJsonDocument::fromJson(ackFile.readAll()).object();
        for (const QJsonValue &v : obj.value(QStringLiteral("read_ids")).toArray())
            readIds.insert(v.toString());
    }

    QVariantList items;
    QFile inbox(m_inboxPath);
    if (inbox.open(QIODevice::ReadOnly)) {
        const QList<QByteArray> lines = inbox.readAll().split('\n');
        for (const QByteArray &raw : lines) {
            const QByteArray trimmed = raw.trimmed();
            if (trimmed.isEmpty())
                continue;
            const QJsonObject obj = QJsonDocument::fromJson(trimmed).object();
            if (obj.isEmpty())
                continue;
            const QString id = obj.value(QStringLiteral("id")).toString();
            const bool isRead = !id.isEmpty() && readIds.contains(id);
            QVariantMap row;
            row.insert(QStringLiteral("id"), id);
            row.insert(QStringLiteral("tsUtc"), obj.value(QStringLiteral("ts_utc")).toString());
            row.insert(QStringLiteral("kind"), obj.value(QStringLiteral("kind")).toString(QStringLiteral("message")));
            row.insert(QStringLiteral("text"), obj.value(QStringLiteral("text")).toString());
            row.insert(QStringLiteral("delivered"), isRead);
            row.insert(QStringLiteral("status"), isRead ? tr("Read by Coordinator") : tr("Queued"));
            items.append(row);
        }
    }

    // Keep latest 12 entries (newest first for the compact delivery strip).
    QVariantList recent;
    const int start = qMax(0, items.size() - 12);
    for (int i = items.size() - 1; i >= start; --i)
        recent.append(items.at(i));

    if (recent != m_sentMessages) {
        m_sentMessages = recent;
        emit inboxChanged();
    }
}

bool DeckBackend::sendToCoordinator(const QString &text, const QString &kind) {
    QString cleaned = text.trimmed();
    if (cleaned.isEmpty())
        return false;
    // Cap length safely below the bridge hook's 8 KiB per-line read cap even after JSON escaping.
    constexpr int kMaxInboxMessageChars = 1800;
    if (cleaned.size() > kMaxInboxMessageChars)
        cleaned = cleaned.left(kMaxInboxMessageChars).trimmed();

    QDir().mkpath(QFileInfo(m_inboxPath).absolutePath());
    QFile inbox(m_inboxPath);
    if (!inbox.open(QIODevice::WriteOnly | QIODevice::Append)) {
        setStatusBanner(tr("Could not append to PI_INBOX.jsonl: %1").arg(inbox.errorString()));
        return false;
    }

    QJsonObject entry;
    entry.insert(QStringLiteral("id"), QUuid::createUuid().toString(QUuid::WithoutBraces).left(12));
    entry.insert(QStringLiteral("ts_utc"), QDateTime::currentDateTimeUtc().toString(Qt::ISODate));
    entry.insert(QStringLiteral("kind"), kind.trimmed().isEmpty() ? QStringLiteral("message") : kind.trimmed());
    entry.insert(QStringLiteral("text"), cleaned);

    const QByteArray line = QJsonDocument(entry).toJson(QJsonDocument::Compact) + '\n';
    if (inbox.write(line) != line.size()) {
        setStatusBanner(tr("Short write appending to PI_INBOX.jsonl"));
        return false;
    }
    inbox.flush();
    inbox.close();

    reloadInboxAndAck();
    return true;
}

void DeckBackend::loadPinnedPrefs() {
    QFile f(m_prefsPath);
    if (!f.open(QIODevice::ReadOnly))
        return;
    const QJsonObject obj = QJsonDocument::fromJson(f.readAll()).object();
    QStringList pins;
    for (const QJsonValue &v : obj.value(QStringLiteral("pinned_ids")).toArray())
        pins.append(v.toString());
    m_pinnedIds = pins;
}

void DeckBackend::savePinnedPrefs() {
    QJsonArray arr;
    for (const QString &id : m_pinnedIds)
        arr.append(id);
    QJsonObject obj;
    obj.insert(QStringLiteral("pinned_ids"), arr);
    atomicWriteFile(m_prefsPath, QJsonDocument(obj).toJson(QJsonDocument::Indented));
}

void DeckBackend::pinQueueCard(const QString &queueId) {
    const QString id = queueId.trimmed();
    if (id.isEmpty())
        return;
    if (m_pinnedIds.contains(id)) {
        m_pinnedIds.removeAll(id);
    } else {
        m_pinnedIds.prepend(id);
        sendToCoordinator(QStringLiteral("PI pinned %1 to top priority on Aletheia Deck.").arg(id),
                          QStringLiteral("card_action"));
    }
    savePinnedPrefs();
    reloadQueue();
}

bool DeckBackend::flagQueueCard(const QString &queueId, const QString &note) {
    const QString id = queueId.trimmed();
    if (id.isEmpty())
        return false;
    const QString msg = note.trimmed().isEmpty()
                            ? QStringLiteral("PI flagged %1 for review on Aletheia Deck.").arg(id)
                            : QStringLiteral("[%1] %2").arg(id, note.trimmed());
    return sendToCoordinator(msg, QStringLiteral("card_action"));
}

bool DeckBackend::addTrackIdea(const QString &ideaText) {
    const QString cleaned = ideaText.trimmed();
    if (cleaned.isEmpty())
        return false;
    return sendToCoordinator(QStringLiteral("[SOP-12 New Track / Idea] %1").arg(cleaned),
                             QStringLiteral("track_idea"));
}

void DeckBackend::sleepAllScreens() {
    if (m_displayBin.isEmpty() || !QFileInfo::exists(m_displayBin)) {
        setStatusBanner(m_displayBin.isEmpty()
            ? tr("aletheion-display not found in ~/.local/bin or PATH")
            : tr("aletheion-display not found at %1").arg(m_displayBin));
        return;
    }
    if (!QProcess::startDetached(m_displayBin, {QStringLiteral("sleep-all"), QStringLiteral("--no-notify")}))
        setStatusBanner(tr("Failed to start %1 sleep-all").arg(m_displayBin));
}

void DeckBackend::toggleScreenPad() {
    if (m_displayBin.isEmpty() || !QFileInfo::exists(m_displayBin)) {
        setStatusBanner(m_displayBin.isEmpty()
            ? tr("aletheion-display not found in ~/.local/bin or PATH")
            : tr("aletheion-display not found at %1").arg(m_displayBin));
        return;
    }
    if (!QProcess::startDetached(m_displayBin, {QStringLiteral("toggle-pad")}))
        setStatusBanner(tr("Failed to start %1 toggle-pad").arg(m_displayBin));
}
