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
    var tvMode: Bool
    var canPrev: Bool
    var canNext: Bool
}

final class PlaybackLiveActivity {
    static let shared = PlaybackLiveActivity()

    /// Type-erased so this compiles on the app's 15.0 deployment target.
    private var _activity: Any?
    private var lastPush = Date.distantPast
    private var lastKey = ""

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
        guard #available(iOS 16.1, *) else { return }
        let strays = Activity<PlaybackAttributes>.activities
        guard !strays.isEmpty else { return }
        Task { for a in strays { await a.end(nil, dismissalPolicy: .immediate) } }
        _activity = nil
    }

    /// MUST be called while the app is FOREGROUND. `Activity.request` from a
    /// background handler is unreliable, which is why NativePlaybackManager
    /// starts the activity on the first `arm()` rather than at handoff.
    func start(state: PlaybackLiveState) {
        guard #available(iOS 16.2, *) else { return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }

        let content = contentState(state)
        let live = Activity<PlaybackAttributes>.activities
        if let keep = live.first {
            activity = keep
            if live.count > 1 {
                Task { for a in live.dropFirst() { await a.end(nil, dismissalPolicy: .immediate) } }
            }
            Task { await keep.update(ActivityContent(state: content, staleDate: nil)) }
            return
        }
        do {
            activity = try Activity.request(
                attributes: PlaybackAttributes(),
                content: ActivityContent(state: content, staleDate: nil),
                pushType: nil)
            lastPush = Date()
            lastKey = changeKey(state)
        } catch {
            _activity = nil
        }
    }

    func update(state: PlaybackLiveState, force: Bool) {
        guard #available(iOS 16.2, *), let act = activity else { return }
        let key = changeKey(state)
        let due = Date().timeIntervalSince(lastPush) >= heartbeat
        guard force || key != lastKey || due else { return }
        lastKey = key
        lastPush = Date()
        let content = contentState(state)
        Task { await act.update(ActivityContent(state: content, staleDate: nil)) }
    }

    func end() {
        guard #available(iOS 16.1, *) else { return }
        let live = Activity<PlaybackAttributes>.activities
        _activity = nil
        lastKey = ""
        guard !live.isEmpty else { return }
        Task { for a in live { await a.end(nil, dismissalPolicy: .immediate) } }
    }

    // MARK: Helpers

    /// Everything EXCEPT position — a moving playhead is not a reason to push.
    private func changeKey(_ s: PlaybackLiveState) -> String {
        "\(s.title)|\(s.series)|\(s.isPaused)|\(s.external)|\(s.tvMode)|\(s.canPrev)|\(s.canNext)"
    }

    @available(iOS 16.1, *)
    private func contentState(_ s: PlaybackLiveState) -> PlaybackAttributes.ContentState {
        PlaybackAttributes.ContentState(
            title: s.title, series: s.series, isPaused: s.isPaused,
            position: s.position, duration: s.duration, stamp: Date(),
            external: s.external, tvMode: s.tvMode,
            canPrev: s.canPrev, canNext: s.canNext)
    }
}
