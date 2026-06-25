//
//  TVRemote.swift
//  StreamLink iOS — Capacitor plugin that starts/updates/stops the TV-remote
//  Live Activity from the web dashboard.
//
//  The dashboard (static/index.html) knows what's on the TV (title, paused,
//  volume, VLC-vs-YouTube) and where the host is. When the fullscreen controls
//  are open over an active TV session it calls TVRemote.start(); thereafter it
//  pushes update() on state changes and stop() when playback/controls end. The
//  activity is started while the app is foreground; iOS then surfaces it in the
//  Dynamic Island once the app is minimized. The buttons themselves are wired in
//  the widget (TVRemoteWidget) to LiveActivityIntents (TVRemoteIntents).
//
//  Gated to iOS 17 — interactive Live Activity buttons are the whole point.
//
//  JS surface (Capacitor plugin "TVRemote"):
//    start({ title, isYouTube, paused, volume, serverUrl, token }) -> { started }
//    update({ title?, isYouTube?, paused?, volume? })              -> {}
//    stop()                                                        -> {}
//

import Foundation
import Capacitor
import ActivityKit

@objc(TVRemote)
public class TVRemote: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "TVRemote"
    public let jsName = "TVRemote"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "start",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "update", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop",   returnType: CAPPluginReturnPromise),
    ]

    // Type-erased so this compiles on the app's 15.0 deployment target.
    private var _activity: Any?

    // The tracked activity, reconciled with what ActivityKit actually has live.
    // CRITICAL: ActivityKit activities outlive the app process — they persist on
    // the lock screen / Dynamic Island across relaunches. `_activity` is in-memory
    // only, so after a relaunch (or a headless background launch) it's nil even
    // though an activity is still on screen. Falling back to the system list here
    // means stop()/update() still find a relaunch-orphaned activity instead of
    // leaving it stuck forever, and the next start() adopts it instead of stacking
    // a duplicate. (Was the cause of "Dynamic Island events go stale and stack up".)
    @available(iOS 17.0, *)
    private var activity: Activity<TVRemoteAttributes>? {
        get { (_activity as? Activity<TVRemoteAttributes>) ?? Activity<TVRemoteAttributes>.activities.first }
        set { _activity = newValue }
    }

    // Fresh cold start: nothing is playing yet, so any TV-remote activity still on
    // screen is a leftover from a previous session that ended while we were
    // backgrounded/killed (so the dashboard's stop() never reached us). End them
    // all — the dashboard re-starts one if a session is genuinely active.
    public override func load() {
        guard #available(iOS 17.0, *) else { return }
        let strays = Activity<TVRemoteAttributes>.activities
        guard !strays.isEmpty else { return }
        Task { for act in strays { await act.end(nil, dismissalPolicy: .immediate) } }
        _activity = nil
    }

    @objc func start(_ call: CAPPluginCall) {
        guard #available(iOS 17.0, *) else { call.resolve(["started": false]); return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            call.resolve(["started": false]); return
        }

        let title = call.getString("title") ?? "Now Playing"
        let isYouTube = call.getBool("isYouTube") ?? false
        let paused = call.getBool("paused") ?? false
        let volume = call.getInt("volume") ?? 100

        // Stash control config for the App Intents.
        AppGroupConfig.setRemote(serverUrl: call.getString("serverUrl"),
                                 token: call.getString("token"),
                                 isYouTube: isYouTube)

        let state = TVRemoteAttributes.ContentState(
            title: title, isPaused: paused, isYouTube: isYouTube, volume: volume)

        // Reuse a running activity rather than stacking duplicates, and proactively
        // end any extras so the Island can never show more than one (a relaunch or a
        // missed stop() can leave several live at once).
        let live = Activity<TVRemoteAttributes>.activities
        if let keep = live.first {
            activity = keep
            if live.count > 1 {
                Task { for act in live.dropFirst() { await act.end(nil, dismissalPolicy: .immediate) } }
            }
            Task { await keep.update(ActivityContent(state: state, staleDate: nil)) }
            call.resolve(["started": true]); return
        }
        do {
            let act = try Activity.request(
                attributes: TVRemoteAttributes(),
                content: ActivityContent(state: state, staleDate: nil),
                pushType: nil)
            activity = act
            call.resolve(["started": true])
        } catch {
            call.resolve(["started": false])
        }
    }

    @objc func update(_ call: CAPPluginCall) {
        guard #available(iOS 17.0, *), let act = activity else { call.resolve(); return }

        if call.getString("serverUrl") != nil || call.getString("token") != nil || call.hasOption("isYouTube") {
            AppGroupConfig.setRemote(serverUrl: call.getString("serverUrl") ?? AppGroupConfig.serverUrl,
                                     token: call.getString("token") ?? AppGroupConfig.deviceToken,
                                     isYouTube: call.getBool("isYouTube") ?? AppGroupConfig.isYouTube)
        }

        let prev = act.content.state
        let state = TVRemoteAttributes.ContentState(
            title: call.getString("title") ?? prev.title,
            isPaused: call.getBool("paused") ?? prev.isPaused,
            isYouTube: call.getBool("isYouTube") ?? prev.isYouTube,
            volume: call.getInt("volume") ?? prev.volume)
        Task { await act.update(ActivityContent(state: state, staleDate: nil)) }
        call.resolve()
    }

    @objc func stop(_ call: CAPPluginCall) {
        guard #available(iOS 17.0, *) else { call.resolve(); return }
        // End every live activity, not just the tracked one — a relaunch may have
        // orphaned the original (its in-memory handle is gone) and we still want
        // "playback ended" to clear it from the Island instead of leaving it stale.
        let live = Activity<TVRemoteAttributes>.activities
        activity = nil
        Task { for act in live { await act.end(nil, dismissalPolicy: .immediate) } }
        call.resolve()
    }
}
