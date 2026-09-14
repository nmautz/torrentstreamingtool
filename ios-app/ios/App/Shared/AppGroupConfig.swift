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
        static let pendingCmd  = "playback.pendingCommand"
        static let strandedBri = "playback.strandedBrightness"
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

    /// A Live Activity transport command that arrived with no live player to
    /// take it (the app process had been killed while the activity stayed on
    /// screen). NativePlayback drains this once it's ready. See
    /// PlaybackCommandBus.send.
    public static var pendingPlaybackCommand: String? {
        get { defaults?.string(forKey: Key.pendingCmd) }
        set { defaults?.set(newValue, forKey: Key.pendingCmd) }
    }

    /// Screen brightness captured before TV Mode blanked the display.
    ///
    /// iOS does NOT restore brightness after a crash, so a force-quit or crash
    /// while dimmed would otherwise leave the user with a black phone and no
    /// obvious cause. Persisting it OUTSIDE the process means the next launch
    /// can put it back. `nil`/absent ⇒ nothing to restore.
    public static var strandedBrightness: Double? {
        get {
            guard let d = defaults, d.object(forKey: Key.strandedBri) != nil else { return nil }
            return d.double(forKey: Key.strandedBri)
        }
        set {
            guard let v = newValue else { defaults?.removeObject(forKey: Key.strandedBri); return }
            defaults?.set(v, forKey: Key.strandedBri)
        }
    }

    /// Write all remote-control config at once (called by the TVRemote plugin).
    public static func setRemote(serverUrl: String?, token: String?, isYouTube: Bool) {
        let d = defaults
        d?.set(serverUrl, forKey: Key.serverUrl)
        d?.set(token, forKey: Key.deviceToken)
        d?.set(isYouTube, forKey: Key.isYouTube)
    }
}
