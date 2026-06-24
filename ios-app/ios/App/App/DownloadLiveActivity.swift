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

    @available(iOS 16.2, *)
    private var activity: Activity<DownloadActivityAttributes>? {
        get { _activity as? Activity<DownloadActivityAttributes> }
        set { _activity = newValue }
    }

    /// Push a fresh snapshot. Starts the activity if none is running.
    /// `force` bypasses the throttle (use on per-file completion / terminal states).
    func sync(title: String, bytesDone: Int64, bytesTotal: Int64, fraction: Double,
              filesDone: Int, fileCount: Int, finished: Bool = false,
              failed: Bool = false, force: Bool = false) {
        guard #available(iOS 16.2, *) else { return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }

        lock.lock()
        if !force {
            let now = Date()
            if now.timeIntervalSince(lastUpdate) < minInterval { lock.unlock(); return }
            lastUpdate = now
        } else {
            lastUpdate = Date()
        }
        let existing = activity
        lock.unlock()

        let state = DownloadActivityAttributes.ContentState(
            title: title, bytesDone: bytesDone, bytesTotal: bytesTotal,
            fraction: fraction, filesDone: filesDone, fileCount: fileCount,
            finished: finished, failed: failed)

        if let act = existing {
            Task { await act.update(ActivityContent(state: state, staleDate: nil)) }
        } else if !finished && !failed {
            do {
                let act = try Activity.request(
                    attributes: DownloadActivityAttributes(),
                    content: ActivityContent(state: state, staleDate: nil),
                    pushType: nil)
                lock.lock(); activity = act; lock.unlock()
            } catch {
                // Activity start can throw (over the system limit, disabled, etc.) —
                // downloads continue regardless.
            }
        }
    }

    /// End the activity, leaving a short-lived terminal frame on screen.
    func end(title: String, finished: Bool, failed: Bool,
             filesDone: Int, fileCount: Int, bytesTotal: Int64) {
        guard #available(iOS 16.2, *) else { return }
        lock.lock(); let act = activity; activity = nil; lock.unlock()
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
