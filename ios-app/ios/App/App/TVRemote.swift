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

    @available(iOS 17.0, *)
    private var activity: Activity<TVRemoteAttributes>? {
        get { _activity as? Activity<TVRemoteAttributes> }
        set { _activity = newValue }
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

        // Reuse a running activity rather than stacking duplicates.
        if let act = activity {
            Task { await act.update(ActivityContent(state: state, staleDate: nil)) }
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
        guard #available(iOS 17.0, *), let act = activity else { call.resolve(); return }
        activity = nil
        Task { await act.end(nil, dismissalPolicy: .immediate) }
        call.resolve()
    }
}
