//
//  DownloadLiveActivity.swift
//  StreamLink iOS — drives the download-progress Live Activity from the
//  background URLSession in BundleDownloadManager.
//
//  A single aggregate activity represents whatever is downloading right now (the
//  title is the active bundle, or "N downloads" when several run at once). It is
//  started on the first active job, updated as bytes/files land (throttled), and
//  ended — briefly showing a terminal "complete"/"failed" state — when the last
//  job finishes. ActivityKit needs iOS 16.2 for the content API; below that every
//  call here is a no-op and downloads still run (just without the lock-screen UI).
//

import Foundation
import ActivityKit

final class DownloadLiveActivity {
    static let shared = DownloadLiveActivity()
    private init() {}

    private let lock = NSLock()
    private var lastUpdate = Date.distantPast
    private let minInterval: TimeInterval = 1.0   // throttle byte-level updates

    // Type-erased handle so this file compiles on the app's 15.0 deployment target.
    private var _activity: Any?
    /// Throttle for the `la-progress` heartbeat — see sync(). Both are read and
    /// written under `lock`, like `lastUpdate`.
    private var lastBeat = Date.distantPast
    /// sync() runs per URLSession write callback, so the "activities are off"
    /// row has to be once per process, not once per packet.
    private var loggedDisabled = false

    // FALLS BACK TO THE SYSTEM'S OWN LIST, as PlaybackLiveActivity's does and as
    // this one did not. ActivityKit activities outlive the process: a background
    // URLSession transfer that finishes after the app is killed leaves a download
    // Island on screen with no handle behind it, so the next sync() saw `nil`,
    // requested a SECOND activity, and the first sat frozen at whatever
    // percentage it died on. That is the stale-Island report, exactly.
    @available(iOS 16.2, *)
    private var activity: Activity<DownloadActivityAttributes>? {
        get { (_activity as? Activity<DownloadActivityAttributes>)
                ?? Activity<DownloadActivityAttributes>.activities.first }
        set { _activity = newValue }
    }

    /// Cold start: no job exists yet in this process, so a download Island on
    /// screen is a leftover. Symmetric with PlaybackLiveActivity.reapStrays().
    /// A resumed download requests a fresh one on its first sync().
    func reapStrays() {
        guard #available(iOS 16.2, *) else { return }
        let strays = Activity<DownloadActivityAttributes>.activities
        guard !strays.isEmpty else { return }
        DiagLog.shared.write("la-reap", [
            "kind": "download", "found": strays.count,
            "ids": strays.prefix(3).map { $0.id }.joined(separator: ","),
        ], cat: "offline")
        Task { for a in strays { await a.end(nil, dismissalPolicy: .immediate) } }
        _activity = nil
    }

    /// Stand down: something else is drawing this download's progress. On iOS 26+
    /// a granted `BGContinuedProcessingTask` comes with the system's OWN progress
    /// UI, with a cancel button, and ours sat next to it saying the same thing
    /// twice — observed on the 18.15.2 verification run. Ends immediately with no
    /// terminal frame: nothing finished, the drawing just changed hands.
    ///
    /// NOT the same as end(): this is reversible. If the grant expires mid-download
    /// the caller starts syncing again and a fresh activity is requested, because
    /// at that point ours is the only progress UI there is.
    func suppress(_ why: String) {
        guard #available(iOS 16.2, *) else { return }
        lock.lock(); let act = activity; _activity = nil; lock.unlock()
        guard let act = act else { return }
        DiagLog.shared.write("la-suppressed", ["kind": "download", "why": why], cat: "offline")
        Task { await act.end(nil, dismissalPolicy: .immediate) }
    }

    /// Push a fresh snapshot. Starts the activity if none is running.
    /// `force` bypasses the throttle (use on per-file completion / terminal states).
    func sync(title: String, bytesDone: Int64, bytesTotal: Int64, fraction: Double,
              filesDone: Int, fileCount: Int, finished: Bool = false,
              failed: Bool = false, force: Bool = false) {
        guard #available(iOS 16.2, *) else { return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else {
            lock.lock(); let first = !loggedDisabled; loggedDisabled = true; lock.unlock()
            if first {
                DiagLog.shared.write("la-start", ["kind": "download", "skipped": "disabled"], cat: "offline")
            }
            return
        }

        lock.lock()
        if !force {
            let now = Date()
            if now.timeIntervalSince(lastUpdate) < minInterval { lock.unlock(); return }
            lastUpdate = now
        } else {
            lastUpdate = Date()
        }
        let existing = activity
        // Decided under the lock, like the throttle above it.
        let beat = Date().timeIntervalSince(lastBeat) >= 30
        if beat { lastBeat = Date() }
        lock.unlock()

        let state = DownloadActivityAttributes.ContentState(
            title: title, bytesDone: bytesDone, bytesTotal: bytesTotal,
            fraction: fraction, filesDone: filesDone, fileCount: fileCount,
            finished: finished, failed: failed)

        if let act = existing {
            Task { await act.update(ActivityContent(state: state, staleDate: nil)) }
            // A DOWNLOAD THAT IS NOT MOVING LOOKS EXACTLY LIKE ONE THAT IS NOT
            // RUNNING, and the transcript could not tell them apart: the only
            // download rows written were the verdicts (complete / failed /
            // retry), so a transfer that simply stopped advancing while the app
            // was backgrounded produced total silence. This heartbeat is the
            // byte count over time — the one measurement that answers it.
            if beat {
                DiagLog.shared.write("la-progress", [
                    "kind": "download", "title": title, "done": bytesDone,
                    "total": bytesTotal, "files": filesDone, "of": fileCount,
                    "pct": Int(fraction * 100),
                ], cat: "offline")
            }
        } else if !finished && !failed {
            do {
                let act = try Activity.request(
                    attributes: DownloadActivityAttributes(),
                    content: ActivityContent(state: state, staleDate: nil),
                    pushType: nil)
                lock.lock(); activity = act; lock.unlock()
                DiagLog.shared.write("la-start", [
                    "kind": "download", "how": "requested", "title": title,
                    "of": fileCount,
                ], cat: "offline")
            } catch {
                // Activity start can throw (over the system limit, disabled, etc.) —
                // downloads continue regardless. Silent until 18.14.0, which meant
                // "the download Island never appears" had no evidence at all.
                DiagLog.shared.write("la-failed", [
                    "kind": "download", "op": "request",
                    "err": String(describing: error),
                ], cat: "offline")
            }
        }
    }

    /// End the activity, leaving a short-lived terminal frame on screen.
    func end(title: String, finished: Bool, failed: Bool,
             filesDone: Int, fileCount: Int, bytesTotal: Int64) {
        guard #available(iOS 16.2, *) else { return }
        lock.lock(); let act = activity; activity = nil; lock.unlock()
        DiagLog.shared.write("la-end", [
            "kind": "download", "why": failed ? "failed" : (finished ? "complete" : "stop"),
            "live": act != nil, "title": title, "files": filesDone, "of": fileCount,
        ], cat: "offline")
        guard let act = act else { return }
        let state = DownloadActivityAttributes.ContentState(
            title: title, bytesDone: bytesTotal, bytesTotal: bytesTotal,
            fraction: finished ? 1.0 : 0.0, filesDone: filesDone, fileCount: fileCount,
            finished: finished, failed: failed)
        Task {
            await act.end(ActivityContent(state: state, staleDate: nil),
                          dismissalPolicy: .after(.now + 4))
        }
    }
}
