import AppKit
import ApplicationServices
import AudioToolbox
import AVFoundation
import CoreMedia
import Darwin
import Foundation
import Security
import UniformTypeIdentifiers

private enum KeychainStore {
    static let service = "com.sproutseeds.read-docs"
    static let openAIAccount = "openai-api-key"
    static let orpOpenAIService = "orp.secret.openai"
    static let orpOpenAIAccount = "openai-primary"

    static func password(account: String, keychainService: String = KeychainStore.service) -> String {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]

        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        guard status == errSecSuccess, let data = item as? Data else {
            return ""
        }
        return String(data: data, encoding: .utf8) ?? ""
    }

    static func setPassword(
        _ value: String,
        account: String,
        keychainService: String = KeychainStore.service
    ) {
        let baseQuery: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: keychainService,
            kSecAttrAccount as String: account,
        ]

        if value.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            SecItemDelete(baseQuery as CFDictionary)
            return
        }

        let data = Data(value.utf8)
        let updateStatus = SecItemUpdate(
            baseQuery as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        if updateStatus == errSecSuccess {
            return
        }

        var addQuery = baseQuery
        addQuery[kSecValueData as String] = data
        SecItemAdd(addQuery as CFDictionary, nil)
    }
}

private struct ReaderPreferences {
    private static let defaults = UserDefaults.standard
    private static let legacyDefaults = UserDefaults(suiteName: "com.DocReader.DocReader")
    static let defaultSpeechRate = 180
    static let minSpeechRate = 90
    static let maxSpeechRate = 300
    static let openAIModels = ["gpt-4o-mini-tts", "tts-1", "tts-1-hd"]
    static let openAIVoices = [
        "marin",
        "cedar",
        "coral",
        "alloy",
        "ash",
        "ballad",
        "echo",
        "fable",
        "nova",
        "onyx",
        "sage",
        "shimmer",
        "verse",
    ]
    private static let defaultOpenAIInstructions =
        "Read clearly at a natural pace with a calm, focused delivery for document narration."
    static let speechBackendOptions: [(title: String, value: String)] = [
        ("Remote Kokoro (strict)", "tailscale-4090"),
        ("Remote Kokoro", "tailscale-kokoro"),
        ("Remote Chatterbox (experimental)", "tailscale-chatterbox"),
        ("Mac Kokoro", "local-kokoro"),
        ("System Voice", "macsay"),
        ("OpenAI API", "openai"),
    ]
    private static var speechBackendValues: Set<String> {
        Set(speechBackendOptions.map(\.value))
    }

    static var mode: String {
        let value = defaults.string(forKey: "reader.mode") ?? "full"
        return ["smart", "full"].contains(value) ? value : "full"
    }

    static var speechRate: Int {
        let stored = defaults.object(forKey: "speech.rate") as? Int
        return normalizedSpeechRate(stored ?? defaultSpeechRate)
    }

    static func normalizedSpeechRate(_ value: Int) -> Int {
        max(minSpeechRate, min(maxSpeechRate, value))
    }

    static func rateLabel(_ value: Int) -> String {
        let rate = normalizedSpeechRate(value)
        let speed = Double(rate) / Double(defaultSpeechRate)
        return "\(rate) WPM / \(String(format: "%.2fx", speed))"
    }

    static var rateControlURL: URL {
        URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent(".doc-reader-managed")
            .appendingPathComponent("read-rate-control.json")
    }

    static func writeRateControlFile(rate: Int = speechRate) {
        let readRate = normalizedSpeechRate(rate)
        let speed = max(0.5, min(2.0, Double(readRate) / Double(defaultSpeechRate)))
        let payload: [String: Any] = [
            "read_rate": readRate,
            "readRate": readRate,
            "read_speed": round(speed * 1000) / 1000,
            "readSpeed": round(speed * 1000) / 1000,
            "updated_at": Date().timeIntervalSince1970,
        ]
        do {
            try FileManager.default.createDirectory(
                at: rateControlURL.deletingLastPathComponent(),
                withIntermediateDirectories: true
            )
            let data = try JSONSerialization.data(withJSONObject: payload, options: [.prettyPrinted])
            try data.write(to: rateControlURL, options: [.atomic])
        } catch {
            return
        }
    }

    static var speechBackend: String {
        let value = defaults.string(forKey: "speech.backend") ?? "local-kokoro"
        if value == "elevenlabs" {
            return "openai"
        }
        if value == "pyttsx3" {
            return "macsay"
        }
        return speechBackendValues.contains(value) ? value : "local-kokoro"
    }

    static var openAIModel: String {
        let value = defaults.string(forKey: "openai.model") ?? "gpt-4o-mini-tts"
        return openAIModels.contains(value) ? value : "gpt-4o-mini-tts"
    }

    static var openAIVoice: String {
        let value = defaults.string(forKey: "openai.voice") ?? "marin"
        return openAIVoices.contains(value) ? value : "marin"
    }

    static var openAIInstructions: String {
        let value = defaults.string(forKey: "openai.instructions") ?? defaultOpenAIInstructions
        let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? defaultOpenAIInstructions : cleaned
    }

    static var appOpenAIAPIKey: String {
        KeychainStore.password(account: KeychainStore.openAIAccount)
    }

    static var openAIAPIKey: String {
        let storedKey = appOpenAIAPIKey
        if !storedKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return storedKey
        }

        for key in ["DOC_READER_OPENAI_API_KEY", "OPENAI_API_KEY"] {
            let value = ProcessInfo.processInfo.environment[key] ?? ""
            let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !cleaned.isEmpty {
                return cleaned
            }
        }

        let orpKey = KeychainStore.password(
            account: KeychainStore.orpOpenAIAccount,
            keychainService: KeychainStore.orpOpenAIService
        )
        if !orpKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return orpKey
        }

        return legacyString(forKeys: ["openai.api_key", "openai/api_key", "OPENAI_API_KEY"])
    }

    static var openAIKeyStatus: String {
        if speechBackend != "openai" {
            return "OpenAI key is unused unless OpenAI API is selected."
        }
        if !appOpenAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "OpenAI key stored in Doc Reader Keychain."
        }
        for key in ["DOC_READER_OPENAI_API_KEY", "OPENAI_API_KEY"] {
            if !(ProcessInfo.processInfo.environment[key] ?? "")
                .trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                return "OpenAI key loaded from environment."
            }
        }
        let orpKey = KeychainStore.password(
            account: KeychainStore.orpOpenAIAccount,
            keychainService: KeychainStore.orpOpenAIService
        )
        if !orpKey.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            return "OpenAI key available from ORP Keychain."
        }
        return "OpenAI key not found; OpenAI API playback is unavailable."
    }

    static func save(
        mode: String,
        speechRate: Int,
        backend: String,
        model: String,
        voice: String,
        instructions: String,
        apiKey: String
    ) {
        defaults.set(mode, forKey: "reader.mode")
        let rate = normalizedSpeechRate(speechRate)
        defaults.set(rate, forKey: "speech.rate")
        defaults.set(backend, forKey: "speech.backend")
        defaults.set(openAIModels.contains(model) ? model : "gpt-4o-mini-tts", forKey: "openai.model")
        defaults.set(openAIVoices.contains(voice) ? voice : "marin", forKey: "openai.voice")
        defaults.set(instructions, forKey: "openai.instructions")
        KeychainStore.setPassword(apiKey, account: KeychainStore.openAIAccount)
        writeRateControlFile(rate: rate)
    }

    static func migrateLegacySettingsIfNeeded() {
        if defaults.string(forKey: "speech.backend") == "elevenlabs" {
            defaults.set("openai", forKey: "speech.backend")
        }

        if defaults.string(forKey: "speech.backend") == nil {
            let backend = legacyString(forKeys: ["voice.backend", "voice/backend"])
            if speechBackendValues.contains(backend) || ["pyttsx3", "elevenlabs"].contains(backend) {
                let migrated = ["pyttsx3": "macsay", "elevenlabs": "openai"][backend] ?? backend
                defaults.set(migrated, forKey: "speech.backend")
            }
        }

        if defaults.string(forKey: "openai.model") == nil {
            defaults.set("gpt-4o-mini-tts", forKey: "openai.model")
        }

        if defaults.string(forKey: "openai.voice") == nil {
            defaults.set("marin", forKey: "openai.voice")
        }
    }

    private static func legacyString(forKeys keys: [String]) -> String {
        for key in keys {
            if let value = legacyDefaults?.string(forKey: key)?.trimmingCharacters(in: .whitespacesAndNewlines),
               !value.isEmpty {
                return value
            }
        }

        let plistURL = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/Preferences/com.DocReader.DocReader.plist")
        guard let dictionary = NSDictionary(contentsOf: plistURL) as? [String: Any] else {
            return ""
        }

        for key in keys {
            if let value = dictionary[key] as? String {
                let cleaned = value.trimmingCharacters(in: .whitespacesAndNewlines)
                if !cleaned.isEmpty {
                    return cleaned
                }
            }
        }
        return ""
    }
}

private struct ReaderHistoryItem: Codable, Equatable {
    var id: String
    var kind: String
    var title: String
    var sourcePath: String
    var snippet: String
    var createdAt: TimeInterval
    var updatedAt: TimeInterval
    var lastSeconds: TimeInterval
    var resumeChunkIndex: Int
    var completed: Bool

    var isDocument: Bool {
        kind == "document"
    }

    var sourceURL: URL {
        URL(fileURLWithPath: sourcePath)
    }
}

private final class ReaderHistoryStore {
    private let defaults = UserDefaults.standard
    private let key = "reader.history.cards.v1"
    private let managedRoot: URL

    var onChange: (() -> Void)?

    init(managedRoot: URL) {
        self.managedRoot = managedRoot
    }

    func items() -> [ReaderHistoryItem] {
        guard let data = defaults.data(forKey: key) else {
            return []
        }
        let decoded = (try? JSONDecoder().decode([ReaderHistoryItem].self, from: data)) ?? []
        return decoded.sorted { lhs, rhs in
            lhs.updatedAt > rhs.updatedAt
        }
    }

    func item(id: String) -> ReaderHistoryItem? {
        items().first { $0.id == id }
    }

    func upsertDocument(file: URL) -> ReaderHistoryItem {
        let resolved = file.standardizedFileURL
        let path = resolved.path
        var current = items()
        let now = Date().timeIntervalSince1970

        if let index = current.firstIndex(where: { $0.kind == "document" && $0.sourcePath == path }) {
            current[index].title = resolved.lastPathComponent
            current[index].updatedAt = now
            save(current)
            return current[index]
        }

        let item = ReaderHistoryItem(
            id: UUID().uuidString,
            kind: "document",
            title: resolved.lastPathComponent,
            sourcePath: path,
            snippet: path,
            createdAt: now,
            updatedAt: now,
            lastSeconds: 0,
            resumeChunkIndex: 0,
            completed: false
        )
        current.append(item)
        save(current)
        return item
    }

    func createTextItem(_ text: String, sourceLabel: String) -> ReaderHistoryItem? {
        let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            return nil
        }

        let id = UUID().uuidString
        let textURL = textHistoryURL.appendingPathComponent("\(id).txt")
        do {
            try FileManager.default.createDirectory(at: textHistoryURL, withIntermediateDirectories: true)
            try "\(cleaned)\n".write(to: textURL, atomically: true, encoding: .utf8)
        } catch {
            return nil
        }

        let now = Date().timeIntervalSince1970
        let snippet = Self.snippet(from: cleaned)
        let title = Self.title(sourceLabel: sourceLabel, snippet: snippet)
        let item = ReaderHistoryItem(
            id: id,
            kind: "text",
            title: title,
            sourcePath: textURL.path,
            snippet: snippet,
            createdAt: now,
            updatedAt: now,
            lastSeconds: 0,
            resumeChunkIndex: 0,
            completed: false
        )

        var current = items()
        current.append(item)
        save(current)
        return item
    }

    func updateProgress(
        id: String,
        seconds: TimeInterval,
        chunkIndex: Int,
        completed: Bool? = nil
    ) {
        var current = items()
        guard let index = current.firstIndex(where: { $0.id == id }) else {
            return
        }
        current[index].lastSeconds = max(0, seconds)
        current[index].resumeChunkIndex = max(0, chunkIndex)
        if let completedValue = completed {
            current[index].completed = completedValue
        }
        current[index].updatedAt = Date().timeIntervalSince1970
        save(current)
    }

    private var textHistoryURL: URL {
        managedRoot
            .appendingPathComponent("history-text", isDirectory: true)
    }

    private func save(_ items: [ReaderHistoryItem]) {
        let sorted = items
            .sorted { lhs, rhs in lhs.updatedAt > rhs.updatedAt }
        if let data = try? JSONEncoder().encode(sorted) {
            defaults.set(data, forKey: key)
            defaults.synchronize()
            DispatchQueue.main.async { [weak self] in
                self?.onChange?()
            }
        }
    }

    private static func snippet(from text: String) -> String {
        let collapsed = text
            .replacingOccurrences(of: "\\s+", with: " ", options: .regularExpression)
            .trimmingCharacters(in: .whitespacesAndNewlines)
        guard collapsed.count > 120 else {
            return collapsed
        }
        let end = collapsed.index(collapsed.startIndex, offsetBy: 117)
        return "\(collapsed[..<end])..."
    }

    private static func title(sourceLabel: String, snippet: String) -> String {
        let label = sourceLabel.trimmingCharacters(in: .whitespacesAndNewlines)
        let prefix = label.isEmpty ? "Text" : label
        guard !snippet.isEmpty else {
            return prefix
        }
        let raw = "\(prefix): \(snippet)"
        guard raw.count > 82 else {
            return raw
        }
        let end = raw.index(raw.startIndex, offsetBy: 79)
        return "\(raw[..<end])..."
    }
}

private func configureDocumentPanel(_ panel: NSOpenPanel) {
    panel.allowedContentTypes = ["pdf", "docx", "txt", "md", "markdown"].compactMap {
        UTType(filenameExtension: $0)
    }
    panel.allowsMultipleSelection = false
    panel.canChooseDirectories = false
}

private final class ReaderEngine: NSObject {
    private struct ProcessSnapshotEntry {
        var pid: pid_t
        var ppid: pid_t
    }

    private var process: Process?
    private let historyStore: ReaderHistoryStore
    private let managedRoot = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".doc-reader-managed", isDirectory: true)

    private var activeItemID: String?
    private var pausedItemID: String?
    private var activeChunkIndex: Int?
    private var lastCompletedChunkIndex = -1
    private var resumeChunkIndex = 0
    private var startDate: Date?
    private var startOffsetSeconds: TimeInterval = 0
    private var lastPositionSeconds: TimeInterval = 0

    var onStatus: ((String) -> Void)?
    var onStateChanged: (() -> Void)?

    init(historyStore: ReaderHistoryStore) {
        self.historyStore = historyStore
        super.init()
    }

    var isRunning: Bool {
        process?.isRunning ?? false
    }

    var isPaused: Bool {
        process == nil && pausedItemID != nil
    }

    var activeHistoryItemID: String? {
        activeItemID ?? pausedItemID
    }

    private var venvURL: URL {
        if let value = ProcessInfo.processInfo.environment["DOC_READER_VENV_DIR"], !value.isEmpty {
            return URL(fileURLWithPath: value, isDirectory: true)
        }
        return managedRoot.appendingPathComponent(".venv", isDirectory: true)
    }

    private var pythonURL: URL {
        venvURL.appendingPathComponent("bin/python")
    }

    var serviceInboxURL: URL {
        managedRoot.appendingPathComponent("service-inbox", isDirectory: true)
    }

    func isPlaying(itemID: String) -> Bool {
        activeItemID == itemID && isRunning
    }

    func isPaused(itemID: String) -> Bool {
        pausedItemID == itemID && !isRunning
    }

    func readText(_ text: String, sourceLabel: String = "Text") {
        let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            onStatus?("No text to read.")
            return
        }
        guard let item = historyStore.createTextItem(cleaned, sourceLabel: sourceLabel) else {
            onStatus?("Could not save text for reading.")
            return
        }
        play(item)
    }

    func readFile(_ file: URL) {
        let item = historyStore.upsertDocument(file: file)
        play(item)
    }

    func play(_ item: ReaderHistoryItem) {
        if isRunning {
            pauseActiveForSwitch()
        }

        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            onStatus?("Reader environment missing. Run read-docs install.")
            return
        }

        var currentItem = historyStore.item(id: item.id) ?? item
        guard FileManager.default.fileExists(atPath: currentItem.sourcePath) else {
            onStatus?("History item file not found: \(currentItem.title)")
            return
        }

        if currentItem.completed {
            historyStore.updateProgress(
                id: currentItem.id,
                seconds: 0,
                chunkIndex: 0,
                completed: false
            )
            currentItem = historyStore.item(id: currentItem.id) ?? currentItem
        }

        let backend = effectiveBackend()
        let resumeFromChunk = max(0, currentItem.resumeChunkIndex)
        let savedSeconds = max(0, currentItem.lastSeconds)
        let startSecondsArgument = resumeFromChunk > 0 ? 0 : max(0, savedSeconds - 20)
        let displayStartSeconds = resumeFromChunk > 0 ? savedSeconds : startSecondsArgument
        ReaderPreferences.writeRateControlFile()

        var args = [
            "-m",
            "doc_reader",
            currentItem.sourcePath,
            "--mode",
            ReaderPreferences.mode,
            "--style",
            "balanced",
            "--rate",
            "\(ReaderPreferences.speechRate)",
            "--rate-control-file",
            ReaderPreferences.rateControlURL.path,
            "--speech-backend",
            backend,
            "--start-chunk-index",
            "\(resumeFromChunk)",
            "--start-seconds",
            String(format: "%.2f", startSecondsArgument),
            "--verbose",
        ]

        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = managedRoot.path

        if backend == "openai" {
            args.append(contentsOf: ["--openai-model", ReaderPreferences.openAIModel])
            args.append(contentsOf: ["--openai-voice", ReaderPreferences.openAIVoice])
            args.append(contentsOf: ["--openai-response-format", "wav"])
            let instructions = ReaderPreferences.openAIInstructions
                .trimmingCharacters(in: .whitespacesAndNewlines)
            if !instructions.isEmpty {
                args.append(contentsOf: ["--openai-instructions", instructions])
            }
            let apiKey = ReaderPreferences.openAIAPIKey.trimmingCharacters(in: .whitespacesAndNewlines)
            if !apiKey.isEmpty {
                environment["OPENAI_API_KEY"] = apiKey
            }
        } else {
            environment["DOC_READER_AUTO_ALLOW_OPENAI"] = "0"
            environment.removeValue(forKey: "OPENAI_API_KEY")
            environment.removeValue(forKey: "DOC_READER_OPENAI_API_KEY")
        }

        let task = Process()
        task.executableURL = pythonURL
        task.arguments = args
        task.currentDirectoryURL = managedRoot
        task.environment = environment

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty, let text = String(data: data, encoding: .utf8) else {
                return
            }
            DispatchQueue.main.async {
                self?.handleReaderOutput(text)
            }
        }

        task.terminationHandler = { [weak self] finished in
            pipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async {
                guard self?.process === finished else {
                    return
                }
                let completed = finished.terminationStatus == 0
                self?.saveActiveProgress(completed: completed)
                self?.process = nil
                self?.activeItemID = nil
                self?.pausedItemID = nil
                self?.startDate = nil
                self?.onStatus?(completed ? "Ready." : "Reader stopped.")
                self?.onStateChanged?()
            }
        }

        do {
            try task.run()
            process = task
            activeItemID = currentItem.id
            pausedItemID = nil
            activeChunkIndex = nil
            lastCompletedChunkIndex = resumeFromChunk - 1
            resumeChunkIndex = resumeFromChunk
            startDate = Date()
            startOffsetSeconds = displayStartSeconds
            lastPositionSeconds = displayStartSeconds
            historyStore.updateProgress(
                id: currentItem.id,
                seconds: displayStartSeconds,
                chunkIndex: resumeFromChunk,
                completed: false
            )
            onStatus?("Reading \(currentItem.title)")
            onStateChanged?()
        } catch {
            activeItemID = nil
            onStatus?("Could not start reader: \(error.localizedDescription)")
            onStateChanged?()
        }
    }

    func togglePause() {
        if isRunning {
            pauseActive()
            return
        }
        if let id = pausedItemID, let item = historyStore.item(id: id) {
            play(item)
            return
        }
        onStatus?("Nothing is currently playing.")
    }

    func stop(waitForExit: Bool = true) {
        if let task = process {
            saveActiveProgress()
            terminateProcess(task, waitForExit: waitForExit)
            process = nil
        }
        activeItemID = nil
        pausedItemID = nil
        startDate = nil
        onStatus?("Stopped.")
        onStateChanged?()
    }

    private func pauseActive() {
        guard let task = process, let itemID = activeItemID else {
            return
        }
        saveActiveProgress()
        terminateProcess(task, waitForExit: true)
        process = nil
        activeItemID = nil
        pausedItemID = itemID
        startDate = nil
        let title = historyStore.item(id: itemID)?.title ?? "reading"
        onStatus?("Paused \(title)")
        onStateChanged?()
    }

    private func pauseActiveForSwitch() {
        guard let task = process, let itemID = activeItemID else {
            return
        }
        saveActiveProgress()
        terminateProcess(task, waitForExit: true)
        process = nil
        activeItemID = nil
        pausedItemID = itemID
        startDate = nil
        onStateChanged?()
    }

    private func terminateProcess(_ task: Process, waitForExit: Bool) {
        let pid = task.processIdentifier
        terminateProcessTree(rootPID: pid, signal: SIGTERM)

        guard waitForExit else {
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.7) { [weak task] in
                guard let task = task, task.isRunning else {
                    return
                }
                self.terminateProcessTree(rootPID: pid, signal: SIGKILL)
            }
            return
        }

        waitForProcessExit(task, timeout: 0.9)
        if task.isRunning {
            terminateProcessTree(rootPID: pid, signal: SIGKILL)
            waitForProcessExit(task, timeout: 0.5)
        }
    }

    private func terminateProcessTree(rootPID: pid_t, signal: Int32) {
        guard rootPID > 0 else {
            return
        }
        let snapshot = processSnapshot()
        let descendants = descendantPIDs(of: rootPID, snapshot: snapshot)
        for pid in descendants.reversed() {
            Darwin.kill(pid, signal)
        }
        Darwin.kill(rootPID, signal)
    }

    private func waitForProcessExit(_ task: Process, timeout: TimeInterval) {
        let deadline = Date().addingTimeInterval(timeout)
        while task.isRunning && Date() < deadline {
            Thread.sleep(forTimeInterval: 0.05)
        }
    }

    private func descendantPIDs(
        of rootPID: pid_t,
        snapshot: [ProcessSnapshotEntry]
    ) -> [pid_t] {
        var childrenByParent: [pid_t: [pid_t]] = [:]
        for entry in snapshot {
            childrenByParent[entry.ppid, default: []].append(entry.pid)
        }

        var descendants: [pid_t] = []
        var queue = childrenByParent[rootPID] ?? []
        while !queue.isEmpty {
            let pid = queue.removeFirst()
            descendants.append(pid)
            queue.append(contentsOf: childrenByParent[pid] ?? [])
        }
        return descendants
    }

    private func processSnapshot() -> [ProcessSnapshotEntry] {
        let task = Process()
        task.executableURL = URL(fileURLWithPath: "/bin/ps")
        task.arguments = ["-axww", "-o", "pid=,ppid=,command="]

        let pipe = Pipe()
        task.standardOutput = pipe
        task.standardError = Pipe()

        do {
            try task.run()
            task.waitUntilExit()
        } catch {
            return []
        }

        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        guard let output = String(data: data, encoding: .utf8) else {
            return []
        }

        return output.split(whereSeparator: \.isNewline).compactMap { line in
            let parts = line.split(separator: " ", maxSplits: 2, omittingEmptySubsequences: true)
            guard parts.count == 3,
                  let pid = pid_t(parts[0]),
                  let ppid = pid_t(parts[1]) else {
                return nil
            }
            return ProcessSnapshotEntry(pid: pid, ppid: ppid)
        }
    }

    private func currentPositionSeconds() -> TimeInterval {
        if isRunning, let startDate = startDate {
            lastPositionSeconds = startOffsetSeconds + Date().timeIntervalSince(startDate)
        }
        return max(0, lastPositionSeconds)
    }

    @discardableResult
    private func saveActiveProgress(completed: Bool? = nil) -> String? {
        guard let id = activeItemID ?? pausedItemID else {
            return nil
        }
        let chunkIndex = activeChunkIndex ?? max(resumeChunkIndex, lastCompletedChunkIndex + 1)
        historyStore.updateProgress(
            id: id,
            seconds: currentPositionSeconds(),
            chunkIndex: max(0, chunkIndex),
            completed: completed
        )
        return id
    }

    private func handleReaderOutput(_ text: String) {
        let lines = text
            .split(whereSeparator: \.isNewline)
            .map(String.init)
        for line in lines where !line.isEmpty {
            if trackProgress(line) {
                continue
            }
            onStatus?(line)
        }
    }

    private func trackProgress(_ line: String) -> Bool {
        if let index = chunkIndex(from: line, prefix: "[doc-reader] chunk-start index=") {
            activeChunkIndex = index
            resumeChunkIndex = max(0, index)
            return true
        }

        if let index = chunkIndex(from: line, prefix: "[doc-reader] chunk-done index=") {
            lastCompletedChunkIndex = max(lastCompletedChunkIndex, index)
            if activeChunkIndex == index {
                activeChunkIndex = nil
            }
            resumeChunkIndex = max(resumeChunkIndex, index + 1)
            saveActiveProgress()
            return true
        }

        if line.hasPrefix("[doc-reader] page number=") {
            return true
        }

        return false
    }

    private func chunkIndex(from line: String, prefix: String) -> Int? {
        guard line.hasPrefix(prefix) else {
            return nil
        }
        return Int(line.dropFirst(prefix.count).trimmingCharacters(in: .whitespacesAndNewlines))
    }

    private func effectiveBackend() -> String {
        ReaderPreferences.speechBackend
    }
}

private final class ReaderWindowController: NSWindowController {
    private let engine: ReaderEngine
    private let historyStore: ReaderHistoryStore
    private let textView = NSTextView()
    private let statusLabel = NSTextField(labelWithString: "Ready.")
    private let pauseButton = NSButton(title: "Pause", target: nil, action: nil)
    private let stopButton = NSButton(title: "Stop", target: nil, action: nil)
    private let historyStack = NSStackView()
    private let historyDocumentView = NSView()
    private let updatedFormatter: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .short
        formatter.timeStyle = .short
        return formatter
    }()

    init(engine: ReaderEngine, historyStore: ReaderHistoryStore) {
        self.engine = engine
        self.historyStore = historyStore

        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 660, height: 680),
            styleMask: [.titled, .closable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        window.title = "Doc Reader"
        window.minSize = NSSize(width: 600, height: 520)
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildUI()
        refreshHistoryCards()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func buildUI() {
        guard let contentView = window?.contentView else {
            return
        }

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 12
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false

        let scrollView = NSScrollView()
        scrollView.hasVerticalScroller = true
        scrollView.borderType = .bezelBorder
        scrollView.documentView = textView
        textView.font = NSFont.systemFont(ofSize: 14)
        textView.string = ""
        scrollView.heightAnchor.constraint(equalToConstant: 150).isActive = true

        let buttonRow = NSStackView()
        buttonRow.orientation = .horizontal
        buttonRow.spacing = 8

        let browseButton = NSButton(title: "Choose Document", target: self, action: #selector(chooseDocument))
        let readTextButton = NSButton(title: "Read Text", target: self, action: #selector(readText))
        let clipboardButton = NSButton(title: "Read Clipboard", target: self, action: #selector(readClipboard))
        pauseButton.target = self
        pauseButton.action = #selector(togglePause)
        pauseButton.isEnabled = false
        stopButton.target = self
        stopButton.action = #selector(stopReading)
        stopButton.isEnabled = false

        for button in [browseButton, readTextButton, clipboardButton, stopButton] {
            button.bezelStyle = .rounded
        }
        pauseButton.bezelStyle = .rounded

        for button in [browseButton, readTextButton, clipboardButton, pauseButton, stopButton] {
            buttonRow.addArrangedSubview(button)
        }

        statusLabel.lineBreakMode = .byTruncatingTail
        statusLabel.maximumNumberOfLines = 2

        let historyLabel = NSTextField(labelWithString: "History")
        historyLabel.font = NSFont.boldSystemFont(ofSize: 13)

        historyStack.orientation = .vertical
        historyStack.alignment = .width
        historyStack.spacing = 8
        historyStack.translatesAutoresizingMaskIntoConstraints = false

        historyDocumentView.translatesAutoresizingMaskIntoConstraints = false
        historyDocumentView.addSubview(historyStack)

        let historyScrollView = NSScrollView()
        historyScrollView.hasVerticalScroller = true
        historyScrollView.borderType = .noBorder
        historyScrollView.documentView = historyDocumentView
        historyScrollView.heightAnchor.constraint(equalToConstant: 300).isActive = true

        NSLayoutConstraint.activate([
            historyStack.leadingAnchor.constraint(equalTo: historyDocumentView.leadingAnchor),
            historyStack.trailingAnchor.constraint(equalTo: historyDocumentView.trailingAnchor),
            historyStack.topAnchor.constraint(equalTo: historyDocumentView.topAnchor),
            historyStack.bottomAnchor.constraint(equalTo: historyDocumentView.bottomAnchor),
            historyStack.widthAnchor.constraint(equalTo: historyScrollView.contentView.widthAnchor),
        ])

        stack.addArrangedSubview(NSTextField(labelWithString: "Paste text or choose a document to read."))
        stack.addArrangedSubview(scrollView)
        stack.addArrangedSubview(buttonRow)
        stack.addArrangedSubview(statusLabel)
        stack.addArrangedSubview(historyLabel)
        stack.addArrangedSubview(historyScrollView)

        contentView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor),
            stack.bottomAnchor.constraint(equalTo: contentView.bottomAnchor),
        ])
    }

    func show() {
        showWindow(nil)
        window?.center()
        NSApp.activate(ignoringOtherApps: true)
        refreshHistoryCards()
    }

    func updateStatus(_ status: String) {
        statusLabel.stringValue = status
    }

    func refreshHistoryCards() {
        for view in historyStack.arrangedSubviews {
            historyStack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }

        let items = historyStore.items()
        if items.isEmpty {
            let empty = NSTextField(labelWithString: "No reading history yet.")
            empty.textColor = .secondaryLabelColor
            historyStack.addArrangedSubview(empty)
            updatePlaybackControls()
            return
        }

        for item in items {
            historyStack.addArrangedSubview(makeHistoryCard(for: item))
        }
        updatePlaybackControls()
    }

    private func makeHistoryCard(for item: ReaderHistoryItem) -> NSView {
        let box = NSBox()
        box.titlePosition = .noTitle
        box.boxType = .custom
        box.wantsLayer = true
        box.layer?.cornerRadius = 8
        box.layer?.borderWidth = 1
        box.layer?.borderColor = NSColor.separatorColor.cgColor
        box.layer?.backgroundColor = NSColor.controlBackgroundColor.cgColor
        box.translatesAutoresizingMaskIntoConstraints = false

        let cardStack = NSStackView()
        cardStack.orientation = .vertical
        cardStack.spacing = 6
        cardStack.translatesAutoresizingMaskIntoConstraints = false

        let topRow = NSStackView()
        topRow.orientation = .horizontal
        topRow.spacing = 8
        topRow.alignment = .centerY

        let titleStack = NSStackView()
        titleStack.orientation = .vertical
        titleStack.spacing = 3

        let titleLabel = NSTextField(labelWithString: item.title)
        titleLabel.font = NSFont.boldSystemFont(ofSize: 13)
        titleLabel.lineBreakMode = .byTruncatingTail
        titleLabel.maximumNumberOfLines = 1

        let metaLabel = NSTextField(labelWithString: historyMetaText(for: item))
        metaLabel.font = NSFont.systemFont(ofSize: 11)
        metaLabel.textColor = .secondaryLabelColor
        metaLabel.lineBreakMode = .byTruncatingTail
        metaLabel.maximumNumberOfLines = 1

        titleStack.addArrangedSubview(titleLabel)
        titleStack.addArrangedSubview(metaLabel)

        let playButton = NSButton(title: historyButtonTitle(for: item), target: self, action: #selector(playHistoryItem))
        playButton.identifier = NSUserInterfaceItemIdentifier(item.id)
        playButton.bezelStyle = .rounded

        topRow.addArrangedSubview(titleStack)
        topRow.addArrangedSubview(playButton)

        let snippetLabel = NSTextField(labelWithString: item.snippet)
        snippetLabel.font = NSFont.systemFont(ofSize: 12)
        snippetLabel.textColor = .secondaryLabelColor
        snippetLabel.lineBreakMode = .byTruncatingTail
        snippetLabel.maximumNumberOfLines = 2

        cardStack.addArrangedSubview(topRow)
        cardStack.addArrangedSubview(snippetLabel)

        box.addSubview(cardStack)
        NSLayoutConstraint.activate([
            cardStack.leadingAnchor.constraint(equalTo: box.leadingAnchor, constant: 12),
            cardStack.trailingAnchor.constraint(equalTo: box.trailingAnchor, constant: -12),
            cardStack.topAnchor.constraint(equalTo: box.topAnchor, constant: 10),
            cardStack.bottomAnchor.constraint(equalTo: box.bottomAnchor, constant: -10),
        ])
        return box
    }

    private func historyMetaText(for item: ReaderHistoryItem) -> String {
        let kind = item.isDocument ? "Document" : "Text"
        let progress = item.completed ? "Complete" : formatTime(item.lastSeconds)
        let updated = updatedFormatter.string(from: Date(timeIntervalSince1970: item.updatedAt))
        return "\(kind) • \(progress) • \(updated)"
    }

    private func historyButtonTitle(for item: ReaderHistoryItem) -> String {
        if engine.isPlaying(itemID: item.id) {
            return "Pause"
        }
        if engine.isPaused(itemID: item.id) {
            return "Resume"
        }
        return "Play"
    }

    private func formatTime(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        let hours = total / 3600
        let minutes = (total % 3600) / 60
        let remaining = total % 60
        if hours > 0 {
            return String(format: "%dh %02dm", hours, minutes)
        }
        return String(format: "%dm %02ds", minutes, remaining)
    }

    private func updatePlaybackControls() {
        pauseButton.isEnabled = engine.isRunning || engine.isPaused
        pauseButton.title = engine.isPaused ? "Resume" : "Pause"
        stopButton.isEnabled = engine.isRunning || engine.isPaused
    }

    @objc private func chooseDocument() {
        let panel = NSOpenPanel()
        configureDocumentPanel(panel)
        panel.begin { [weak self] response in
            guard response == .OK, let url = panel.url else {
                return
            }
            self?.engine.readFile(url)
        }
    }

    @objc private func readText() {
        engine.readText(textView.string, sourceLabel: "Pasted Text")
    }

    @objc private func readClipboard() {
        let text = NSPasteboard.general.string(forType: .string) ?? ""
        engine.readText(text, sourceLabel: "Clipboard")
    }

    @objc private func togglePause() {
        engine.togglePause()
    }

    @objc private func playHistoryItem(_ sender: NSButton) {
        guard let id = sender.identifier?.rawValue,
              let item = historyStore.item(id: id) else {
            statusLabel.stringValue = "History item not found."
            return
        }
        if engine.isPlaying(itemID: id) || engine.isPaused(itemID: id) {
            engine.togglePause()
            return
        }
        engine.play(item)
    }

    @objc private func stopReading() {
        engine.stop()
    }
}

private final class PreferencesWindowController: NSWindowController {
    private let modePopup = NSPopUpButton()
    private let rateSlider = NSSlider()
    private let rateValueLabel = NSTextField(labelWithString: "")
    private let backendPopup = NSPopUpButton()
    private let modelPopup = NSPopUpButton()
    private let apiKeyField = NSSecureTextField()
    private let voicePopup = NSPopUpButton()
    private let instructionsField = NSTextField()
    private let statusLabel = NSTextField(labelWithString: "")

    init() {
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 370),
            styleMask: [.titled, .closable],
            backing: .buffered,
            defer: false
        )
        window.title = "Doc Reader Settings"
        window.isReleasedWhenClosed = false
        super.init(window: window)
        buildUI()
        loadStoredValues()
    }

    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    private func buildUI() {
        guard let contentView = window?.contentView else {
            return
        }

        let stack = NSStackView()
        stack.orientation = .vertical
        stack.spacing = 10
        stack.edgeInsets = NSEdgeInsets(top: 16, left: 16, bottom: 16, right: 16)
        stack.translatesAutoresizingMaskIntoConstraints = false

        modePopup.addItems(withTitles: ["Full Reading", "Smart Summary"])
        rateSlider.minValue = Double(ReaderPreferences.minSpeechRate)
        rateSlider.maxValue = Double(ReaderPreferences.maxSpeechRate)
        rateSlider.target = self
        rateSlider.action = #selector(rateSliderChanged)
        backendPopup.addItems(withTitles: ReaderPreferences.speechBackendOptions.map(\.title))
        modelPopup.addItems(withTitles: ReaderPreferences.openAIModels)
        voicePopup.addItems(withTitles: ReaderPreferences.openAIVoices)
        apiKeyField.placeholderString = "OpenAI API key (optional; ORP Keychain is used when blank)"
        instructionsField.placeholderString = "Voice instructions"

        let saveButton = NSButton(title: "Save", target: self, action: #selector(savePreferences))

        stack.addArrangedSubview(row(label: "Mode", control: modePopup))
        stack.addArrangedSubview(rateRow())
        stack.addArrangedSubview(row(label: "Speech", control: backendPopup))
        stack.addArrangedSubview(row(label: "Model", control: modelPopup))
        stack.addArrangedSubview(row(label: "API Key", control: apiKeyField))
        stack.addArrangedSubview(row(label: "Voice", control: voicePopup))
        stack.addArrangedSubview(row(label: "Instructions", control: instructionsField))

        let buttons = NSStackView()
        buttons.orientation = .horizontal
        buttons.spacing = 8
        buttons.addArrangedSubview(saveButton)
        stack.addArrangedSubview(buttons)
        stack.addArrangedSubview(statusLabel)

        contentView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor),
            stack.topAnchor.constraint(equalTo: contentView.topAnchor),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: contentView.bottomAnchor),
        ])
    }

    private func row(label: String, control: NSView) -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 12
        let text = NSTextField(labelWithString: label)
        text.widthAnchor.constraint(equalToConstant: 80).isActive = true
        control.widthAnchor.constraint(greaterThanOrEqualToConstant: 280).isActive = true
        stack.addArrangedSubview(text)
        stack.addArrangedSubview(control)
        return stack
    }

    private func rateRow() -> NSStackView {
        let stack = NSStackView()
        stack.orientation = .horizontal
        stack.spacing = 12
        let text = NSTextField(labelWithString: "Speed")
        text.widthAnchor.constraint(equalToConstant: 80).isActive = true
        rateSlider.widthAnchor.constraint(greaterThanOrEqualToConstant: 220).isActive = true
        rateValueLabel.widthAnchor.constraint(equalToConstant: 96).isActive = true
        stack.addArrangedSubview(text)
        stack.addArrangedSubview(rateSlider)
        stack.addArrangedSubview(rateValueLabel)
        return stack
    }

    private func loadStoredValues() {
        modePopup.selectItem(at: ReaderPreferences.mode == "smart" ? 1 : 0)
        rateSlider.doubleValue = Double(ReaderPreferences.speechRate)
        updateRateValueLabel()
        let backendIndex = ReaderPreferences.speechBackendOptions.firstIndex {
            $0.value == ReaderPreferences.speechBackend
        } ?? 0
        backendPopup.selectItem(at: backendIndex)
        modelPopup.selectItem(withTitle: ReaderPreferences.openAIModel)
        voicePopup.selectItem(withTitle: ReaderPreferences.openAIVoice)
        apiKeyField.stringValue = ReaderPreferences.appOpenAIAPIKey
        instructionsField.stringValue = ReaderPreferences.openAIInstructions
        statusLabel.stringValue = ReaderPreferences.openAIKeyStatus
    }

    @objc private func rateSliderChanged() {
        let rate = ReaderPreferences.normalizedSpeechRate(Int(rateSlider.doubleValue.rounded()))
        rateSlider.doubleValue = Double(rate)
        ReaderPreferences.writeRateControlFile(rate: rate)
        updateRateValueLabel()
    }

    private func updateRateValueLabel() {
        let rate = ReaderPreferences.normalizedSpeechRate(Int(rateSlider.doubleValue.rounded()))
        rateValueLabel.stringValue = ReaderPreferences.rateLabel(rate)
    }

    func show() {
        loadStoredValues()
        showWindow(nil)
        window?.center()
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func savePreferences() {
        let mode = modePopup.indexOfSelectedItem == 1 ? "smart" : "full"
        let backendIndex = max(0, backendPopup.indexOfSelectedItem)
        let backend = ReaderPreferences.speechBackendOptions[
            min(backendIndex, ReaderPreferences.speechBackendOptions.count - 1)
        ].value
        let model = modelPopup.selectedItem?.title ?? "gpt-4o-mini-tts"
        let voice = voicePopup.selectedItem?.title ?? "marin"
        ReaderPreferences.save(
            mode: mode,
            speechRate: Int(rateSlider.doubleValue.rounded()),
            backend: backend,
            model: model,
            voice: voice,
            instructions: instructionsField.stringValue,
            apiKey: apiKeyField.stringValue
        )
        statusLabel.stringValue = "Saved. \(ReaderPreferences.openAIKeyStatus)"
    }
}

private struct PasteboardSnapshot {
    struct Entry {
        let type: NSPasteboard.PasteboardType
        let data: Data
    }

    let items: [[Entry]]

    static func capture(from pasteboard: NSPasteboard) -> PasteboardSnapshot {
        let items = (pasteboard.pasteboardItems ?? []).map { item in
            item.types.compactMap { type -> Entry? in
                guard let data = item.data(forType: type) else {
                    return nil
                }
                return Entry(type: type, data: data)
            }
        }
        return PasteboardSnapshot(items: items)
    }

    func restore(to pasteboard: NSPasteboard) {
        pasteboard.clearContents()
        let restoredItems = items.map { entries -> NSPasteboardItem in
            let item = NSPasteboardItem()
            for entry in entries {
                item.setData(entry.data, forType: entry.type)
            }
            return item
        }
        if !restoredItems.isEmpty {
            pasteboard.writeObjects(restoredItems)
        }
    }
}

private final class DictationAudioRecorder: NSObject {
    let url: URL
    var onAudioLevel: ((Double) -> Void)?

    private let engine = AVAudioEngine()
    private let inputNode: AVAudioInputNode
    private var inputFormat: AVAudioFormat?
    private var finishing = false
    private var lastLevelUpdateAt = Date.distantPast
    private var audioFile: AVAudioFile?
    private var bufferCount = 0
    private(set) var isRecording = false

    init(device: AVCaptureDevice, url: URL) throws {
        self.url = url
        inputNode = engine.inputNode
        super.init()

        if let audioDeviceID = Self.audioDeviceID(forUID: device.uniqueID),
           let audioUnit = inputNode.audioUnit {
            var currentDevice = audioDeviceID
            let status = AudioUnitSetProperty(
                audioUnit,
                kAudioOutputUnitProperty_CurrentDevice,
                kAudioUnitScope_Global,
                0,
                &currentDevice,
                UInt32(MemoryLayout<AudioDeviceID>.size)
            )
            guard status == noErr else {
                throw Self.error("The selected microphone could not be assigned to the audio engine.")
            }
        }

        let resolvedInputFormat = inputNode.inputFormat(forBus: 0)
        guard resolvedInputFormat.channelCount > 0, resolvedInputFormat.sampleRate > 0 else {
            throw Self.error("The selected microphone has no readable audio format.")
        }
        inputFormat = resolvedInputFormat
    }

    func start() -> Bool {
        guard let inputFormat else {
            return false
        }
        do {
            audioFile = try AVAudioFile(forWriting: url, settings: inputFormat.settings)
        } catch {
            return false
        }

        inputNode.installTap(onBus: 0, bufferSize: 1024, format: inputFormat) { [weak self] buffer, _ in
            guard let self, self.isRecording, !self.finishing else {
                return
            }
            do {
                try self.audioFile?.write(from: buffer)
                self.bufferCount += 1
                self.emitAudioLevel(from: buffer)
            } catch {
                self.finishing = true
                self.engine.stop()
            }
        }

        do {
            try engine.start()
            isRecording = true
            return true
        } catch {
            inputNode.removeTap(onBus: 0)
            audioFile = nil
            return false
        }
    }

    func stop(completion: @escaping (URL, Error?) -> Void) {
        guard !finishing else {
            return
        }
        finishing = true
        inputNode.removeTap(onBus: 0)
        engine.stop()
        audioFile = nil
        isRecording = false

        let fileSize = (
            try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? NSNumber
        )?.int64Value ?? 0
        if bufferCount <= 0 || fileSize <= 0 {
            completion(url, Self.error("No microphone samples were captured."))
        } else {
            completion(url, nil)
        }
    }

    private func emitAudioLevel(from buffer: AVAudioPCMBuffer) {
        let now = Date()
        guard now.timeIntervalSince(lastLevelUpdateAt) >= 0.08 else {
            return
        }
        lastLevelUpdateAt = now
        let level = Self.audioLevel(from: buffer)
        DispatchQueue.main.async { [weak self] in
            self?.onAudioLevel?(level)
        }
    }

    private static func audioLevel(from buffer: AVAudioPCMBuffer) -> Double {
        let frameCount = Int(buffer.frameLength)
        guard frameCount > 0 else {
            return 0
        }
        var sumSquares = 0.0
        var sampleCount = 0

        if let channels = buffer.floatChannelData {
            for channel in 0..<Int(buffer.format.channelCount) {
                let values = channels[channel]
                for index in 0..<frameCount {
                    let value = Double(values[index])
                    sumSquares += value * value
                }
                sampleCount += frameCount
            }
        } else if let channels = buffer.int16ChannelData {
            for channel in 0..<Int(buffer.format.channelCount) {
                let values = channels[channel]
                for index in 0..<frameCount {
                    let value = Double(values[index]) / Double(Int16.max)
                    sumSquares += value * value
                }
                sampleCount += frameCount
            }
        }

        guard sampleCount > 0 else {
            return 0
        }
        return min(1.0, max(0.0, sqrt(sumSquares / Double(sampleCount)) * 8.0))
    }

    private static func audioDeviceID(forUID uid: String) -> AudioDeviceID? {
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDevices,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size
        ) == noErr else {
            return nil
        }

        let count = Int(size) / MemoryLayout<AudioDeviceID>.size
        var deviceIDs = Array(repeating: AudioDeviceID(0), count: count)
        guard AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject),
            &address,
            0,
            nil,
            &size,
            &deviceIDs
        ) == noErr else {
            return nil
        }

        for deviceID in deviceIDs {
            var uidAddress = AudioObjectPropertyAddress(
                mSelector: kAudioDevicePropertyDeviceUID,
                mScope: kAudioObjectPropertyScopeGlobal,
                mElement: kAudioObjectPropertyElementMain
            )
            var uidSize = UInt32(MemoryLayout<CFString?>.size)
            var deviceUID: CFString?
            let status = withUnsafeMutablePointer(to: &deviceUID) { pointer in
                AudioObjectGetPropertyData(
                    deviceID,
                    &uidAddress,
                    0,
                    nil,
                    &uidSize,
                    pointer
                )
            }
            if status == noErr, let deviceUID, String(deviceUID) == uid {
                return deviceID
            }
        }
        return nil
    }

    private static func error(_ message: String) -> NSError {
        NSError(
            domain: "com.sproutseeds.read-docs.dictation",
            code: 1,
            userInfo: [NSLocalizedDescriptionKey: message]
        )
    }
}

private final class RecordingOverlayPanel: NSPanel {
    override var canBecomeKey: Bool {
        true
    }

    override var canBecomeMain: Bool {
        false
    }
}

private final class AppDelegate: NSObject, NSApplicationDelegate {
    private let managedRoot = URL(fileURLWithPath: NSHomeDirectory())
        .appendingPathComponent(".doc-reader-managed", isDirectory: true)
    private let webBaseURL = URL(string: "http://127.0.0.1:8766")!
    private let webLaunchAgentLabel = "com.docreader.web"
    private let ttsLaunchAgentLabel = "com.docreader.tts-local"
    private let umbraTtsHealthURL = URL(string: "http://100.72.151.28:8771/healthz")!
    private let umbraSSHHost = "Umbra"
    private let umbraTtsRootWindowsPath = "C:\\Users\\codyr\\.doc-reader-tts"
    private var statusItem: NSStatusItem?
    private let statusMenuItem = NSMenuItem(title: "Ready.", action: nil, keyEquivalent: "")
    private let pauseMenuItem = NSMenuItem(title: "Pause Web Reading", action: #selector(togglePause), keyEquivalent: "")
    private let stopMenuItem = NSMenuItem(title: "Stop Web Reading", action: #selector(stopReading), keyEquivalent: "")
    private var statusTimer: Timer?
    private var fallbackWebProcess: Process?
    private var startupOrchestrationInProgress = false
    private var globalFlagsMonitor: Any?
    private var localFlagsMonitor: Any?
    private var globalKeyMonitor: Any?
    private var localKeyMonitor: Any?
    private var optionKeyWasDown = false
    private var dictationGestureActive = false
    private var lastDictationGestureAt = Date.distantPast
    private var dictationEnabled = false
    private var audioRecorder: DictationAudioRecorder?
    private var recordingURL: URL?
    private var recordingStartedAt: Date?
    private var recordingStartPending = false
    private var recordingStartPendingAt: Date?
    private var dictationTranscriptionID: UUID?
    private var dictationTranscriptionStartedAt: Date?
    private var dictationTranscriptionWatchdog: DispatchWorkItem?
    private var activeDictationUploadTask: URLSessionUploadTask?
    private var activeDictationSourceURL: URL?
    private var pendingDictationStart: DispatchWorkItem?
    private var pendingDictationStop: DispatchWorkItem?
    private var pendingReadbackGesture: DispatchWorkItem?
    private var dictationLatchedByRapidRelease = false
    private var readbackKeyWasDown = false
    private var readbackGestureCanceled = false
    private var selectedMicrophoneID = ""
    private var requestedInputMonitoringAccess = false
    private var lastDictationEvent = "native helper started"
    private var dictationTargetApp: NSRunningApplication?
    private var recordingWindow: NSWindow?
    private var recordingLabel: NSTextField?
    private var recordingCancelButton: NSButton?
    private var recordingLevelFill: NSView?
    private var recordingLevelWidthConstraint: NSLayoutConstraint?
    private var dictationAudioLevel = 0.0
    private var dictationPeakAudioLevel = 0.0
    private var lastDictationAudioLevelAt = Date.distantPast
    private var lastDictationAudioLevelPublishAt = Date.distantPast
    private var activeMicrophoneID = ""
    private var lastSavedRecordingPath = ""
    private var lastSavedRecordingBytes: Int64 = 0
    private var lastSavedRecordingSeconds: TimeInterval = 0
    private var lastSavedRecordingContentType = ""
    private var lastSavedRecordingPeakLevel = 0.0
    private var lastSavedRecordingCreatedAt: TimeInterval = 0
    private var webReadingPaused = false
    private var webActiveItemID: String?
    // Hotkeys chosen on the web page (Details > Dictation) arrive through the
    // /api/native/status poll as small specs ("alt_r", "f8", "mouse:x1",
    // "<ctrl>+<shift>+r"). Anything missing or unparseable keeps the built-in keys:
    // hold Option to dictate, Control+Option+Command+R (and the older gestures) to read.
    private var dictationHotkeySpec = ""
    private var readbackShortcutSpec = ""
    private var dictationModifierFlag: NSEvent.ModifierFlags? = .option
    private var dictationModifierKeyCodes: Set<UInt16> = [58, 61]
    private var dictationPlainKeyCode: UInt16?
    private var dictationMouseButton: Int?
    private var dictationKeyDisplayName = "Option"
    private var customReadbackFlags: NSEvent.ModifierFlags?
    private var customReadbackKeyCode: UInt16?
    private var globalKeyUpMonitor: Any?
    private var localKeyUpMonitor: Any?
    private var globalMouseMonitor: Any?
    private var localMouseMonitor: Any?
    private static let modifierKeyCodes: [String: [UInt16]] = [
        "ctrl": [59, 62], "ctrl_l": [59], "ctrl_r": [62],
        "alt": [58, 61], "alt_l": [58], "alt_r": [61], "alt_gr": [61],
        "shift": [56, 60], "shift_l": [56], "shift_r": [60],
    ]
    private static let plainKeyCodes: [String: UInt16] = [
        "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109,
        "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113, "f16": 106, "f17": 64, "f18": 79, "f19": 80, "f20": 90,
        "insert": 114, "home": 115, "end": 119, "page_up": 116, "page_down": 121,
        "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11, "q": 12, "w": 13,
        "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21, "6": 22, "5": 23, "9": 25, "7": 26,
        "8": 28, "0": 29, "o": 31, "u": 32, "i": 34, "p": 35, "l": 37, "j": 38, "k": 40, "n": 45, "m": 46, "space": 49,
    ]
    private let rightCommandKeyCode: UInt16 = 54
    private let readSelectedTextKeyCode: UInt16 = 15
    private let commandLReadSelectedTextKeyCode: UInt16 = 37
    private let dictationStartDelaySeconds: TimeInterval = 0.16
    private let readbackGestureDelaySeconds: TimeInterval = 0.08
    private let minimumToggleStopSeconds: TimeInterval = 0.85
    private let maximumDictationStartPendingSeconds: TimeInterval = 8
    private let maximumDictationRecordingSeconds: TimeInterval = 300
    private let dictationTranscriptionTimeoutSeconds: TimeInterval = 120
    private let staleRecordingLevelSeconds: TimeInterval = 30
    private var selectedTextReadInProgress = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if closeDuplicateNativeInstanceIfNeeded() {
            return
        }
        ReaderPreferences.migrateLegacySettingsIfNeeded()
        buildMenu()
        installDictationHotkeyMonitor()
        installSelectedTextReadbackMonitor()
        installCustomDictationMonitors()
        ensureWebAppRunning()
        ensureStartupOrchestration()
        statusTimer = Timer.scheduledTimer(withTimeInterval: 2.5, repeats: true) { [weak self] _ in
            self?.enforceDictationRecordingSafetyLimits()
            self?.refreshWebState()
            self?.publishNativeDictationStatus()
        }
    }

    private func closeDuplicateNativeInstanceIfNeeded() -> Bool {
        let currentPID = ProcessInfo.processInfo.processIdentifier
        let bundleID = Bundle.main.bundleIdentifier ?? "com.sproutseeds.read-docs"
        let others = NSRunningApplication
            .runningApplications(withBundleIdentifier: bundleID)
            .filter { $0.processIdentifier != currentPID }
        guard !others.isEmpty else {
            return false
        }

        let isLaunchAgentInstance =
            ProcessInfo.processInfo.environment["DOC_READER_DISABLE_WEB_FALLBACK"] == "1"
        if isLaunchAgentInstance {
            for app in others {
                app.terminate()
            }
            return false
        }

        NSWorkspace.shared.open(webBaseURL)
        NSApp.terminate(nil)
        return true
    }

    private func buildMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        statusItem = item
        item.button?.image = NSImage(systemSymbolName: "doc.text.fill", accessibilityDescription: "Doc Reader")
        item.button?.imagePosition = .imageOnly

        let menu = NSMenu()
        menu.addItem(NSMenuItem(title: "Open DocReader Page", action: #selector(openReader), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Read Clipboard in DocReader", action: #selector(readClipboard), keyEquivalent: ""))
        menu.addItem(NSMenuItem(title: "Read Selected Text (Right Command or Command-L)", action: #selector(readSelectedText), keyEquivalent: ""))
        menu.addItem(pauseMenuItem)
        menu.addItem(stopMenuItem)
        menu.addItem(.separator())
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: ""))

        for menuItem in menu.items where menuItem.action != nil {
            menuItem.target = self
        }
        item.menu = menu
        updateMenuControls()
    }

    @objc private func openReader() {
        guard !shouldSuppressBrowserOpenForDictation() else {
            return
        }
        ensureWebAppRunning { [weak self] in
            guard let self else {
                return
            }
            NSWorkspace.shared.open(self.webBaseURL)
        }
    }

    @objc private func readClipboard() {
        let text = NSPasteboard.general.string(forType: .string) ?? ""
        let cleaned = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !cleaned.isEmpty else {
            statusMenuItem.title = "Clipboard is empty."
            return
        }
        postWebJSON(
            path: "/api/text",
            payload: ["label": "Clipboard", "text": cleaned],
            openWhenDone: true
        )
    }

    @objc private func readSelectedText() {
        guard audioRecorder == nil, !recordingStartPending, dictationTranscriptionID == nil else {
            statusMenuItem.title = "Finish dictation before reading selected text."
            return
        }
        guard !selectedTextReadInProgress else {
            return
        }
        guard AXIsProcessTrusted() else {
            promptForAccessibilityAccess()
            statusMenuItem.title = "Allow Accessibility for selected-text reading."
            return
        }

        selectedTextReadInProgress = true
        statusMenuItem.title = "Copying selected text..."
        let sourceName = NSWorkspace.shared.frontmostApplication?.localizedName ?? "Current App"
        let pasteboard = NSPasteboard.general
        let snapshot = PasteboardSnapshot.capture(from: pasteboard)

        pasteboard.clearContents()
        sendCopyShortcut()

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.22) { [weak self] in
            guard let self else {
                return
            }
            let copied = pasteboard.string(forType: .string) ?? ""
            let cleaned = copied.trimmingCharacters(in: .whitespacesAndNewlines)
            snapshot.restore(to: pasteboard)
            self.selectedTextReadInProgress = false

            guard !cleaned.isEmpty else {
                self.statusMenuItem.title = "No selected text found."
                return
            }

            let label = sourceName == "Doc Reader" ? "Selected Text" : "Selection from \(sourceName)"
            self.statusMenuItem.title = "Reading selected text..."
            self.postWebJSON(
                path: "/api/text",
                payload: ["label": label, "text": cleaned],
                openWhenDone: false
            )
        }
    }

    @objc private func togglePause() {
        if webReadingPaused, let itemID = webActiveItemID, !itemID.isEmpty {
            postWebJSON(
                path: "/api/items/\(urlPathComponent(itemID))/play",
                payload: nil,
                openWhenDone: false
            )
            return
        }
        postWebJSON(path: "/api/pause", payload: nil, openWhenDone: false)
    }

    @objc private func stopReading() {
        postWebJSON(path: "/api/stop", payload: nil, openWhenDone: false)
    }

    @objc private func quit() {
        stopDictationRecording(send: false)
        fallbackWebProcess?.terminate()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopDictationRecording(send: false)
        activeDictationUploadTask?.cancel()
        dictationTranscriptionWatchdog?.cancel()
        fallbackWebProcess?.terminate()
    }

    @objc private func cancelDictationRecording() {
        pendingDictationStart?.cancel()
        pendingDictationStart = nil
        pendingDictationStop?.cancel()
        pendingDictationStop = nil
        optionKeyWasDown = false
        dictationGestureActive = false
        dictationLatchedByRapidRelease = false
        lastDictationGestureAt = Date()
        if audioRecorder == nil,
           !recordingStartPending,
           cancelDictationTranscription(reason: "transcription canceled from overlay") {
            return
        }
        hideRecordingOverlay()
        statusMenuItem.title = "Dictation canceled."
        logDictation("recording canceled from overlay")
        stopDictationRecording(send: false)
        publishNativeDictationStatus(activeMicrophoneID: selectedMicrophoneDevice()?.uniqueID ?? "")
    }

    private func installDictationHotkeyMonitor() {
        globalFlagsMonitor = NSEvent.addGlobalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            DispatchQueue.main.async {
                self?.handleModifierFlags(event)
            }
        }
        localFlagsMonitor = NSEvent.addLocalMonitorForEvents(matching: .flagsChanged) { [weak self] event in
            self?.handleModifierFlags(event)
            return event
        }
    }

    private func installSelectedTextReadbackMonitor() {
        globalKeyMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyDown) { [weak self] event in
            DispatchQueue.main.async {
                self?.handleReadbackHotkey(event)
            }
        }
        localKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handleReadbackHotkey(event)
            return event
        }
    }

    private func handleReadbackHotkey(_ event: NSEvent) {
        if let plainCode = dictationPlainKeyCode, event.keyCode == plainCode {
            handleCustomDictationGesture(down: true, isRepeat: event.isARepeat)
            return
        }
        if event.keyCode == 53, audioRecorder != nil || recordingStartPending || dictationTranscriptionID != nil {
            cancelDictationRecording()
            return
        }
        guard !event.isARepeat else {
            return
        }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if let customCode = customReadbackKeyCode, let customFlags = customReadbackFlags {
            let pressed = flags.intersection([.control, .option, .shift, .command])
            guard event.keyCode == customCode, pressed == customFlags else {
                return
            }
            pendingDictationStart?.cancel()
            pendingDictationStart = nil
            logDictation("custom selected-text readback shortcut")
            readSelectedText()
            return
        }
        let usesControlCommand = flags.contains(.control) && flags.contains(.command)
        let usesControlOption = flags.contains(.control) && flags.contains(.option)
        let usesLegacyReadback = event.keyCode == readSelectedTextKeyCode && (usesControlCommand || usesControlOption)
        let usesCommandLReadback = event.keyCode == commandLReadSelectedTextKeyCode
            && flags.contains(.command)
            && flags.intersection([.control, .option, .shift]).isEmpty
        if readbackKeyWasDown && !usesLegacyReadback && !usesCommandLReadback {
            cancelPendingReadbackGesture(reason: "right command readback canceled by key chord")
        }
        guard usesLegacyReadback || usesCommandLReadback else {
            return
        }
        pendingDictationStart?.cancel()
        pendingDictationStart = nil
        pendingReadbackGesture?.cancel()
        pendingReadbackGesture = nil
        readbackKeyWasDown = false
        readbackGestureCanceled = false
        if optionKeyWasDown, audioRecorder == nil, !recordingStartPending {
            optionKeyWasDown = false
            dictationGestureActive = false
        }
        logDictation(usesCommandLReadback ? "command-l selected-text readback" : "selected-text readback hotkey")
        readSelectedText()
    }

    private func handleModifierFlags(_ event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        handleReadbackModifierGesture(event, flags: flags)
        guard let dictationFlag = dictationModifierFlag else {
            return // the dictation key is a plain key or a mouse button; handled elsewhere
        }
        let optionDown = flags.contains(dictationFlag)
        let otherModifiers = NSEvent.ModifierFlags([.command, .control, .option, .shift]).subtracting(dictationFlag)
        let optionOnlyGesture = optionDown && flags.intersection(otherModifiers).isEmpty
        if optionOnlyGesture {
            pendingDictationStop?.cancel()
            pendingDictationStop = nil
            if audioRecorder != nil || recordingStartPending {
                if isRapidToggleStop() {
                    dictationLatchedByRapidRelease = true
                    optionKeyWasDown = false
                    dictationGestureActive = true
                    showRecordingOverlay(text: "Recording... tap \(dictationKeyDisplayName) to stop")
                    statusMenuItem.title = "Recording dictation. Tap \(dictationKeyDisplayName) to stop."
                    logDictation("ignored rapid option stop")
                    publishNativeDictationStatus(activeMicrophoneID: selectedMicrophoneDevice()?.uniqueID ?? "")
                    return
                }
                optionKeyWasDown = false
                dictationGestureActive = false
                dictationLatchedByRapidRelease = false
                lastDictationGestureAt = Date()
                logDictation("option down stopped active recording")
                stopDictationRecording(send: true)
                return
            }
            if optionKeyWasDown {
                return
            }
            optionKeyWasDown = true
            dictationGestureActive = true
            lastDictationGestureAt = Date()
            logDictation("option down pending")
            scheduleDictationStart()
        } else {
            if optionDown {
                if pendingDictationStart != nil {
                    pendingDictationStart?.cancel()
                    pendingDictationStart = nil
                    logDictation("option dictation start canceled by modifier chord")
                }
                if audioRecorder == nil, !recordingStartPending {
                    optionKeyWasDown = false
                    dictationGestureActive = false
                }
                return
            }
            guard optionKeyWasDown else {
                return
            }
            guard dictationModifierKeyCodes.contains(event.keyCode) else {
                logDictation("ignored other modifier change keyCode=\(event.keyCode)")
                return
            }
            scheduleDictationStop()
        }
    }

    private func shouldSuppressBrowserOpenForDictation() -> Bool {
        dictationGestureActive || Date().timeIntervalSince(lastDictationGestureAt) < 2.5
    }

    private func handleReadbackModifierGesture(_ event: NSEvent, flags: NSEvent.ModifierFlags) {
        let commandDown = flags.contains(.command)
        if event.keyCode == rightCommandKeyCode {
            if commandDown {
                readbackKeyWasDown = true
                readbackGestureCanceled = false
                scheduleReadbackGesture()
            } else {
                let shouldReadOnRelease =
                    readbackKeyWasDown &&
                    !readbackGestureCanceled &&
                    pendingReadbackGesture != nil
                pendingReadbackGesture?.cancel()
                pendingReadbackGesture = nil
                readbackKeyWasDown = false
                readbackGestureCanceled = false
                if shouldReadOnRelease {
                    triggerSelectedTextReadback(reason: "right command readback release")
                }
            }
            return
        }

        if readbackKeyWasDown, commandDown {
            cancelPendingReadbackGesture(reason: "right command readback canceled by modifier chord")
        }
    }

    private func scheduleReadbackGesture() {
        pendingReadbackGesture?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self else {
                return
            }
            self.pendingReadbackGesture = nil
            guard self.readbackKeyWasDown, !self.readbackGestureCanceled else {
                return
            }
            guard self.isBareCommandKeyCurrentlyDown() else {
                self.cancelPendingReadbackGesture(reason: "right command readback canceled before delay elapsed")
                return
            }
            self.triggerSelectedTextReadback(reason: "right command readback")
        }
        pendingReadbackGesture = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + readbackGestureDelaySeconds, execute: workItem)
    }

    private func triggerSelectedTextReadback(reason: String) {
        pendingReadbackGesture?.cancel()
        pendingReadbackGesture = nil
        readbackGestureCanceled = true
        logDictation(reason)
        readSelectedText()
    }

    private func cancelPendingReadbackGesture(reason: String) {
        pendingReadbackGesture?.cancel()
        pendingReadbackGesture = nil
        readbackGestureCanceled = true
        logDictation(reason)
    }

    private func scheduleDictationStart() {
        pendingDictationStart?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self else {
                return
            }
            self.pendingDictationStart = nil
            guard self.optionKeyWasDown else {
                self.logDictation("option start canceled before delay elapsed")
                return
            }
            guard self.isBareOptionKeyCurrentlyDown() else {
                self.optionKeyWasDown = false
                self.dictationGestureActive = false
                self.logDictation("option start canceled by chord or release")
                return
            }
            self.logDictation("option down")
            self.startDictationRecordingIfEnabled()
        }
        pendingDictationStart = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + dictationStartDelaySeconds, execute: workItem)
    }

    private func scheduleDictationStop() {
        if pendingDictationStart != nil {
            pendingDictationStart?.cancel()
            pendingDictationStart = nil
            optionKeyWasDown = false
            dictationGestureActive = false
            lastDictationGestureAt = Date()
            logDictation("option up before recording start")
            return
        }

        pendingDictationStop?.cancel()
        let workItem = DispatchWorkItem { [weak self] in
            guard let self else {
                return
            }
            if self.isOptionKeyCurrentlyDown() {
                self.logDictation("ignored spurious option up; modifier still down")
                self.optionKeyWasDown = true
                return
            }
            self.optionKeyWasDown = false
            self.dictationGestureActive = false
            self.lastDictationGestureAt = Date()
            if self.audioRecorder != nil || self.recordingStartPending {
                self.dictationLatchedByRapidRelease = true
                self.dictationGestureActive = true
                self.showRecordingOverlay(text: "Recording... tap \(dictationKeyDisplayName) to stop")
                self.statusMenuItem.title = "Recording dictation. Tap \(dictationKeyDisplayName) to stop."
                self.logDictation("option release ignored; recording latched")
                self.publishNativeDictationStatus(activeMicrophoneID: self.selectedMicrophoneDevice()?.uniqueID ?? "")
                return
            }
            self.logDictation("option up confirmed")
            self.stopDictationRecording(send: true)
        }
        pendingDictationStop = workItem
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.18, execute: workItem)
    }

    private func dictationModifierMask() -> CGEventFlags? {
        guard let flag = dictationModifierFlag else {
            return nil
        }
        if flag == .control {
            return .maskControl
        }
        if flag == .shift {
            return .maskShift
        }
        return .maskAlternate
    }

    private func isOptionKeyCurrentlyDown() -> Bool {
        guard let mask = dictationModifierMask() else {
            return optionKeyWasDown
        }
        let combined = CGEventSource.flagsState(.combinedSessionState)
        let hid = CGEventSource.flagsState(.hidSystemState)
        return combined.contains(mask) || hid.contains(mask)
    }

    private func isBareCommandKeyCurrentlyDown() -> Bool {
        let combined = CGEventSource.flagsState(.combinedSessionState)
        let hid = CGEventSource.flagsState(.hidSystemState)
        let commandDown = combined.contains(.maskCommand) || hid.contains(.maskCommand)
        let chordDown =
            combined.contains(.maskAlternate) || hid.contains(.maskAlternate) ||
            combined.contains(.maskControl) || hid.contains(.maskControl) ||
            combined.contains(.maskShift) || hid.contains(.maskShift)
        return commandDown && !chordDown
    }

    private func isBareOptionKeyCurrentlyDown() -> Bool {
        guard let mask = dictationModifierMask() else {
            return optionKeyWasDown
        }
        let combined = CGEventSource.flagsState(.combinedSessionState)
        let hid = CGEventSource.flagsState(.hidSystemState)
        let keyDown = combined.contains(mask) || hid.contains(mask)
        let others: [CGEventFlags] = [.maskCommand, .maskAlternate, .maskControl, .maskShift].filter { $0 != mask }
        let chordDown = others.contains { combined.contains($0) || hid.contains($0) }
        return keyDown && !chordDown
    }

    // MARK: - Hotkeys chosen on the web page

    private func installCustomDictationMonitors() {
        globalKeyUpMonitor = NSEvent.addGlobalMonitorForEvents(matching: .keyUp) { [weak self] event in
            DispatchQueue.main.async {
                self?.handlePlainDictationKeyUp(event)
            }
        }
        localKeyUpMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyUp) { [weak self] event in
            self?.handlePlainDictationKeyUp(event)
            return event
        }
        globalMouseMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.otherMouseDown, .otherMouseUp]) { [weak self] event in
            DispatchQueue.main.async {
                self?.handleMouseDictationButton(event)
            }
        }
        localMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.otherMouseDown, .otherMouseUp]) { [weak self] event in
            self?.handleMouseDictationButton(event)
            return event
        }
    }

    private func handlePlainDictationKeyUp(_ event: NSEvent) {
        guard let plainCode = dictationPlainKeyCode, event.keyCode == plainCode else {
            return
        }
        handleCustomDictationGesture(down: false, isRepeat: false)
    }

    private func handleMouseDictationButton(_ event: NSEvent) {
        guard let wanted = dictationMouseButton, event.buttonNumber == wanted else {
            return
        }
        handleCustomDictationGesture(down: event.type == .otherMouseDown, isRepeat: false)
    }

    /// Hold-to-talk for a plain key (F8, Insert, ...) or a side mouse button.
    private func handleCustomDictationGesture(down: Bool, isRepeat: Bool) {
        if down {
            guard !isRepeat, !optionKeyWasDown else {
                return
            }
            optionKeyWasDown = true
            dictationGestureActive = true
            lastDictationGestureAt = Date()
            logDictation("\(dictationKeyDisplayName) down")
            startDictationRecordingIfEnabled()
            return
        }
        guard optionKeyWasDown else {
            return
        }
        optionKeyWasDown = false
        dictationGestureActive = false
        dictationLatchedByRapidRelease = false
        lastDictationGestureAt = Date()
        if audioRecorder != nil || recordingStartPending {
            logDictation("\(dictationKeyDisplayName) up")
            stopDictationRecording(send: true)
        }
    }

    private func applyHotkeySettings(dictation: String, readback: String) {
        if dictation != dictationHotkeySpec {
            dictationHotkeySpec = dictation
            applyDictationHotkey(dictation)
        }
        if readback != readbackShortcutSpec {
            readbackShortcutSpec = readback
            applyReadbackShortcut(readback)
        }
    }

    private func applyDictationHotkey(_ spec: String) {
        var flag: NSEvent.ModifierFlags? = .option
        var codes: Set<UInt16> = [58, 61]
        var plainCode: UInt16?
        var mouseButton: Int?
        var name = "Option"
        let key = spec.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let plainKeyNames = ["insert", "home", "end", "page_up", "page_down"]
        if key.isEmpty || key == "alt" {
            // built-in: hold Option
        } else if let modifierCodes = Self.modifierKeyCodes[key] {
            codes = Set(modifierCodes)
            if key.hasPrefix("ctrl") {
                flag = .control
                name = "Control"
            } else if key.hasPrefix("shift") {
                flag = .shift
                name = "Shift"
            } else {
                flag = .option
                name = "Option"
            }
            if key.hasSuffix("_l") {
                name = "Left " + name
            } else if key.hasSuffix("_r") || key == "alt_gr" {
                name = "Right " + name
            }
        } else if key == "mouse:x1" || key == "mouse:x2" {
            flag = nil
            codes = []
            mouseButton = key == "mouse:x1" ? 3 : 4
            name = key == "mouse:x1" ? "Mouse 4" : "Mouse 5"
        } else if key.hasPrefix("f") || plainKeyNames.contains(key), let code = Self.plainKeyCodes[key] {
            flag = nil
            codes = []
            plainCode = code
            name = key.hasPrefix("f") ? key.uppercased() : key.replacingOccurrences(of: "_", with: " ").capitalized
        } else {
            logDictation("unsupported dictation key '\(key)'; keeping Option")
        }
        if audioRecorder != nil || recordingStartPending {
            stopDictationRecording(send: true)
        }
        optionKeyWasDown = false
        dictationGestureActive = false
        dictationModifierFlag = flag
        dictationModifierKeyCodes = codes
        dictationPlainKeyCode = plainCode
        dictationMouseButton = mouseButton
        dictationKeyDisplayName = name
        logDictation("dictation key now \(name)")
    }

    private func applyReadbackShortcut(_ spec: String) {
        let chord = spec.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !chord.isEmpty, chord != "<ctrl>+<alt>+<cmd>+r" else {
            customReadbackFlags = nil
            customReadbackKeyCode = nil
            logDictation("read-selection shortcut: built-in")
            return
        }
        var flags: NSEvent.ModifierFlags = []
        var keyCode: UInt16?
        for part in chord.split(separator: "+").map(String.init) {
            switch part {
            case "<ctrl>":
                flags.insert(.control)
            case "<alt>":
                flags.insert(.option)
            case "<shift>":
                flags.insert(.shift)
            case "<cmd>":
                flags.insert(.command)
            default:
                let token = part.hasPrefix("<") && part.hasSuffix(">") && part.count > 2
                    ? String(part.dropFirst().dropLast())
                    : part
                keyCode = Self.plainKeyCodes[token]
            }
        }
        guard let code = keyCode, !flags.isEmpty else {
            customReadbackFlags = nil
            customReadbackKeyCode = nil
            logDictation("unsupported read-selection shortcut '\(chord)'; keeping built-in")
            return
        }
        customReadbackFlags = flags
        customReadbackKeyCode = code
        logDictation("read-selection shortcut now \(chord)")
    }

    private func isRapidToggleStop() -> Bool {
        let start = recordingStartedAt ?? lastDictationGestureAt
        return Date().timeIntervalSince(start) < minimumToggleStopSeconds
    }

    private func shouldContinueDictationStart() -> Bool {
        optionKeyWasDown
            || dictationGestureActive
            || dictationLatchedByRapidRelease
            || isOptionKeyCurrentlyDown()
    }

    private func startDictationRecordingIfEnabled(allowStateRefresh: Bool = true) {
        guard dictationEnabled else {
            if allowStateRefresh {
                logDictation("option requested; refreshing dictation enabled state")
                refreshWebState { [weak self] in
                    guard let self else {
                        return
                    }
                    if self.dictationEnabled {
                        self.startDictationRecordingIfEnabled(allowStateRefresh: false)
                    } else {
                        self.logDictation("option ignored; dictation disabled")
                    }
                }
            } else {
                logDictation("option ignored; dictation disabled")
            }
            return
        }
        guard audioRecorder == nil, !recordingStartPending else {
            logDictation("option ignored; recorder already active or starting")
            return
        }
        guard dictationTranscriptionID == nil else {
            statusMenuItem.title = "Finishing previous dictation transcription."
            logDictation("option ignored; transcription already active")
            return
        }
        if !CGPreflightListenEventAccess() {
            requestInputMonitoringAccessIfNeeded()
            logDictation("option event received; Input Monitoring still reports unavailable")
        }
        setRecordingStartPending(true)
        dictationTargetApp = NSWorkspace.shared.frontmostApplication
        lastDictationEvent = "starting recorder"
        publishNativeDictationStatus()
        requestMicrophoneAndStartRecording()
        if !isWebHealthy() {
            ensureWebAppRunning()
        }
    }

    private func requestMicrophoneAndStartRecording() {
        AVCaptureDevice.requestAccess(for: .audio) { [weak self] granted in
            DispatchQueue.main.async {
                guard let self else {
                    return
                }
                guard granted else {
                    self.setRecordingStartPending(false)
                    self.statusMenuItem.title = "Microphone access is required for dictation."
                    self.lastDictationEvent = "microphone access denied"
                    self.logDictation("microphone access denied")
                    return
                }
                guard self.shouldContinueDictationStart() else {
                    self.setRecordingStartPending(false)
                    self.hideRecordingOverlay()
                    self.statusMenuItem.title = "Dictation canceled."
                    self.lastDictationEvent = "start canceled before recorder opened"
                    self.logDictation("start canceled before recorder opened")
                    return
                }
                self.startDictationRecording()
            }
        }
    }

    private func startDictationRecording() {
        guard shouldContinueDictationStart() else {
            setRecordingStartPending(false)
            hideRecordingOverlay()
            statusMenuItem.title = "Dictation canceled."
            lastDictationEvent = "start canceled before capture session"
            logDictation("start canceled before capture session")
            return
        }
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("docreader-dictation-\(UUID().uuidString).wav")

        do {
            guard let device = selectedMicrophoneDevice() else {
                setRecordingStartPending(false)
                statusMenuItem.title = "No microphone input is available."
                lastDictationEvent = "no microphone input"
                logDictation("no microphone input")
                return
            }
            let recorder = try DictationAudioRecorder(device: device, url: url)
            dictationAudioLevel = 0
            dictationPeakAudioLevel = 0
            lastDictationAudioLevelAt = Date()
            lastDictationAudioLevelPublishAt = Date.distantPast
            activeMicrophoneID = device.uniqueID
            recorder.onAudioLevel = { [weak self] level in
                self?.updateDictationAudioLevel(level)
            }
            guard recorder.start() else {
                setRecordingStartPending(false)
                activeMicrophoneID = ""
                statusMenuItem.title = "Could not start microphone recording."
                lastDictationEvent = "could not start microphone recording"
                logDictation("could not start microphone recording")
                return
            }

            recordingURL = url
            recordingStartedAt = Date()
            audioRecorder = recorder
            setRecordingStartPending(false)
            let overlayText = dictationLatchedByRapidRelease
                ? "Recording... tap \(dictationKeyDisplayName) to stop"
                : "Recording..."
            showRecordingOverlay(text: overlayText)
            statusMenuItem.title = dictationLatchedByRapidRelease
                ? "Recording dictation. Tap \(dictationKeyDisplayName) to stop."
                : "Recording dictation with \(device.localizedName)..."
            lastDictationEvent = "recording with \(device.localizedName)"
            logDictation("recording started microphone=\(device.localizedName)")
            publishNativeDictationStatus()
        } catch {
            setRecordingStartPending(false)
            activeMicrophoneID = ""
            statusMenuItem.title = "Recording failed: \(error.localizedDescription)"
            lastDictationEvent = "recording failed: \(error.localizedDescription)"
            logDictation("recording failed \(error.localizedDescription)")
        }
    }

    private func stopDictationRecording(send: Bool) {
        guard let recorder = audioRecorder else {
            if recordingStartPending {
                setRecordingStartPending(false)
                dictationLatchedByRapidRelease = false
                dictationGestureActive = false
                lastDictationEvent = "recording canceled before start"
                logDictation("recording canceled before start")
                hideRecordingOverlay()
                publishNativeDictationStatus()
            }
            return
        }
        let elapsed = recordingStartedAt.map { Date().timeIntervalSince($0) } ?? 0
        let outputFileURL = recorder.url
        recordingStartedAt = nil
        let shouldSend = send && elapsed >= 0.35
        dictationLatchedByRapidRelease = false
        audioRecorder = nil
        recordingURL = nil
        publishNativeDictationStatus()
        let peakLevel = dictationPeakAudioLevel
        let recordedMicrophoneID = activeMicrophoneID
        activeMicrophoneID = ""

        recorder.stop { [weak self] url, error in
            guard let self else {
                return
            }
            if let error {
                self.hideRecordingOverlay()
                self.dictationTargetApp = nil
                try? FileManager.default.removeItem(at: outputFileURL)
                self.statusMenuItem.title = "Recording failed: \(error.localizedDescription)"
                self.lastDictationEvent = "recording finish failed: \(error.localizedDescription)"
                self.logDictation(String(format: "recording finish failed %@ peak_level=%.2f", error.localizedDescription, peakLevel))
                return
            }

            if shouldSend {
                _ = self.saveDictationRecording(
                    url,
                    elapsed: elapsed,
                    peakLevel: peakLevel,
                    contentType: "audio/wav",
                    microphoneID: recordedMicrophoneID
                )
                self.showRecordingOverlay(text: "Transcribing...")
                self.statusMenuItem.title = "Transcribing..."
                self.lastDictationEvent = String(format: "transcribing %.2fs recording", elapsed)
                self.logDictation(String(format: "recording stopped; transcribing elapsed=%.2fs peak_level=%.2f", elapsed, peakLevel))
                self.sendDictationAudio(url, contentType: "audio/wav", elapsed: elapsed)
            } else {
                self.hideRecordingOverlay()
                self.dictationTargetApp = nil
                try? FileManager.default.removeItem(at: outputFileURL)
                self.lastDictationEvent = String(format: "recording canceled after %.2fs", elapsed)
                self.logDictation(String(format: "recording stopped; canceled elapsed=%.2fs send=%@ peak_level=%.2f", elapsed, send ? "true" : "false", peakLevel))
            }
        }
    }

    private func setRecordingStartPending(_ pending: Bool) {
        recordingStartPending = pending
        recordingStartPendingAt = pending ? Date() : nil
    }

    private func enforceDictationRecordingSafetyLimits() {
        guard audioRecorder != nil || recordingStartPending else {
            return
        }
        if recordingStartPending,
           let pendingAt = recordingStartPendingAt,
           Date().timeIntervalSince(pendingAt) >= maximumDictationStartPendingSeconds {
            optionKeyWasDown = false
            dictationGestureActive = false
            dictationLatchedByRapidRelease = false
            setRecordingStartPending(false)
            lastDictationGestureAt = Date()
            hideRecordingOverlay()
            statusMenuItem.title = "Dictation start timed out. Tap Option again."
            logDictation("recording start timed out")
            publishNativeDictationStatus()
            return
        }
        let elapsed = recordingStartedAt.map { Date().timeIntervalSince($0) } ?? 0
        if elapsed >= maximumDictationRecordingSeconds {
            optionKeyWasDown = false
            dictationGestureActive = false
            dictationLatchedByRapidRelease = false
            lastDictationGestureAt = Date()
            statusMenuItem.title = "Dictation auto-stopped after five minutes."
            logDictation(String(format: "recording auto-stopped after %.2fs", elapsed))
            stopDictationRecording(send: true)
            return
        }
        if audioRecorder != nil,
           Date().timeIntervalSince(lastDictationAudioLevelAt) >= staleRecordingLevelSeconds {
            showRecordingOverlay(text: "Recording... tap Option, Esc, or x to stop")
            statusMenuItem.title = "Recording dictation. Tap Option, Esc, or x to stop."
            publishNativeDictationStatus(activeMicrophoneID: selectedMicrophoneDevice()?.uniqueID ?? "")
        }
    }

    private func selectedMicrophoneDevice() -> AVCaptureDevice? {
        let devices = audioCaptureDevices()
        if !selectedMicrophoneID.isEmpty,
           let selected = devices.first(where: { $0.uniqueID == selectedMicrophoneID }) {
            return selected
        }
        if let preferred = preferredMicrophoneDevice(in: devices) {
            selectedMicrophoneID = preferred.uniqueID
            return preferred
        }
        return AVCaptureDevice.default(for: .audio) ?? devices.first
    }

    private func preferredMicrophoneDevice(in devices: [AVCaptureDevice]) -> AVCaptureDevice? {
        let tokens = ["logi", "logitech"]
        if let nameMatch = devices.first(where: { device in
            let name = device.localizedName.lowercased()
            return tokens.contains(where: { name.contains($0) })
        }) {
            return nameMatch
        }
        return devices.first(where: { device in
            let id = device.uniqueID.lowercased()
            return tokens.contains(where: { id.contains($0) })
        })
    }

    private func audioCaptureDevices() -> [AVCaptureDevice] {
        AVCaptureDevice.DiscoverySession(
            deviceTypes: [.microphone],
            mediaType: .audio,
            position: .unspecified
        ).devices
    }

    private var dictationRecordingsURL: URL {
        managedRoot.appendingPathComponent("dictation-recordings", isDirectory: true)
    }

    private func updateDictationAudioLevel(_ level: Double) {
        let clamped = min(1.0, max(0.0, level))
        dictationAudioLevel = clamped
        dictationPeakAudioLevel = max(dictationPeakAudioLevel, clamped)
        lastDictationAudioLevelAt = Date()
        updateRecordingLevelMeter(clamped)

        guard Date().timeIntervalSince(lastDictationAudioLevelPublishAt) >= 0.35 else {
            return
        }
        lastDictationAudioLevelPublishAt = Date()
        publishNativeDictationStatus()
    }

    private func publishNativeDictationStatus(activeMicrophoneID overrideActiveMicrophoneID: String? = nil) {
        let devices = audioCaptureDevices().map {
            ["id": $0.uniqueID, "name": $0.localizedName]
        }
        let recentLevel = Date().timeIntervalSince(lastDictationAudioLevelAt) < 1.0
        let level = audioRecorder?.isRecording == true && recentLevel ? dictationAudioLevel : 0
        let activeID = overrideActiveMicrophoneID ?? activeMicrophoneID
        var payload: [String: Any] = [
            "devices": devices,
            "microphone_authorization": microphoneAuthorizationLabel(),
            "input_monitoring_trusted": CGPreflightListenEventAccess(),
            "accessibility_trusted": AXIsProcessTrusted(),
            "active_microphone_id": activeID,
            "recording": audioRecorder?.isRecording ?? false,
            "recording_start_pending": recordingStartPending,
            "last_dictation_event": lastDictationEvent,
            "audio_level": level,
            "audio_peak_level": dictationPeakAudioLevel,
        ]
        if !lastSavedRecordingPath.isEmpty {
            payload["last_recording_path"] = lastSavedRecordingPath
            payload["last_recording_bytes"] = lastSavedRecordingBytes
            payload["last_recording_seconds"] = lastSavedRecordingSeconds
            payload["last_recording_content_type"] = lastSavedRecordingContentType
            payload["last_recording_peak_level"] = lastSavedRecordingPeakLevel
            payload["last_recording_created_at"] = lastSavedRecordingCreatedAt
        }
        var request = URLRequest(url: webURL(path: "/api/native/dictation"))
        request.httpMethod = "POST"
        request.timeoutInterval = 0.6
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        URLSession.shared.dataTask(with: request).resume()
    }

    private func microphoneAuthorizationLabel() -> String {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return "authorized"
        case .denied:
            return "denied"
        case .restricted:
            return "restricted"
        case .notDetermined:
            return "not determined"
        @unknown default:
            return "unknown"
        }
    }

    private func requestInputMonitoringAccessIfNeeded() {
        guard !CGPreflightListenEventAccess(), !requestedInputMonitoringAccess else {
            return
        }
        requestedInputMonitoringAccess = true
        CGRequestListenEventAccess()
    }

    private func saveDictationRecording(
        _ sourceURL: URL,
        elapsed: TimeInterval,
        peakLevel: Double,
        contentType: String,
        microphoneID: String
    ) -> URL? {
        do {
            try FileManager.default.createDirectory(
                at: dictationRecordingsURL,
                withIntermediateDirectories: true
            )
            let stamp = Self.recordingTimestamp()
            let id = UUID().uuidString
            let destination = dictationRecordingsURL
                .appendingPathComponent("dictation-\(stamp)-\(id).\(Self.recordingExtension(for: contentType))")
            try FileManager.default.copyItem(at: sourceURL, to: destination)
            let attributes = try FileManager.default.attributesOfItem(atPath: destination.path)
            let bytes = (attributes[.size] as? NSNumber)?.int64Value ?? 0
            let createdAt = Date().timeIntervalSince1970

            lastSavedRecordingPath = destination.path
            lastSavedRecordingBytes = bytes
            lastSavedRecordingSeconds = elapsed
            lastSavedRecordingContentType = contentType
            lastSavedRecordingPeakLevel = peakLevel
            lastSavedRecordingCreatedAt = createdAt

            let metadata: [String: Any] = [
                "path": destination.path,
                "bytes": bytes,
                "duration_seconds": elapsed,
                "content_type": contentType,
                "peak_level": peakLevel,
                "created_at": createdAt,
                "microphone_id": microphoneID,
                "selected_microphone_id": selectedMicrophoneID,
            ]
            let metadataURL = destination.deletingPathExtension().appendingPathExtension("json")
            if let data = try? JSONSerialization.data(
                withJSONObject: metadata,
                options: [.prettyPrinted, .sortedKeys]
            ) {
                try data.write(to: metadataURL, options: .atomic)
            }

            logDictation(
                String(
                    format: "recording saved path=%@ bytes=%lld elapsed=%.2fs peak_level=%.2f",
                    destination.path,
                    bytes,
                    elapsed,
                    peakLevel
                )
            )
            publishNativeDictationStatus()
            return destination
        } catch {
            lastDictationEvent = "recording save failed: \(error.localizedDescription)"
            logDictation("recording save failed \(error.localizedDescription)")
            publishNativeDictationStatus()
            return nil
        }
    }

    private static func recordingTimestamp() -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.timeZone = TimeZone.current
        formatter.dateFormat = "yyyyMMdd-HHmmss"
        return formatter.string(from: Date())
    }

    private static func recordingExtension(for contentType: String) -> String {
        switch contentType.lowercased().split(separator: ";", maxSplits: 1).first.map(String.init) {
        case "audio/wav", "audio/x-wav", "audio/wave":
            return "wav"
        case "audio/mp4", "audio/m4a":
            return "m4a"
        default:
            return "audio"
        }
    }

    private func logDictation(_ message: String) {
        lastDictationEvent = message
        fputs("[doc-reader-dictation] \(Date()) \(message)\n", stderr)
        fflush(stderr)
    }

    private func beginDictationTranscriptionWatchdog(id: UUID, sourceURL: URL) {
        dictationTranscriptionWatchdog?.cancel()
        dictationTranscriptionID = id
        dictationTranscriptionStartedAt = Date()
        activeDictationSourceURL = sourceURL
        recordingCancelButton?.isHidden = false

        let workItem = DispatchWorkItem { [weak self] in
            guard let self, self.dictationTranscriptionID == id else {
                return
            }
            self.activeDictationUploadTask?.cancel()
            self.activeDictationUploadTask = nil
            self.dictationTranscriptionID = nil
            self.dictationTranscriptionStartedAt = nil
            self.dictationTranscriptionWatchdog = nil
            self.activeDictationSourceURL = nil
            self.hideRecordingOverlay()
            self.statusMenuItem.title = "Dictation transcription timed out."
            self.logDictation("transcription timed out")
            self.dictationTargetApp = nil
            try? FileManager.default.removeItem(at: sourceURL)
            self.publishNativeDictationStatus()
        }
        dictationTranscriptionWatchdog = workItem
        DispatchQueue.main.asyncAfter(
            deadline: .now() + dictationTranscriptionTimeoutSeconds + 5,
            execute: workItem
        )
    }

    private func finishDictationTranscription(id: UUID) -> Bool {
        guard dictationTranscriptionID == id else {
            return false
        }
        dictationTranscriptionWatchdog?.cancel()
        dictationTranscriptionWatchdog = nil
        dictationTranscriptionID = nil
        dictationTranscriptionStartedAt = nil
        activeDictationUploadTask = nil
        activeDictationSourceURL = nil
        return true
    }

    private func cancelDictationTranscription(reason: String) -> Bool {
        guard dictationTranscriptionID != nil else {
            return false
        }
        dictationTranscriptionWatchdog?.cancel()
        dictationTranscriptionWatchdog = nil
        activeDictationUploadTask?.cancel()
        activeDictationUploadTask = nil
        dictationTranscriptionID = nil
        dictationTranscriptionStartedAt = nil
        if let sourceURL = activeDictationSourceURL {
            try? FileManager.default.removeItem(at: sourceURL)
        }
        activeDictationSourceURL = nil
        hideRecordingOverlay()
        statusMenuItem.title = "Dictation canceled."
        logDictation(reason)
        dictationTargetApp = nil
        publishNativeDictationStatus()
        return true
    }

    private func sendDictationAudio(_ url: URL, contentType: String = "audio/wav", elapsed: TimeInterval = 0) {
        guard let audioData = try? Data(contentsOf: url), !audioData.isEmpty else {
            hideRecordingOverlay()
            statusMenuItem.title = "No dictation audio captured."
            lastDictationEvent = "no dictation audio captured"
            logDictation("no dictation audio captured")
            try? FileManager.default.removeItem(at: url)
            return
        }

        let transcriptionID = UUID()
        beginDictationTranscriptionWatchdog(id: transcriptionID, sourceURL: url)
        ensureWebAppRunning { [weak self] in
            guard let self else {
                return
            }
            guard self.dictationTranscriptionID == transcriptionID else {
                return
            }
            var request = URLRequest(url: self.webURL(path: "/api/transcribe"))
            request.httpMethod = "POST"
            request.timeoutInterval = self.dictationTranscriptionTimeoutSeconds
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
            request.setValue("en", forHTTPHeaderField: "X-Doc-Reader-Language")
            if elapsed > 0 {
                request.setValue(
                    String(format: "%.6f", elapsed),
                    forHTTPHeaderField: "X-Doc-Reader-Elapsed-Seconds"
                )
            }
            let task = URLSession.shared.uploadTask(with: request, from: audioData) { data, response, error in
                try? FileManager.default.removeItem(at: url)
                let succeeded = (response as? HTTPURLResponse)?.statusCode == 200 && error == nil
                let payload = data.flatMap {
                    try? JSONSerialization.jsonObject(with: $0)
                } as? [String: Any]
                let transcription = (payload?["text"] as? String ?? "")
                    .trimmingCharacters(in: .whitespacesAndNewlines)
                DispatchQueue.main.async {
                    guard self.finishDictationTranscription(id: transcriptionID) else {
                        return
                    }
                    self.hideRecordingOverlay()
                    if succeeded {
                        self.refreshWebState()
                        if transcription.isEmpty {
                            self.statusMenuItem.title = "Dictation produced no text."
                            self.lastDictationEvent = "transcription produced no text"
                            self.logDictation("transcription produced no text")
                            self.dictationTargetApp = nil
                        } else {
                            self.lastDictationEvent = "transcription received"
                            self.logDictation("transcription received chars=\(transcription.count)")
                            self.insertDictationText(transcription)
                        }
                    } else {
                        let message = data.flatMap { String(data: $0, encoding: .utf8) }
                            ?? error?.localizedDescription
                            ?? "Dictation transcription failed."
                        self.statusMenuItem.title = message
                        self.lastDictationEvent = "transcription failed: \(message)"
                        self.logDictation("transcription failed \(message)")
                        self.dictationTargetApp = nil
                    }
                }
            }
            self.activeDictationUploadTask = task
            task.resume()
        }
    }

    private func insertDictationText(_ text: String) {
        if !AXIsProcessTrusted() {
            copyTextToClipboard(text)
            promptForAccessibilityAccess()
            statusMenuItem.title = "Dictation copied. Allow Accessibility to insert automatically."
            dictationTargetApp = nil
            return
        }

        let pasteboard = NSPasteboard.general
        let snapshot = PasteboardSnapshot.capture(from: pasteboard)
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
        let pasteboardChangeCount = pasteboard.changeCount

        let targetApp = dictationTargetApp
        dictationTargetApp = nil
        if let targetApp, !targetApp.isTerminated {
            targetApp.activate(options: [])
        }

        DispatchQueue.main.asyncAfter(deadline: .now() + 0.12) {
            self.sendPasteShortcut()
            self.statusMenuItem.title = "Dictation inserted."
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) {
                if NSPasteboard.general.changeCount == pasteboardChangeCount {
                    snapshot.restore(to: NSPasteboard.general)
                }
            }
        }
    }

    private func copyTextToClipboard(_ text: String) {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(text, forType: .string)
    }

    private func promptForAccessibilityAccess() {
        let key = kAXTrustedCheckOptionPrompt.takeUnretainedValue() as String
        let options = [key: true] as CFDictionary
        AXIsProcessTrustedWithOptions(options)
    }

    private func sendPasteShortcut() {
        let source = CGEventSource(stateID: .combinedSessionState)
        let vKey: CGKeyCode = 0x09
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: false)
        keyDown?.flags = .maskCommand
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cghidEventTap)
        keyUp?.post(tap: .cghidEventTap)
    }

    private func sendCopyShortcut() {
        let source = CGEventSource(stateID: .combinedSessionState)
        let vKey: CGKeyCode = 0x08
        let keyDown = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: true)
        let keyUp = CGEvent(keyboardEventSource: source, virtualKey: vKey, keyDown: false)
        keyDown?.flags = .maskCommand
        keyUp?.flags = .maskCommand
        keyDown?.post(tap: .cghidEventTap)
        keyUp?.post(tap: .cghidEventTap)
    }

    private func showRecordingOverlay(text: String) {
        let rect = recordingOverlayFrame()
        if recordingWindow == nil {
            let window = RecordingOverlayPanel(
                contentRect: rect,
                styleMask: [.borderless, .nonactivatingPanel],
                backing: .buffered,
                defer: false
            )
            window.level = .floating
            window.isOpaque = false
            window.backgroundColor = NSColor.clear
            window.ignoresMouseEvents = false
            window.hidesOnDeactivate = false
            window.collectionBehavior = [.canJoinAllSpaces, .transient, .ignoresCycle]

            let container = NSVisualEffectView(frame: NSRect(x: 0, y: 0, width: rect.width, height: rect.height))
            container.material = .hudWindow
            container.blendingMode = .behindWindow
            container.state = .active
            container.wantsLayer = true
            container.layer?.cornerRadius = 10

            let topRow = NSStackView()
            topRow.orientation = .horizontal
            topRow.spacing = 7
            topRow.alignment = .centerY
            topRow.translatesAutoresizingMaskIntoConstraints = false

            let dot = NSTextField(labelWithString: "●")
            dot.textColor = .systemRed
            dot.font = NSFont.systemFont(ofSize: 11, weight: .semibold)
            dot.translatesAutoresizingMaskIntoConstraints = false

            let label = NSTextField(labelWithString: text)
            label.textColor = .labelColor
            label.font = NSFont.systemFont(ofSize: 12, weight: .semibold)
            label.lineBreakMode = .byTruncatingTail
            label.maximumNumberOfLines = 1
            label.usesSingleLineMode = true
            label.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
            label.translatesAutoresizingMaskIntoConstraints = false

            let cancelButton = NSButton(title: "x", target: self, action: #selector(cancelDictationRecording))
            cancelButton.bezelStyle = .circular
            cancelButton.controlSize = .small
            cancelButton.font = NSFont.systemFont(ofSize: 11, weight: .bold)
            cancelButton.toolTip = "Cancel dictation"
            cancelButton.translatesAutoresizingMaskIntoConstraints = false

            let levelTrack = NSView()
            levelTrack.wantsLayer = true
            levelTrack.layer?.backgroundColor = NSColor.separatorColor.withAlphaComponent(0.45).cgColor
            levelTrack.layer?.cornerRadius = 2.5
            levelTrack.translatesAutoresizingMaskIntoConstraints = false

            let levelFill = NSView()
            levelFill.wantsLayer = true
            levelFill.layer?.backgroundColor = NSColor.systemGreen.cgColor
            levelFill.layer?.cornerRadius = 2.5
            levelFill.translatesAutoresizingMaskIntoConstraints = false

            levelTrack.addSubview(levelFill)
            topRow.addArrangedSubview(dot)
            topRow.addArrangedSubview(label)
            topRow.addArrangedSubview(cancelButton)
            container.addSubview(topRow)
            container.addSubview(levelTrack)

            let fillWidth = levelFill.widthAnchor.constraint(equalToConstant: 2)
            NSLayoutConstraint.activate([
                topRow.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 12),
                topRow.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
                topRow.topAnchor.constraint(equalTo: container.topAnchor, constant: 8),

                levelTrack.leadingAnchor.constraint(equalTo: container.leadingAnchor, constant: 12),
                levelTrack.trailingAnchor.constraint(equalTo: container.trailingAnchor, constant: -12),
                levelTrack.topAnchor.constraint(equalTo: topRow.bottomAnchor, constant: 6),
                levelTrack.heightAnchor.constraint(equalToConstant: 5),

                cancelButton.widthAnchor.constraint(equalToConstant: 22),
                cancelButton.heightAnchor.constraint(equalToConstant: 22),

                levelFill.leadingAnchor.constraint(equalTo: levelTrack.leadingAnchor),
                levelFill.topAnchor.constraint(equalTo: levelTrack.topAnchor),
                levelFill.bottomAnchor.constraint(equalTo: levelTrack.bottomAnchor),
                fillWidth,
            ])
            window.contentView = container
            recordingWindow = window
            recordingLabel = label
            recordingCancelButton = cancelButton
            recordingLevelFill = levelFill
            recordingLevelWidthConstraint = fillWidth
        }
        recordingWindow?.setFrame(rect, display: true)
        recordingLabel?.stringValue = text
        recordingCancelButton?.isHidden = audioRecorder == nil
            && !recordingStartPending
            && dictationTranscriptionID == nil
        updateRecordingLevelMeter(dictationAudioLevel)
        recordingWindow?.makeKeyAndOrderFront(nil)
        recordingWindow?.orderFrontRegardless()
    }

    private func recordingOverlayFrame() -> NSRect {
        let fallbackFrame = NSRect(x: 0, y: 0, width: 1280, height: 800)
        let mouseLocation = NSEvent.mouseLocation
        let screen = NSScreen.screens.first { screen in
            NSMouseInRect(mouseLocation, screen.frame, false)
        } ?? NSScreen.main ?? NSScreen.screens.first
        let screenFrame = screen?.visibleFrame ?? fallbackFrame
        let margin: CGFloat = 18
        let availableWidth = max(220, screenFrame.width - (margin * 2))
        let width = min(max(260, screenFrame.width * 0.22), min(360, availableWidth))
        let height: CGFloat = 46
        let x = max(screenFrame.minX + margin, screenFrame.maxX - width - margin)
        let y = screenFrame.minY + margin
        return NSRect(x: x, y: y, width: width, height: height)
    }

    private func updateRecordingLevelMeter(_ level: Double) {
        guard let window = recordingWindow,
              let constraint = recordingLevelWidthConstraint else {
            return
        }
        let clamped = min(1.0, max(0.0, level))
        let width = max(2, (window.frame.width - 24) * clamped)
        constraint.constant = width
        let hue = 0.32 - (0.22 * clamped)
        recordingLevelFill?.layer?.backgroundColor = NSColor(
            hue: hue,
            saturation: 0.78,
            brightness: 0.9,
            alpha: 1
        ).cgColor
    }

    private func hideRecordingOverlay() {
        recordingWindow?.orderOut(nil)
    }

    private func ensureStartupOrchestration() {
        if startupOrchestrationInProgress {
            return
        }
        startupOrchestrationInProgress = true
        statusMenuItem.title = "Checking DocReader startup..."

        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else {
                return
            }
            let cliExitCode = self.runReadDocsEnsure()
            if cliExitCode != 0 {
                self.ensureFallbackStartupDependencies()
            }

            DispatchQueue.main.async {
                self.startupOrchestrationInProgress = false
                self.refreshWebState()
            }
        }
    }

    private func runReadDocsEnsure() -> Int32 {
        guard let readDocsURL = readDocsCommandURL() else {
            return 1
        }
        return runProcess(
            readDocsURL.path,
            ["ensure"],
            environment: startupOrchestrationEnvironment(),
            currentDirectory: managedRoot
        )
    }

    private func readDocsCommandURL() -> URL? {
        let environment = ProcessInfo.processInfo.environment
        var candidates: [String] = []
        if let override = environment["DOC_READER_READ_DOCS_BIN"],
           !override.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            candidates.append(override)
        }
        candidates.append(managedRoot.appendingPathComponent("read-docs").path)
        candidates.append("/opt/homebrew/bin/read-docs")
        candidates.append("/usr/local/bin/read-docs")

        return candidates
            .first { FileManager.default.isExecutableFile(atPath: $0) }
            .map { URL(fileURLWithPath: $0) }
    }

    private func startupOrchestrationEnvironment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let defaultPath = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
        if let currentPath = environment["PATH"], !currentPath.isEmpty {
            environment["PATH"] = "\(defaultPath):\(currentPath)"
        } else {
            environment["PATH"] = defaultPath
        }
        environment["DOC_READER_MANAGED_ROOT"] = managedRoot.path
        environment["DOC_READER_VENV_DIR"] = managedRoot.appendingPathComponent(".venv", isDirectory: true).path
        environment["DOC_READER_TTS_UMBRA_URL"] = "http://100.72.151.28:8771"
        environment["DOC_READER_TTS_MAC_URL"] = "http://127.0.0.1:8772"
        return environment
    }

    private func ensureFallbackStartupDependencies() {
        kickstartMacTtsLaunchAgent()
        if shouldAutoStartRemoteSpeech {
            ensureUmbraTtsRunning()
        }
    }

    private var shouldAutoStartRemoteSpeech: Bool {
        ProcessInfo.processInfo.environment["DOC_READER_REMOTE_SPEECH_AUTOSTART"] == "1"
    }

    private func ensureUmbraTtsRunning() {
        if isHealthEndpointReady(umbraTtsHealthURL, timeoutInterval: 1.0) {
            return
        }
        setStatusMenuTitle("Starting remote speech service...")
        let environment = startupOrchestrationEnvironment()
        let stopCommand = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File \"\(umbraTtsRootWindowsPath)\\stop-tts.ps1\""
        _ = runProcess(
            "/usr/bin/ssh",
            [umbraSSHHost, "cmd", "/c", stopCommand],
            environment: environment,
            currentDirectory: managedRoot
        )
        _ = runProcess(
            "/usr/bin/ssh",
            [umbraSSHHost, "cmd", "/c", "schtasks /Run /TN DocReaderTTS"],
            environment: environment,
            currentDirectory: managedRoot
        )
        _ = waitForHealth(umbraTtsHealthURL, attempts: 60, interval: 0.5)
    }

    private func waitForHealth(_ url: URL, attempts: Int, interval: TimeInterval) -> Bool {
        for _ in 0..<attempts {
            if isHealthEndpointReady(url, timeoutInterval: 1.5) {
                return true
            }
            Thread.sleep(forTimeInterval: interval)
        }
        return false
    }

    private func setStatusMenuTitle(_ title: String) {
        DispatchQueue.main.async { [weak self] in
            self?.statusMenuItem.title = title
        }
    }

    private func ensureWebAppRunning(completion: (() -> Void)? = nil) {
        if isWebHealthy() {
            refreshWebState(completion: completion)
            return
        }

        statusMenuItem.title = "Starting DocReader web app..."
        kickstartWebLaunchAgent()

        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else {
                return
            }

            var ready = self.waitForWebHealth()
            if !ready && !self.webFallbackDisabled {
                self.startFallbackWebApp()
                ready = self.waitForWebHealth()
            }

            DispatchQueue.main.async {
                if ready {
                    self.refreshWebState(completion: completion)
                } else {
                    self.statusMenuItem.title = "DocReader web app is not reachable."
                    self.updateMenuControls(running: false, paused: false)
                }
            }
        }
    }

    private func waitForWebHealth() -> Bool {
        for _ in 0..<16 {
            if isWebHealthy() {
                return true
            }
            Thread.sleep(forTimeInterval: 0.25)
        }
        return false
    }

    private func isWebHealthy() -> Bool {
        isHealthEndpointReady(webURL(path: "/healthz"), timeoutInterval: 0.75)
    }

    private func isHealthEndpointReady(_ url: URL, timeoutInterval: TimeInterval) -> Bool {
        var request = URLRequest(url: url)
        request.timeoutInterval = timeoutInterval
        let semaphore = DispatchSemaphore(value: 0)
        var healthy = false
        URLSession.shared.dataTask(with: request) { data, response, _ in
            defer { semaphore.signal() }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let data else {
                return
            }
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            healthy = payload?["ok"] as? Bool == true
        }.resume()

        _ = semaphore.wait(timeout: .now() + timeoutInterval + 0.25)
        return healthy
    }

    private func refreshWebState(completion: (() -> Void)? = nil) {
        var request = URLRequest(url: webURL(path: "/api/native/status"))
        request.timeoutInterval = 1.0
        URLSession.shared.dataTask(with: request) { [weak self] data, response, _ in
            guard let self else {
                return
            }
            guard let http = response as? HTTPURLResponse, http.statusCode == 200, let data else {
                DispatchQueue.main.async {
                    self.webReadingPaused = false
                    self.webActiveItemID = nil
                    self.statusMenuItem.title = "DocReader web app is not reachable."
                    self.updateMenuControls(running: false, paused: false)
                    completion?()
                }
                return
            }
            let payload = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let status = payload?["status"] as? String ?? "DocReader web app ready."
            let running = payload?["running"] as? Bool ?? false
            let paused = payload?["paused"] as? Bool ?? false
            let activeID = payload?["active_id"] as? String ?? ""
            let stt = payload?["stt"] as? [String: Any]
            let sttEnabled = stt?["enabled"] as? Bool ?? false
            let microphone = stt?["microphone"] as? [String: Any]
            let selectedMicrophoneID = microphone?["selected_id"] as? String ?? ""
            let hotkeys = stt?["hotkeys"] as? [String: Any]
            let dictationSpecFromWeb = hotkeys?["dictation_key"] as? String ?? ""
            let readbackSpecFromWeb = hotkeys?["selection_shortcut"] as? String ?? ""
            DispatchQueue.main.async {
                self.webReadingPaused = paused
                self.webActiveItemID = activeID.isEmpty ? nil : activeID
                self.dictationEnabled = sttEnabled
                self.selectedMicrophoneID = selectedMicrophoneID
                self.applyHotkeySettings(dictation: dictationSpecFromWeb, readback: readbackSpecFromWeb)
                if sttEnabled {
                    self.requestInputMonitoringAccessIfNeeded()
                }
                self.statusMenuItem.title = status
                self.updateMenuControls(running: running, paused: paused)
                completion?()
            }
        }.resume()
    }

    private func postWebJSON(path: String, payload: [String: Any]?, openWhenDone: Bool) {
        ensureWebAppRunning { [weak self] in
            guard let self else {
                return
            }
            var request = URLRequest(url: self.webURL(path: path))
            request.httpMethod = "POST"
            request.timeoutInterval = 10
            if let payload {
                request.setValue("application/json", forHTTPHeaderField: "Content-Type")
                request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
            }

            URLSession.shared.dataTask(with: request) { data, response, error in
                let succeeded = (response as? HTTPURLResponse)?.statusCode == 200 && error == nil
                DispatchQueue.main.async {
                    if succeeded {
                        self.refreshWebState()
                        if openWhenDone && !self.shouldSuppressBrowserOpenForDictation() {
                            NSWorkspace.shared.open(self.webBaseURL)
                        }
                    } else {
                        let message = data.flatMap { String(data: $0, encoding: .utf8) }
                            ?? error?.localizedDescription
                            ?? "Request failed."
                        self.statusMenuItem.title = message
                    }
                }
            }.resume()
        }
    }

    private func webURL(path: String) -> URL {
        URL(string: path, relativeTo: webBaseURL)!.absoluteURL
    }

    private func urlPathComponent(_ value: String) -> String {
        var allowed = CharacterSet.urlPathAllowed
        allowed.remove(charactersIn: "/")
        return value.addingPercentEncoding(withAllowedCharacters: allowed) ?? value
    }

    private func kickstartWebLaunchAgent() {
        kickstartLaunchAgent(label: webLaunchAgentLabel)
    }

    private func kickstartMacTtsLaunchAgent() {
        kickstartLaunchAgent(label: ttsLaunchAgentLabel)
    }

    private func kickstartLaunchAgent(label: String) {
        let domain = "gui/\(getuid())"
        let target = "\(domain)/\(label)"
        let plistURL = URL(fileURLWithPath: NSHomeDirectory())
            .appendingPathComponent("Library/LaunchAgents/\(label).plist")

        if runProcess("/bin/launchctl", ["print", target]) != 0,
           FileManager.default.fileExists(atPath: plistURL.path) {
            _ = runProcess("/bin/launchctl", ["bootstrap", domain, plistURL.path])
            _ = runProcess("/bin/launchctl", ["enable", target])
        }
        _ = runProcess("/bin/launchctl", ["kickstart", "-k", target])
    }

    private func startFallbackWebApp() {
        if fallbackWebProcess?.isRunning == true {
            return
        }
        let pythonURL = managedRoot.appendingPathComponent(".venv/bin/python")
        guard FileManager.default.isExecutableFile(atPath: pythonURL.path) else {
            return
        }
        let process = Process()
        process.executableURL = pythonURL
        process.arguments = [
            "-m",
            "doc_reader.webapp",
            "--host",
            "127.0.0.1",
            "--port",
            "8766",
        ]
        var environment = ProcessInfo.processInfo.environment
        environment["PYTHONPATH"] = managedRoot.path
        environment["DOC_READER_MANAGED_ROOT"] = managedRoot.path
        environment["DOC_READER_TTS_UMBRA_URL"] = "http://100.72.151.28:8771"
        environment["DOC_READER_TTS_MAC_URL"] = "http://127.0.0.1:8772"
        process.environment = environment
        process.currentDirectoryURL = managedRoot
        try? process.run()
        fallbackWebProcess = process
    }

    private var webFallbackDisabled: Bool {
        let value = ProcessInfo.processInfo.environment["DOC_READER_DISABLE_WEB_FALLBACK"] ?? ""
        return ["1", "true", "yes"].contains(value.lowercased())
    }

    @discardableResult
    private func runProcess(
        _ executable: String,
        _ arguments: [String],
        environment: [String: String]? = nil,
        currentDirectory: URL? = nil
    ) -> Int32 {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: executable)
        process.arguments = arguments
        process.environment = environment
        process.currentDirectoryURL = currentDirectory
        process.standardOutput = nil
        process.standardError = nil
        do {
            try process.run()
            process.waitUntilExit()
            return process.terminationStatus
        } catch {
            return 1
        }
    }

    private func updateMenuControls(running: Bool = false, paused: Bool = false) {
        pauseMenuItem.isEnabled = running || paused
        pauseMenuItem.title = paused ? "Resume Web Reading" : "Pause Web Reading"
        stopMenuItem.isEnabled = running || paused
    }
}

private extension FileManager {
    func createDirectoryIfNeeded(at url: URL) {
        try? createDirectory(at: url, withIntermediateDirectories: true)
    }
}

private let app = NSApplication.shared
private let delegate = AppDelegate()
app.delegate = delegate
app.run()
