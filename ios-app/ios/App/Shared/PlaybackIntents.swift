//
//  PlaybackIntents.swift
//  StreamLink iOS — interactive Live Activity buttons for LOCAL background
//  playback (the NativePlayback AVPlayer).
//
//  Contrast with TVRemoteIntents.swift: those control a REMOTE player (host VLC
//  or the YouTube kiosk) and therefore POST to the host. These control the
//  phone's OWN AVPlayer, so they must reach it in-process — no network at all.
//
//  A LiveActivityIntent's perform() runs in the *app's* process. During
//  background playback that process is already alive (the `audio` background
//  mode keeps it so), which is exactly why an in-process command works here.
//
//  ── The cross-target problem, and why PlaybackCommandBus exists ─────────────
//  This file is compiled into BOTH targets (the App and the widget extension) —
//  the widget needs the intent types to attach them to its buttons. But
//  NativePlaybackManager lives ONLY in the App target, so naming it here would
//  break the extension build with "cannot find 'NativePlaybackManager' in
//  scope". The bus is the seam: this file declares only a protocol + a weak
//  static sink, the App target registers the real player as that sink at launch,
//  and the extension compiles against the protocol alone. At tap time the intent
//  runs in the app process, where the sink is populated.
//
//  Requires iOS 17 (interactive Live Activity buttons).
//

import Foundation
import AppIntents

/// One transport action against the local player.
public enum PlaybackCommand: String, Sendable {
    case playPause
    case skipForward
    case skipBack
    case next
    case prev
}

/// Implemented by NativePlaybackManager (App target only).
public protocol PlaybackCommandSink: AnyObject {
    func handlePlaybackCommand(_ cmd: PlaybackCommand)
}

public enum PlaybackCommandBus {
    /// Weak so the sink's own lifecycle stays authoritative. Populated by the
    /// App target; always nil inside the widget extension's process.
    public static weak var sink: PlaybackCommandSink?

    /// Deliver a command, or park it for the app to drain if no sink is live.
    ///
    /// The parked path covers the edge case where the app process was killed
    /// while the Live Activity stayed on screen (activities outlive the process
    /// — the same hazard TVRemote's stray-activity reconciliation exists for).
    /// iOS launches us to run the intent, but our `load()` may not have run yet;
    /// NativePlayback drains the pending command once it's ready.
    public static func send(_ cmd: PlaybackCommand) {
        if let s = sink {
            s.handlePlaybackCommand(cmd)
        } else {
            AppGroupConfig.pendingPlaybackCommand = cmd.rawValue
        }
    }
}

@available(iOS 17.0, *)
struct PlaybackPlayPauseIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Play / Pause"
    func perform() async throws -> some IntentResult {
        PlaybackCommandBus.send(.playPause)
        return .result()
    }
}

@available(iOS 17.0, *)
struct PlaybackSkipForwardIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Skip Forward"
    func perform() async throws -> some IntentResult {
        PlaybackCommandBus.send(.skipForward)
        return .result()
    }
}

@available(iOS 17.0, *)
struct PlaybackSkipBackIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Skip Back"
    func perform() async throws -> some IntentResult {
        PlaybackCommandBus.send(.skipBack)
        return .result()
    }
}

@available(iOS 17.0, *)
struct PlaybackNextIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Next Episode"
    func perform() async throws -> some IntentResult {
        PlaybackCommandBus.send(.next)
        return .result()
    }
}

@available(iOS 17.0, *)
struct PlaybackPrevIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Previous Episode"
    func perform() async throws -> some IntentResult {
        PlaybackCommandBus.send(.prev)
        return .result()
    }
}
