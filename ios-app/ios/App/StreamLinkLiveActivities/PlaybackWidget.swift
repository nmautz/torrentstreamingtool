//
//  PlaybackWidget.swift
//  StreamLinkLiveActivities — lock-screen + Dynamic Island transport for the
//  phone's OWN background playback (NativePlayback's AVPlayer).
//
//  Sibling of TVRemoteWidget, and deliberately the same shape — but its buttons
//  fire PlaybackIntents, which drive the LOCAL player in-process rather than
//  POSTing to the host. Interactive buttons require iOS 17; on 16.x the activity
//  still shows as a display-only now-playing card.
//
//  THE CLOCK IS SELF-ADVANCING. `ProgressView(timerInterval:)` and
//  `Text(timerInterval:)` count on their own from the origin date carried in the
//  state, so the lock screen stays live with NO updates being pushed. Do not
//  replace these with a value-based ProgressView fed by a 1 Hz push — that
//  blows the ActivityKit update budget and ends up freezing the display.
//  When paused there is no running clock, so we render a static value instead.
//

import WidgetKit
import SwiftUI
import ActivityKit
import AppIntents

@available(iOS 16.1, *)
struct PlaybackWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: PlaybackAttributes.self) { context in
            PlaybackLockScreenView(state: context.state)
                .padding()
                .activityBackgroundTint(Color.black.opacity(0.85))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    iconButton("gobackward.15", intent: PlaybackSkipBackIntent())
                }
                DynamicIslandExpandedRegion(.trailing) {
                    iconButton("goforward.15", intent: PlaybackSkipForwardIntent())
                }
                DynamicIslandExpandedRegion(.center) {
                    playPauseButton(isPaused: context.state.isPaused)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.title).font(.caption).lineLimit(1)
                        PlaybackProgressBar(state: context.state)
                    }
                }
            } compactLeading: {
                Image(systemName: context.state.external ? "tv.fill" : "play.circle.fill")
                    .foregroundColor(.green)
            } compactTrailing: {
                PlaybackClockText(state: context.state)
                    .font(.caption2)
                    .monospacedDigit()
                    .frame(maxWidth: 44)
            } minimal: {
                Image(systemName: context.state.isPaused ? "pause.fill" : "play.fill")
                    .foregroundColor(.green)
            }
        }
    }

    @ViewBuilder
    private func playPauseButton(isPaused: Bool) -> some View {
        if #available(iOS 17.0, *) {
            Button(intent: PlaybackPlayPauseIntent()) {
                Image(systemName: isPaused ? "play.fill" : "pause.fill").font(.title2)
            }
            .buttonStyle(.plain)
            .tint(.white)
        } else {
            Image(systemName: isPaused ? "play.fill" : "pause.fill").font(.title2)
        }
    }

    @ViewBuilder
    private func iconButton(_ systemName: String, intent: some LiveActivityIntent) -> some View {
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

// MARK: - Lock screen

@available(iOS 16.1, *)
private struct PlaybackLockScreenView: View {
    let state: PlaybackAttributes.ContentState

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 6) {
                Image(systemName: state.external ? "tv.fill" : "iphone")
                    .foregroundColor(.green)
                Text(headline).font(.caption).bold().foregroundColor(.secondary)
                Spacer()
                PlaybackClockText(state: state)
                    .font(.caption2).monospacedDigit().foregroundColor(.secondary)
            }

            if !state.series.isEmpty {
                Text(state.series).font(.caption2).foregroundColor(.secondary).lineLimit(1)
            }
            Text(state.title).font(.subheadline).bold().lineLimit(1)

            PlaybackProgressBar(state: state)

            HStack(spacing: 20) {
                Spacer()
                if state.canPrev { button("backward.end.fill", PlaybackPrevIntent()) }
                button("gobackward.15", PlaybackSkipBackIntent())
                button(state.isPaused ? "play.fill" : "pause.fill",
                       PlaybackPlayPauseIntent(), large: true)
                button("goforward.15", PlaybackSkipForwardIntent())
                if state.canNext { button("forward.end.fill", PlaybackNextIntent()) }
                Spacer()
            }
        }
    }

    private var headline: String {
        if state.external { return "PLAYING ON DISPLAY" }
        return "PLAYING ON THIS DEVICE"
    }

    @ViewBuilder
    private func button(_ systemName: String, _ intent: some LiveActivityIntent,
                        large: Bool = false) -> some View {
        if #available(iOS 17.0, *) {
            Button(intent: intent) {
                Image(systemName: systemName).font(large ? .title : .title3)
            }
            .buttonStyle(.plain)
            .tint(.white)
        } else {
            Image(systemName: systemName).font(large ? .title : .title3)
        }
    }
}

// MARK: - Self-advancing clock pieces

/// Elapsed-time text. While playing it is a live `timerInterval` view that ticks
/// on its own; paused, it is a static rendering of the sampled position.
@available(iOS 16.1, *)
private struct PlaybackClockText: View {
    let state: PlaybackAttributes.ContentState

    var body: some View {
        if state.isPaused {
            Text(fmt(state.position))
        } else {
            Text(timerInterval: state.originDate...Date.distantFuture,
                 countsDown: false)
        }
    }

    private func fmt(_ t: Double) -> String {
        let s = max(Int(t.rounded()), 0)
        let h = s / 3600, m = (s % 3600) / 60, sec = s % 60
        return h > 0 ? String(format: "%d:%02d:%02d", h, m, sec)
                     : String(format: "%d:%02d", m, sec)
    }
}

/// Progress bar. Same trick: a `timerInterval` ProgressView advances with no
/// pushes; paused (or unknown duration) falls back to a static value.
@available(iOS 16.1, *)
private struct PlaybackProgressBar: View {
    let state: PlaybackAttributes.ContentState

    var body: some View {
        if state.duration > 1 && !state.isPaused {
            ProgressView(timerInterval: state.originDate...state.originDate
                            .addingTimeInterval(state.duration),
                         countsDown: false) {
                EmptyView()
            } currentValueLabel: {
                EmptyView()
            }
            .progressViewStyle(.linear)
            .tint(.green)
        } else {
            ProgressView(value: fraction)
                .progressViewStyle(.linear)
                .tint(.green)
        }
    }

    private var fraction: Double {
        guard state.duration > 0 else { return 0 }
        return min(max(state.position / state.duration, 0), 1)
    }
}
