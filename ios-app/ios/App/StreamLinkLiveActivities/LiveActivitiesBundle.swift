//
//  LiveActivitiesBundle.swift
//  StreamLinkLiveActivities — widget extension entry point.
//
//  Hosts all three Live Activity widgets: download progress (display only), the
//  TV remote (interactive, drives the host's player) and local background
//  playback (interactive, drives the phone's own AVPlayer). The whole extension
//  is iOS 16.1+; the individual configurations gate themselves further where
//  needed.
//

import WidgetKit
import SwiftUI

@main
struct StreamLinkLiveActivitiesBundle: WidgetBundle {
    // The extension's minimum deployment target is iOS 17, so the 16.1-gated
    // widgets below are always available here.
    var body: some Widget {
        DownloadActivityWidget()
        TVRemoteWidget()
        PlaybackWidget()
    }
}
