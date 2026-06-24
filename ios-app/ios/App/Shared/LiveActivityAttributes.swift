//
//  LiveActivityAttributes.swift
//  StreamLink iOS — Live Activities (shared between the App target and the
//  StreamLinkLiveActivities widget extension).
//
//  Two ActivityKit activities:
//    • DownloadActivityAttributes — background bundle-download progress on the
//      lock screen / Dynamic Island (display only).
//    • TVRemoteAttributes — a remote for whatever is playing on the TV, with
//      interactive pause + volume buttons (driven by the App Intents in
//      TVRemoteIntents.swift). The phone has no local audio session, so this is
//      a Live Activity, not an MPNowPlayingInfoCenter now-playing item.
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

        public init(title: String, bytesDone: Int64, bytesTotal: Int64,
                    fraction: Double, filesDone: Int, fileCount: Int,
                    finished: Bool = false, failed: Bool = false) {
            self.title = title; self.bytesDone = bytesDone; self.bytesTotal = bytesTotal
            self.fraction = fraction; self.filesDone = filesDone; self.fileCount = fileCount
            self.finished = finished; self.failed = failed
        }
    }

    // Static attributes (fixed for the activity's life). Kept minimal — the title
    // lives in ContentState so it can change as the active bundle changes.
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
