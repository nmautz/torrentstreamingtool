//
//  TVRemoteWidget.swift
//  StreamLinkLiveActivities — lock-screen + Dynamic Island remote for whatever
//  is playing on the TV. The buttons fire LiveActivityIntents (TVRemoteIntents)
//  that POST to the host. Interactive buttons require iOS 17; on 16.x the same
//  activity still shows (display only — buttons are inert below 17).
//

import WidgetKit
import SwiftUI
import ActivityKit
import AppIntents

@available(iOS 16.1, *)
struct TVRemoteWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: TVRemoteAttributes.self) { context in
            TVRemoteLockScreenView(state: context.state)
                .padding()
                .activityBackgroundTint(Color.black.opacity(0.85))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    volButton(systemName: "speaker.wave.1.fill", intent: TVVolumeDownIntent())
                }
                DynamicIslandExpandedRegion(.trailing) {
                    volButton(systemName: "speaker.wave.3.fill", intent: TVVolumeUpIntent())
                }
                DynamicIslandExpandedRegion(.center) {
                    playPauseButton(isPaused: context.state.isPaused)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    HStack(spacing: 6) {
                        Image(systemName: context.state.isYouTube ? "play.rectangle.fill" : "tv.fill")
                            .foregroundColor(.secondary)
                        Text(context.state.title).font(.caption).lineLimit(1)
                    }
                }
            } compactLeading: {
                Image(systemName: context.state.isYouTube ? "play.rectangle.fill" : "tv.fill")
                    .foregroundColor(.green)
            } compactTrailing: {
                playPauseButton(isPaused: context.state.isPaused, compact: true)
            } minimal: {
                Image(systemName: context.state.isPaused ? "pause.fill" : "play.fill")
                    .foregroundColor(.green)
            }
        }
    }

    @ViewBuilder
    private func playPauseButton(isPaused: Bool, compact: Bool = false) -> some View {
        if #available(iOS 17.0, *) {
            Button(intent: TVPlayPauseIntent()) {
                Image(systemName: isPaused ? "play.fill" : "pause.fill")
                    .font(compact ? .body : .title2)
            }
            .buttonStyle(.plain)
            .tint(.white)
        } else {
            Image(systemName: isPaused ? "play.fill" : "pause.fill")
                .font(compact ? .body : .title2)
        }
    }

    @ViewBuilder
    private func volButton(systemName: String, intent: some LiveActivityIntent) -> some View {
        if #available(iOS 17.0, *) {
            Button(intent: intent) {
                Image(systemName: systemName).font(.title3)
            }
            .buttonStyle(.plain)
            .tint(.white)
        } else {
            Image(systemName: systemName).font(.title3)
        }
    }
}

@available(iOS 16.1, *)
private struct TVRemoteLockScreenView: View {
    let state: TVRemoteAttributes.ContentState
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 6) {
                Image(systemName: state.isYouTube ? "play.rectangle.fill" : "tv.fill")
                    .foregroundColor(.green)
                Text("PLAYING ON TV").font(.caption).bold().foregroundColor(.secondary)
                Spacer()
            }
            Text(state.title).font(.subheadline).bold().lineLimit(1)
            HStack(spacing: 24) {
                Spacer()
                remoteButton(systemName: "speaker.wave.1.fill", intent: TVVolumeDownIntent())
                remoteButton(systemName: state.isPaused ? "play.fill" : "pause.fill",
                             intent: TVPlayPauseIntent(), large: true)
                remoteButton(systemName: "speaker.wave.3.fill", intent: TVVolumeUpIntent())
                Spacer()
            }
        }
    }

    @ViewBuilder
    private func remoteButton(systemName: String, intent: some LiveActivityIntent, large: Bool = false) -> some View {
        if #available(iOS 17.0, *) {
            Button(intent: intent) {
                Image(systemName: systemName).font(large ? .title : .title2)
            }
            .buttonStyle(.plain)
            .tint(.white)
        } else {
            Image(systemName: systemName).font(large ? .title : .title2)
        }
    }
}
