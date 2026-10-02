//
//  SiriIntents.swift
//  StreamLink iOS — asking Siri about the library (20.4.0 spike).
//
//  "Is Star Wars done downloading?"  "What's downloading in StreamLink?"
//
//  Siri reaches an app only through App Intents. These are deliberately thin:
//  the host decides which title was meant and writes the sentence
//  (voicestatus.py, GET /api/voice/titles and /api/voice/status), so the wording
//  and the matching change with a host update and never need a new app build.
//
//  ── What this spike is for ──────────────────────────────────────────────────
//  Two things no document settles, so every call is written to the diagnostic
//  log (`siri-*` rows) to be read back off the phone:
//    1. Does Siri route a plain question here WITHOUT the app's name, or only
//       the App Shortcut phrases below, which carry it?
//    2. What does Siri hand over as the title: the spoken words (`siri-match`),
//       or an entity it already resolved (`siri-resolve`)?
//
//  ── Where the host address comes from ───────────────────────────────────────
//  An intent runs in the app's process with NO web view: the system starts the
//  app in the background just to run perform(). The dashboard's own record of
//  its server lives in the page's localStorage, which is not readable from
//  here, so MainViewController.watchHostURL copies the origin of whatever host
//  the web view last opened into the App Group (AppGroupConfig.hostUrl).
//
//  Requires iOS 17, like the other intents in this app.
//

import Foundation
import AppIntents

@available(iOS 17.0, *)
enum VoiceClient {
    struct Title: Decodable { let key: String; let name: String }
    private struct Titles: Decodable { let titles: [Title] }
    private struct Status: Decodable { let speech: String }

    static let unreachable = "I can't reach your StreamLink server right now."
    static let unconfigured = "Open StreamLink and connect to your server first."

    private static func get(_ path: String, _ query: [URLQueryItem]) async -> Data? {
        guard let base = AppGroupConfig.hostUrl ?? AppGroupConfig.serverUrl, !base.isEmpty,
              var parts = URLComponents(string: base + path) else { return nil }
        parts.queryItems = query.isEmpty ? nil : query
        guard let url = parts.url else { return nil }
        var request = URLRequest(url: url)
        // Siri gives perform() only a few seconds before it says the app is
        // taking too long; an unreachable host must lose that race, not Siri.
        request.timeoutInterval = 6
        if let d = AppGroupConfig.deviceId, !d.isEmpty { request.setValue(d, forHTTPHeaderField: "X-Device-Id") }
        guard let (data, response) = try? await URLSession.shared.data(for: request),
              (response as? HTTPURLResponse)?.statusCode == 200 else { return nil }
        return data
    }

    /// Library titles; `q` is what Siri heard, nil for the suggested list.
    static func titles(matching q: String?, limit: Int = 12) async -> [Title] {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let q, !q.isEmpty { query.append(URLQueryItem(name: "q", value: q)) }
        guard let data = await get("/api/voice/titles", query),
              let body = try? JSONDecoder().decode(Titles.self, from: data) else { return [] }
        return body.titles
    }

    /// The sentence to say. `key` nil asks what is downloading at all.
    static func status(key: String?) async -> String {
        guard let base = AppGroupConfig.hostUrl ?? AppGroupConfig.serverUrl, !base.isEmpty else {
            return unconfigured
        }
        let query = key.map { [URLQueryItem(name: "key", value: $0)] } ?? []
        guard let data = await get("/api/voice/status", query),
              let body = try? JSONDecoder().decode(Status.self, from: data) else { return unreachable }
        return body.speech
    }
}

/// A show or film in the library, as a person names it. `id` is the host's
/// series key, the same grouping a library tile uses.
@available(iOS 17.0, *)
struct LibraryTitle: AppEntity {
    static var typeDisplayRepresentation = TypeDisplayRepresentation(name: "Title")
    static var defaultQuery = LibraryTitleQuery()

    let id: String
    let name: String

    var displayRepresentation: DisplayRepresentation { DisplayRepresentation(title: "\(name)") }
}

@available(iOS 17.0, *)
struct LibraryTitleQuery: EntityStringQuery {
    func entities(for identifiers: [String]) async throws -> [LibraryTitle] {
        let all = await VoiceClient.titles(matching: nil, limit: 500)
        let byKey = Dictionary(all.map { ($0.key, $0.name) }, uniquingKeysWith: { a, _ in a })
        DiagLog.shared.write("siri-resolve", ["ids": identifiers, "known": all.count], cat: "app")
        // A title the host no longer lists (or a host that is unreachable) still
        // resolves, so perform() runs and the HOST's answer is what gets said.
        return identifiers.map { LibraryTitle(id: $0, name: byKey[$0] ?? "that title") }
    }

    func entities(matching string: String) async throws -> [LibraryTitle] {
        let hits = await VoiceClient.titles(matching: string)
        DiagLog.shared.write("siri-match", ["heard": string, "hits": hits.map { $0.name }], cat: "app")
        return hits.map { LibraryTitle(id: $0.key, name: $0.name) }
    }

    func suggestedEntities() async throws -> [LibraryTitle] {
        await VoiceClient.titles(matching: nil, limit: 50).map { LibraryTitle(id: $0.key, name: $0.name) }
    }
}

@available(iOS 17.0, *)
struct DownloadStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "Check Download Status"
    static var description = IntentDescription(
        "Says whether a film or show in your StreamLink library has finished downloading, how far along it is, and how long until it is prepped for streaming.",
        categoryName: "Library",
        searchKeywords: ["download", "downloading", "progress", "prepped", "ready", "finished"])
    static var openAppWhenRun = false

    @Parameter(title: "Title", requestValueDialog: "Which title?")
    var target: LibraryTitle

    static var parameterSummary: some ParameterSummary {
        Summary("Is \(\.$target) done downloading?")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let speech = await VoiceClient.status(key: target.id)
        DiagLog.shared.write("siri-status", ["key": target.id, "name": target.name, "said": speech], cat: "app")
        return .result(value: speech, dialog: "\(speech)")
    }
}

@available(iOS 17.0, *)
struct DownloadsOverviewIntent: AppIntent {
    static var title: LocalizedStringResource = "What's Downloading"
    static var description = IntentDescription(
        "Lists what your StreamLink server is downloading right now, with progress and time left.",
        categoryName: "Library",
        searchKeywords: ["download", "downloading", "progress", "queue"])
    static var openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let speech = await VoiceClient.status(key: nil)
        DiagLog.shared.write("siri-overview", ["said": speech], cat: "app")
        return .result(value: speech, dialog: "\(speech)")
    }
}

/// The phrases that work on every Siri, old and new. Each must carry the app's
/// name; a phrase may carry one parameter, and Siri only recognises parameter
/// values it was told about (`updateAppShortcutParameters`, called from
/// MainViewController when the host is known).
@available(iOS 17.0, *)
struct StreamLinkShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: DownloadStatusIntent(),
            phrases: [
                "Is \(\.$target) done downloading in \(.applicationName)",
                "Check on \(\.$target) in \(.applicationName)",
                "How long until \(\.$target) is ready in \(.applicationName)",
                "\(.applicationName) download status",
            ],
            shortTitle: "Download Status",
            systemImageName: "arrow.down.circle")
        AppShortcut(
            intent: DownloadsOverviewIntent(),
            phrases: [
                "What's downloading in \(.applicationName)",
                "What is \(.applicationName) downloading",
                "\(.applicationName) downloads",
            ],
            shortTitle: "What's Downloading",
            systemImageName: "list.bullet")
    }
}
