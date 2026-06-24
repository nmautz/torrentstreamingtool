//
//  AppGroupConfig.swift
//  StreamLink iOS — shared App Group config (App target + widget extension).
//
//  The TV-remote App Intents (TVRemoteIntents.swift) run in the *app's* process
//  but are triggered from the Live Activity in the widget extension. They need to
//  know where to POST control commands. We stash the host base URL + pairing
//  token + which playback path is active (VLC vs YouTube) in the shared App Group
//  UserDefaults. The TVRemote Capacitor plugin writes these on start/update; the
//  intents read them at tap time.
//
//  App Group: group.com.streamlink.client (declared in both targets' entitlements).
//

import Foundation

public enum AppGroupConfig {
    public static let suiteName = "group.com.streamlink.client"

    private static var defaults: UserDefaults? { UserDefaults(suiteName: suiteName) }

    private enum Key {
        static let serverUrl   = "tvremote.serverUrl"
        static let deviceToken = "tvremote.deviceToken"
        static let isYouTube   = "tvremote.isYouTube"
    }

    public static var serverUrl: String? {
        get { defaults?.string(forKey: Key.serverUrl) }
        set { defaults?.set(newValue, forKey: Key.serverUrl) }
    }

    public static var deviceToken: String? {
        get { defaults?.string(forKey: Key.deviceToken) }
        set { defaults?.set(newValue, forKey: Key.deviceToken) }
    }

    public static var isYouTube: Bool {
        get { defaults?.bool(forKey: Key.isYouTube) ?? false }
        set { defaults?.set(newValue, forKey: Key.isYouTube) }
    }

    /// Write all remote-control config at once (called by the TVRemote plugin).
    public static func setRemote(serverUrl: String?, token: String?, isYouTube: Bool) {
        let d = defaults
        d?.set(serverUrl, forKey: Key.serverUrl)
        d?.set(token, forKey: Key.deviceToken)
        d?.set(isYouTube, forKey: Key.isYouTube)
    }
}
