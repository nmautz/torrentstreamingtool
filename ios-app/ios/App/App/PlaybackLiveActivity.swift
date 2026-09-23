//
//  PlaybackLiveActivity.swift
//  StreamLink iOS — the Live Activity for LOCAL background playback.
//
//  Driven by NativePlaybackManager. Modeled on DownloadLiveActivity.swift (the
//  type-erased `_activity: Any?` so this file compiles against the App target's
//  iOS 15.0 deployment target) PLUS TVRemote.swift's stray-activity
//  reconciliation, which is hard-won and deliberately duplicated here:
//
//    • ActivityKit activities OUTLIVE the app process — they stay on the lock
//      screen / Dynamic Island across a relaunch. An in-memory handle is nil
//      after a relaunch even though an activity is still on screen, so every
//      accessor falls back to the system's own list.
//    • start() ADOPTS a running activity instead of stacking a duplicate, and
//      proactively ends extras.
//    • end() ends EVERY activity, not just the tracked one.
//
//  Without these you get "Dynamic Island events go stale and stack up".
//
//  THE CLOCK: updates are deliberately RARE. The widget seeds SwiftUI's
//  self-advancing timer views from (position, stamp) so the lock-screen clock
//  and progress bar run with no pushes at all. ActivityKit budgets updates and
//  starts dropping them if you push every second — which would freeze the very
//  clock the pushes were meant to drive. Push on real state changes only, plus
//  a slow heartbeat.
//

import Foundation
import ActivityKit

/// Plain, availability-free mirror of PlaybackAttributes.ContentState so
/// NativePlaybackManager can build state without `@available` at every call.
struct PlaybackLiveState {
    var title: String
    var series: String
    var isPaused: Bool
    var position: Double
    var duration: Double
    var external: Bool
    var canPrev: Bool
    var canNext: Bool
}

final class PlaybackLiveActivity {
    static let shared = PlaybackLiveActivity()

    /// Type-erased so this compiles on the app's 15.0 deployment target.
    private var _activity: Any?
    private var lastPush = Date.distantPast
    private var lastKey = ""
    /// Throttle for the `la-missing` row — see update().
    private var lastMissLog = Date.distantPast

    /// Only push when something a viewer can SEE changed, or when the heartbeat
    /// is due. `position` is excluded on purpose — the widget advances its own
    /// clock from the stamp.
    private let heartbeat: TimeInterval = 30

    @available(iOS 16.1, *)
    private var activity: Activity<PlaybackAttributes>? {
        get { (_activity as? Activity<PlaybackAttributes>) ?? Activity<PlaybackAttributes>.activities.first }
        set { _activity = newValue }
    }

    /// End anything left over from a previous process. Called on cold start —
    /// nothing is playing yet, so a live playback activity can only be a
    /// leftover whose stop() never reached us.
    func reapStrays() {
        // 16.2, not 16.1: `end(_:dismissalPolicy:)` is 16.2+, and start() is
        // gated the same way, so 16.1 can never have created one of ours.
        guard #available(iOS 16.2, *) else { return }
        let strays = Activity<PlaybackAttributes>.activities
        guard !strays.isEmpty else { return }
        // AN ACTIVITY FOUND AT COLD START IS A STALE ONE, BY DEFINITION —
        // nothing is playing yet, so it can only be the leftover of a session
        // whose end() never ran (force-quit, jetsam, crash). Reaping it has
        // always worked; what was missing is the row saying it happened, which
        // is the difference between "the Island went stale" as a report and as
        // an observation. Written BEFORE the async end so a second death in the
        // same second still leaves the count behind.
        DiagLog.shared.write("la-reap", [
            "kind": "playback", "found": strays.count,
            "ids": strays.prefix(3).map { $0.id }.joined(separator: ","),
        ], cat: "app")
        Task { for a in strays { await a.end(nil, dismissalPolicy: .immediate) } }
        _activity = nil
    }

    /// Count what is REALLY on the lock screen against what we believe is
    /// playing. A `live: 1` with `playing: false` is a stale activity caught in
    /// the act — the exact state the user sees and the one nothing else here can
    /// observe, because every other row describes an intention rather than the
    /// system's own list. Cheap, and only called on rare events.
    func audit(_ why: String, playing: Bool) {
        guard #available(iOS 16.2, *) else { return }
        let live = Activity<PlaybackAttributes>.activities
        guard !live.isEmpty || playing else { return }   // nothing to say
        DiagLog.shared.write("la-audit", [
            "kind": "playback", "why": why, "live": live.count,
            "playing": playing, "tracked": _activity != nil,
            "sincePush": Int(Date().timeIntervalSince(lastPush)),
            "enabled": ActivityAuthorizationInfo().areActivitiesEnabled,
        ], cat: "app")
    }

    /// MUST be called while the app is FOREGROUND. `Activity.request` from a
    /// background handler is unreliable, which is why NativePlaybackManager
    /// starts the activity on the first `arm()` rather than at handoff.
    func start(state: PlaybackLiveState) {
        guard #available(iOS 16.2, *) else {
            DiagLog.shared.write("la-start", ["kind": "playback", "skipped": "os"], cat: "app")
            return
        }
        // "Live Activities are turned off in Settings" and "we started one and it
        // went stale" look identical from the outside: no Island, or an Island
        // that never moves. Only this row tells them apart.
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            DiagLog.shared.write("la-start", ["kind": "playback", "skipped": "disabled"], cat: "app")
            return
        }

        let content = contentState(state)
        let live = Activity<PlaybackAttributes>.activities
        if let keep = live.first {
            activity = keep
            // ADOPTING HAS TO RESET THE DEDUPE STATE TOO. `lastKey`/`lastPush`
            // described the activity we did NOT adopt (or, after a relaunch,
            // nothing at all), so the next update() saw a changed key and a due
            // heartbeat and pushed unconditionally. ActivityKit budgets updates
            // and silently drops them once a session overspends — and a dropped
            // update is precisely a frozen Island. Seed them from what we just
            // pushed instead.
            lastKey = changeKey(state)
            lastPush = Date()
            if live.count > 1 {
                Task { for a in live.dropFirst() { await a.end(nil, dismissalPolicy: .immediate) } }
            }
            Task { await keep.update(ActivityContent(state: content, staleDate: nil)) }
            DiagLog.shared.write("la-start", [
                "kind": "playback", "how": "adopted", "live": live.count,
                "ended": live.count - 1, "title": state.title,
            ], cat: "app")
            return
        }
        do {
            activity = try Activity.request(
                attributes: PlaybackAttributes(),
                content: ActivityContent(state: content, staleDate: nil),
                pushType: nil)
            lastPush = Date()
            lastKey = changeKey(state)
            DiagLog.shared.write("la-start", [
                "kind": "playback", "how": "requested", "title": state.title,
            ], cat: "app")
        } catch {
            // SWALLOWED FOR THE LIFE OF THE FEATURE. `Activity.request` throws on
            // the system activity limit, on a target that disallows them, and
            // when the app is not foreground — every one of which reads to the
            // user as "the Dynamic Island stopped working", with nothing
            // anywhere to say so.
            _activity = nil
            DiagLog.shared.write("la-failed", [
                "kind": "playback", "op": "request",
                "err": String(describing: error),
            ], cat: "app")
        }
    }

    func update(state: PlaybackLiveState, force: Bool) {
        guard #available(iOS 16.2, *) else { return }
        // No handle AND nothing in the system's list: we think we are driving an
        // Island that does not exist. Throttled hard — update() is called from
        // the 1 Hz transport mirror, so an unguarded row here would be the
        // loudest thing in the transcript.
        guard let act = activity else {
            if Date().timeIntervalSince(lastMissLog) > 60 {
                lastMissLog = Date()
                DiagLog.shared.write("la-missing", [
                    "kind": "playback", "title": state.title, "paused": state.isPaused,
                    "enabled": ActivityAuthorizationInfo().areActivitiesEnabled,
                ], cat: "app")
            }
            return
        }
        let key = changeKey(state)
        let due = Date().timeIntervalSince(lastPush) >= heartbeat
        guard force || key != lastKey || due else { return }
        lastKey = key
        lastPush = Date()
        let content = contentState(state)
        Task { await act.update(ActivityContent(state: content, staleDate: nil)) }
    }

    func end(_ why: String = "stop") {
        guard #available(iOS 16.2, *) else { return }
        let live = Activity<PlaybackAttributes>.activities
        _activity = nil
        lastKey = ""
        // `live: 0` here is the interesting one: we asked to end an Island that
        // the system says is not there, so a later stale one cannot be blamed on
        // a missed end() — it was requested again after this point.
        DiagLog.shared.write("la-end", [
            "kind": "playback", "why": why, "live": live.count,
        ], cat: "app")
        guard !live.isEmpty else { return }
        Task { for a in live { await a.end(nil, dismissalPolicy: .immediate) } }
    }

    // MARK: Helpers

    /// Everything EXCEPT position — a moving playhead is not a reason to push.
    private func changeKey(_ s: PlaybackLiveState) -> String {
        "\(s.title)|\(s.series)|\(s.isPaused)|\(s.external)|\(s.canPrev)|\(s.canNext)"
    }

    @available(iOS 16.1, *)
    private func contentState(_ s: PlaybackLiveState) -> PlaybackAttributes.ContentState {
        PlaybackAttributes.ContentState(
            title: s.title, series: s.series, isPaused: s.isPaused,
            position: s.position, duration: s.duration, stamp: Date(),
            external: s.external,
            canPrev: s.canPrev, canNext: s.canNext)
    }
}
