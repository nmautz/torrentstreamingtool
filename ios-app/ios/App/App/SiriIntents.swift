//
//  SiriIntents.swift
//  StreamLink iOS — asking Siri about the library (20.4.0 spike).
//
//  "Is Star Wars done downloading in StreamLink?"  "What's ready in StreamLink?"
//  "Download Star Wars, the original one, in StreamLink."
//
//  Siri reaches an app only through App Intents. These are deliberately thin:
//  the host decides which title was meant and writes the sentence
//  (voicestatus.py, voicepick.py, /api/voice/*), so the wording
//  and the matching change with a host update and never need a new app build.
//
//  ── What the first device tests showed (iPhone 16, iOS 27, 2026-10-02) ──────
//  Siri uses an intent as a TOOL: it picks one by its title and description,
//  runs it, and answers in its own words out of what came back. So:
//    * The description is what Siri routes on. "What's downloading" was never
//      run for "is SpongeBob prepped?"; Siri said it could not search the app.
//    * The title is a plain String. As an AppEntity (20.4.0) Siri never once
//      called the entity query, so it could not fill the parameter and the
//      intent was never chosen. The host matches the spoken name instead.
//    * The sentence must contain "StreamLink"; without it Siri never comes here.
//  Every run is written to the diagnostic log (`siri-*` rows).
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
    private struct Status: Decodable { let speech: String }

    /// What the host understood a spoken film name to mean (GET /api/voice/find).
    struct Found: Decodable {
        let found: Bool
        let speech: String
        let tmdb_id: Int?
        let name: String?
        let confirm: String?
    }

    static let unreachable = "I can't reach your StreamLink server right now."
    static let unconfigured = "Open StreamLink and connect to your server first."

    private static var base: String? {
        guard let b = AppGroupConfig.hostUrl ?? AppGroupConfig.serverUrl, !b.isEmpty else { return nil }
        return b
    }

    private static func get(_ path: String, _ query: [URLQueryItem]) async -> Data? {
        guard let base, var parts = URLComponents(string: base + path) else { return nil }
        parts.queryItems = query.isEmpty ? nil : query
        guard let url = parts.url else { return nil }
        var request = URLRequest(url: url)
        // Siri gives perform() only a few seconds before it says the app is
        // taking too long; an unreachable host must lose that race, not Siri.
        request.timeoutInterval = 6
        return await send(request)
    }

    /// One retry on a transport error, and only on one. Seen on the first real
    /// voice download: the app is suspended while Siri waits for "yes", the
    /// kept-alive connection the lookup used dies meanwhile, and the POST that
    /// follows fails at once without reaching the host (no row in its request
    /// log). URLSession retries a GET over a fresh connection by itself but
    /// never a POST. Retrying is safe: the host joins a search already running
    /// and refuses a second copy of a torrent it has.
    private static func send(_ req: URLRequest) async -> Data? {
        var request = req
        if let d = AppGroupConfig.deviceId, !d.isEmpty { request.setValue(d, forHTTPHeaderField: "X-Device-Id") }
        for attempt in 0..<2 {
            do {
                let (data, response) = try await URLSession.shared.data(for: request)
                let code = (response as? HTTPURLResponse)?.statusCode ?? 0
                if code == 200 { return data }
                DiagLog.shared.write("siri-http", ["path": request.url?.path ?? "", "status": code], cat: "app")
                return nil
            } catch {
                let e = error as NSError
                DiagLog.shared.write("siri-http", ["path": request.url?.path ?? "", "err": e.code,
                                                   "domain": e.domain, "attempt": attempt], cat: "app")
                if e.code == NSURLErrorTimedOut { return nil }
            }
        }
        return nil
    }

    /// Step 1 of a download: which film the spoken words mean. Starts nothing.
    static func find(_ spoken: String) async -> Found {
        guard base != nil else {
            return Found(found: false, speech: unconfigured, tmdb_id: nil, name: nil, confirm: nil)
        }
        guard let data = await get("/api/voice/find", [URLQueryItem(name: "q", value: spoken)]),
              let body = try? JSONDecoder().decode(Found.self, from: data) else {
            return Found(found: false, speech: unreachable, tmdb_id: nil, name: nil, confirm: nil)
        }
        return body
    }

    /// Step 2: start it. The host holds the call for up to 8 s while it searches
    /// the indexers, so this waits longer than a status question does.
    static func download(tmdbId: Int) async -> String {
        guard let base, let url = URL(string: base + "/api/voice/download") else { return unconfigured }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONSerialization.data(withJSONObject: ["tmdb_id": tmdbId])
        request.timeoutInterval = 12
        guard let data = await send(request),
              let body = try? JSONDecoder().decode(Status.self, from: data) else { return unreachable }
        return body.speech
    }

    /// The sentence to say. `name` is the title as spoken, matched on the host;
    /// nil asks for the full report.
    static func status(name: String?) async -> String {
        guard let base = AppGroupConfig.hostUrl ?? AppGroupConfig.serverUrl, !base.isEmpty else {
            return unconfigured
        }
        let query = name.map { [URLQueryItem(name: "q", value: $0)] } ?? []
        guard let data = await get("/api/voice/status", query),
              let body = try? JSONDecoder().decode(Status.self, from: data) else { return unreachable }
        return body.speech
    }
}

@available(iOS 17.0, *)
struct DownloadStatusIntent: AppIntent {
    static var title: LocalizedStringResource = "Check a Title"
    static var description = IntentDescription(
        "Looks up one film or show in the StreamLink library by name and reports whether it has finished downloading, how far along it is and how long is left, and whether it is prepped, available and ready to stream. Use for any question about a specific title in StreamLink.",
        categoryName: "Library",
        searchKeywords: ["download", "downloading", "progress", "prepped", "ready", "finished", "available", "library"])
    static var openAppWhenRun = false

    @Parameter(title: "Title",
               description: "The name of the film or show, as spoken, for example SpongeBob or Star Wars.",
               requestValueDialog: "Which title?")
    var name: String

    static var parameterSummary: some ParameterSummary {
        Summary("Check \(\.$name) in the library")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let speech = await VoiceClient.status(name: name)
        DiagLog.shared.write("siri-status", ["heard": name, "said": speech], cat: "app")
        return .result(value: speech, dialog: "\(speech)")
    }
}

@available(iOS 17.0, *)
struct DownloadsOverviewIntent: AppIntent {
    static var title: LocalizedStringResource = "Library Status"
    static var description = IntentDescription(
        "Reports the state of the StreamLink library: what is downloading now with progress and time left, which films and shows most recently finished downloading, and whether each is prepped, available and ready to stream. Use for any question about what is downloading, finished, available, prepped or ready in StreamLink.",
        categoryName: "Library",
        searchKeywords: ["download", "downloading", "progress", "queue", "prepped", "ready", "finished", "available", "library", "status"])
    static var openAppWhenRun = false

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let speech = await VoiceClient.status(name: nil)
        DiagLog.shared.write("siri-overview", ["said": speech], cat: "app")
        return .result(value: speech, dialog: "\(speech)")
    }
}

/// "Download Star Wars, the original one in StreamLink." Films only (20.6.0).
///
/// Two calls to the host with the person in between: /find says which film it
/// took the words to mean, `requestConfirmation` puts that back ("Download Star
/// Wars (1977)?"), and only a yes reaches /download. Nobody is looking at a
/// screen, so the confirmation is the only check that the right film starts.
@available(iOS 17.0, *)
struct DownloadFilmIntent: AppIntent {
    // Siri's first reading of "download Star Wars in StreamLink" was "find that
    // film INSIDE the app": it looked, found nothing, and showed its own film
    // card without ever running this. So the description says outright that
    // this is for a film the app does NOT have yet.
    static var title: LocalizedStringResource = "Request a New Film"
    static var description = IntentDescription(
        "Requests a film that is NOT yet in StreamLink: the StreamLink server searches for it and downloads it into the library. The film does not need to exist in the app already, so do not look for it in the app first. Use whenever the user asks StreamLink to download, get, grab, add, request or fetch a film or movie, for example: download the original Star Wars in StreamLink.",
        categoryName: "Library",
        searchKeywords: ["download", "get", "grab", "add", "request", "fetch", "film", "movie", "new"])
    static var openAppWhenRun = false

    @Parameter(title: "Film",
               description: "The film's name as spoken, including anything said about which version, for example: Star Wars the original one, Dune 1984, or Dune the new one.",
               requestValueDialog: "Which film?")
    var name: String

    static var parameterSummary: some ParameterSummary {
        Summary("Download \(\.$name)")
    }

    func perform() async throws -> some IntentResult & ProvidesDialog & ReturnsValue<String> {
        let found = await VoiceClient.find(name)
        DiagLog.shared.write("siri-find", ["heard": name, "found": found.found,
                                           "name": found.name ?? "", "said": found.speech], cat: "app")
        guard found.found, let id = found.tmdb_id else {
            return .result(value: found.speech, dialog: "\(found.speech)")
        }
        let question = found.confirm ?? found.speech
        // Throws if the person says no, which ends the intent with nothing started.
        if #available(iOS 18.0, *) {
            try await requestConfirmation(actionName: .download, dialog: "\(question)")
        } else {
            try await requestConfirmation(result: .result(dialog: "\(question)"),
                                          confirmationActionName: .download)
        }
        let speech = await VoiceClient.download(tmdbId: id)
        DiagLog.shared.write("siri-download", ["tmdb": id, "name": found.name ?? "", "said": speech], cat: "app")
        return .result(value: speech, dialog: "\(speech)")
    }
}

/// The phrases that work on every Siri, old and new. Each must carry the app's
/// name. A phrase cannot carry a String parameter, so the per-title one asks
/// "Which title?" on the old Siri; the new one fills it from the sentence.
@available(iOS 17.0, *)
struct StreamLinkShortcuts: AppShortcutsProvider {
    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: DownloadsOverviewIntent(),
            phrases: [
                "What's downloading in \(.applicationName)",
                "What is \(.applicationName) downloading",
                "What finished downloading in \(.applicationName)",
                "What's ready in \(.applicationName)",
                "\(.applicationName) status",
            ],
            shortTitle: "Library Status",
            systemImageName: "list.bullet")
        AppShortcut(
            intent: DownloadStatusIntent(),
            phrases: [
                "Check a title in \(.applicationName)",
                "Is something done downloading in \(.applicationName)",
                "Is something prepped in \(.applicationName)",
            ],
            shortTitle: "Check a Title",
            systemImageName: "arrow.down.circle")
        AppShortcut(
            intent: DownloadFilmIntent(),
            phrases: [
                "Download a film in \(.applicationName)",
                "Download a movie in \(.applicationName)",
                "Request a film in \(.applicationName)",
                "Request a movie in \(.applicationName)",
                "Add a film to \(.applicationName)",
                "Add a movie to \(.applicationName)",
                "Ask \(.applicationName) to get a film",
            ],
            shortTitle: "Request a New Film",
            systemImageName: "square.and.arrow.down")
    }
}
