//
//  TVRemoteIntents.swift
//  StreamLink iOS — interactive Live Activity buttons for the TV remote.
//
//  These LiveActivityIntents back the Dynamic Island / lock-screen buttons. A
//  LiveActivityIntent's perform() runs in the *app's* process (the system spins
//  it up briefly even while the app is suspended), so it can make a network call
//  to the host control endpoints. Config (host URL, token, VLC-vs-YouTube path)
//  comes from the shared App Group (AppGroupConfig), written by the TVRemote
//  plugin when playback starts.
//
//  Requires iOS 17 (interactive Live Activity buttons).
//

import Foundation
import AppIntents
import ActivityKit

@available(iOS 17.0, *)
enum TVRemoteAction { case playPause, volumeUp, volumeDown }

@available(iOS 17.0, *)
enum TVRemoteClient {
    /// Fire one control command at the host. Best-effort: failures are swallowed
    /// (the button just no-ops) since there is no good way to surface an error
    /// from a Live Activity button.
    static func send(_ action: TVRemoteAction) async {
        guard let base = AppGroupConfig.serverUrl, !base.isEmpty,
              let baseURL = URL(string: base) else { return }
        let isYouTube = AppGroupConfig.isYouTube
        let token = AppGroupConfig.deviceToken

        var request: URLRequest
        if isYouTube {
            // POST /api/youtube/control  { action, value? }
            guard let url = URL(string: "/api/youtube/control", relativeTo: baseURL) else { return }
            request = URLRequest(url: url)
            request.httpMethod = "POST"
            request.setValue("application/json", forHTTPHeaderField: "Content-Type")
            let body: [String: Any]
            switch action {
            case .playPause:  body = ["action": "playpause"]
            case .volumeUp:   body = ["action": "volume_step", "value": 10]
            case .volumeDown: body = ["action": "volume_step", "value": -10]
            }
            request.httpBody = try? JSONSerialization.data(withJSONObject: body)
        } else {
            // VLC: dedicated POST routes, params in the query string.
            let path: String
            switch action {
            case .playPause:  path = "/api/vlc/pause"
            case .volumeUp:   path = "/api/vlc/volume/up?step=10"
            case .volumeDown: path = "/api/vlc/volume/down?step=10"
            }
            guard let url = URL(string: path, relativeTo: baseURL) else { return }
            request = URLRequest(url: url)
            request.httpMethod = "POST"
        }
        if let t = token, !t.isEmpty { request.setValue(t, forHTTPHeaderField: "X-Device-Token") }
        request.timeoutInterval = 8

        _ = try? await URLSession.shared.data(for: request)
    }

    /// Optimistically flip the paused flag on the running TV-remote activity so
    /// the glyph updates immediately without waiting for the next state sync.
    static func togglePausedOptimistically() async {
        for activity in Activity<TVRemoteAttributes>.activities {
            var s = activity.content.state
            s.isPaused.toggle()
            await activity.update(ActivityContent(state: s, staleDate: nil))
        }
    }
}

@available(iOS 17.0, *)
struct TVPlayPauseIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Play / Pause"
    func perform() async throws -> some IntentResult {
        await TVRemoteClient.togglePausedOptimistically()
        await TVRemoteClient.send(.playPause)
        return .result()
    }
}

@available(iOS 17.0, *)
struct TVVolumeUpIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Volume Up"
    func perform() async throws -> some IntentResult {
        await TVRemoteClient.send(.volumeUp)
        return .result()
    }
}

@available(iOS 17.0, *)
struct TVVolumeDownIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Volume Down"
    func perform() async throws -> some IntentResult {
        await TVRemoteClient.send(.volumeDown)
        return .result()
    }
}
