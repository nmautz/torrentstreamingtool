//
//  LiveActivityAttributes.swift
//  StreamLink iOS — Live Activities (shared between the App target and the
//  StreamLinkLiveActivities widget extension).
//
//  Three ActivityKit activities:
//    • DownloadActivityAttributes — background bundle-download progress on the
//      lock screen / Dynamic Island (display only).
//    • TVRemoteAttributes — a remote for whatever is playing on the TV, with
//      interactive pause + volume buttons (driven by the App Intents in
//      TVRemoteIntents.swift). This one controls a REMOTE player (host VLC /
//      YouTube kiosk), so its buttons POST to the host.
//    • PlaybackAttributes — the phone's OWN background playback (NativePlayback's
//      AVPlayer, running under the `audio` background mode). Its buttons drive
//      the local player in-process via PlaybackIntents.swift. This one is
//      accompanied by a real MPNowPlayingInfoCenter item, because unlike the TV
//      remote there IS a local audio session.
//
//  ActivityAttributes requires iOS 16.1; gate every use behind availability.
//

import Foundation
import ActivityKit

@available(iOS 16.1, *)
public struct DownloadActivityAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var title: String
        public var bytesDone: Int64
        public var bytesTotal: Int64
        public var fraction: Double
        public var filesDone: Int
        public var fileCount: Int
        public var finished: Bool
        public var failed: Bool
        /// WHY THE BYTES STOPPED, in the user's words — "" while running.
        /// A download that has been gated off (battery floor, Low Power Mode,
        /// charger-only, cellular) has stopped on purpose, and a progress bar
        /// that simply freezes is indistinguishable from one that has wedged.
        /// This is the only channel that can tell the difference on the lock
        /// screen, so it carries the reason rather than a bare flag.
        public var paused: String

        public init(title: String, bytesDone: Int64, bytesTotal: Int64,
                    fraction: Double, filesDone: Int, fileCount: Int,
                    finished: Bool = false, failed: Bool = false,
                    paused: String = "") {
            self.title = title; self.bytesDone = bytesDone; self.bytesTotal = bytesTotal
            self.fraction = fraction; self.filesDone = filesDone; self.fileCount = fileCount
            self.finished = finished; self.failed = failed; self.paused = paused
        }

        // TOLERANT DECODE, because iOS restores ONGOING LIVE ACTIVITIES AT LAUNCH
        // and their ContentState was serialised by whichever build started them.
        // A previous build's payload has no `paused` key at all, and a synthesised
        // decoder treats a missing key for a non-optional as an error — thrown in
        // the launch path, before the app has drawn anything. Adding a field to a
        // shipped ActivityAttributes is therefore a compatibility change, not an
        // additive one: every new member needs `decodeIfPresent` and a default
        // here, forever. Only `init(from:)` is custom, so `encode(to:)` stays
        // synthesised and always writes the current shape.
        public init(from decoder: Decoder) throws {
            let c = try decoder.container(keyedBy: CodingKeys.self)
            title      = try c.decodeIfPresent(String.self, forKey: .title) ?? ""
            bytesDone  = try c.decodeIfPresent(Int64.self,  forKey: .bytesDone) ?? 0
            bytesTotal = try c.decodeIfPresent(Int64.self,  forKey: .bytesTotal) ?? 0
            fraction   = try c.decodeIfPresent(Double.self, forKey: .fraction) ?? 0
            filesDone  = try c.decodeIfPresent(Int.self,    forKey: .filesDone) ?? 0
            fileCount  = try c.decodeIfPresent(Int.self,    forKey: .fileCount) ?? 0
            finished   = try c.decodeIfPresent(Bool.self,   forKey: .finished) ?? false
            failed     = try c.decodeIfPresent(Bool.self,   forKey: .failed) ?? false
            paused     = try c.decodeIfPresent(String.self, forKey: .paused) ?? ""
        }
    }

    // Static attributes (fixed for the activity's life). Kept minimal — the title
    // lives in ContentState so it can change as the active bundle changes.
    public init() {}
}

/// The phone's own background playback (NativePlayback's AVPlayer).
///
/// `position` + `stamp` are a SAMPLE, not a live clock: the widget seeds
/// SwiftUI's self-advancing `ProgressView(timerInterval:)` / `Text(timerInterval:)`
/// from them, so the lock screen counts up on its own with NO pushes. Do not
/// "fix" the clock by pushing an update every second — ActivityKit budgets
/// updates and will start dropping them (and the Island goes stale). Push only
/// on a real state change plus a slow heartbeat.
@available(iOS 16.1, *)
public struct PlaybackAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var title: String        // episode / movie title
        public var series: String       // small line: "Show · S01E03" (18.24.0), or "" (a movie, or no episode name)
        public var isPaused: Bool
        public var position: Double     // seconds, as sampled at `stamp`
        public var duration: Double     // seconds (0 ⇒ unknown)
        public var stamp: Date          // when `position` was sampled
        public var external: Bool       // video is going out to a monitor / AirPlay
        public var canPrev: Bool
        public var canNext: Bool

        public init(title: String, series: String, isPaused: Bool,
                    position: Double, duration: Double, stamp: Date = Date(),
                    external: Bool = false,
                    canPrev: Bool = false, canNext: Bool = false) {
            self.title = title; self.series = series; self.isPaused = isPaused
            self.position = position; self.duration = duration; self.stamp = stamp
            self.external = external
            self.canPrev = canPrev; self.canNext = canNext
        }

        /// The wall-clock instant the media's 0:00 corresponds to. Feeding this
        /// to a `timerInterval` view makes it track the real playhead.
        public var originDate: Date { stamp.addingTimeInterval(-position) }
    }

    public init() {}
}

@available(iOS 16.1, *)
public struct TVRemoteAttributes: ActivityAttributes {
    public struct ContentState: Codable, Hashable {
        public var title: String
        public var isPaused: Bool
        public var isYouTube: Bool
        public var volume: Int       // VLC: 0-200, YouTube: 0-100 (display only)

        public init(title: String, isPaused: Bool, isYouTube: Bool, volume: Int) {
            self.title = title; self.isPaused = isPaused
            self.isYouTube = isYouTube; self.volume = volume
        }
    }

    public init() {}
}
