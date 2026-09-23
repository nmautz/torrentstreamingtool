//
//  DownloadActivityWidget.swift
//  StreamLinkLiveActivities — lock-screen + Dynamic Island UI for an in-flight
//  bundle download. Display only; the data is pushed from DownloadLiveActivity
//  in the app target as the background URLSession makes progress.
//

import WidgetKit
import SwiftUI
import ActivityKit

@available(iOS 16.1, *)
struct DownloadActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: DownloadActivityAttributes.self) { context in
            // Lock screen / banner.
            DownloadLockScreenView(state: context.state)
                .padding()
                .activityBackgroundTint(Color.black.opacity(0.85))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Image(systemName: statusIcon(context.state))
                        .foregroundColor(statusColor(context.state))
                        .font(.title2)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text("\(context.state.filesDone)/\(context.state.fileCount)")
                        .font(.caption).monospacedDigit().foregroundColor(.secondary)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.paused.isEmpty ? context.state.title
                                                          : context.state.paused)
                            .font(.caption).lineLimit(1)
                        ProgressView(value: clampedFraction(context.state))
                            .tint(statusColor(context.state))
                    }
                }
            } compactLeading: {
                Image(systemName: context.state.paused.isEmpty ? "arrow.down" : "pause.fill")
                    .foregroundColor(statusColor(context.state))
            } compactTrailing: {
                Text(percentText(context.state))
                    .font(.caption2).monospacedDigit()
            } minimal: {
                Image(systemName: statusIcon(context.state))
                    .foregroundColor(statusColor(context.state))
            }
            .widgetURL(URL(string: "streamlink://downloads"))
        }
    }
}

@available(iOS 16.1, *)
private struct DownloadLockScreenView: View {
    let state: DownloadActivityAttributes.ContentState
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Image(systemName: statusIcon(state))
                    .foregroundColor(statusColor(state))
                Text(state.failed ? "DOWNLOAD FAILED"
                   : state.finished ? "DOWNLOAD COMPLETE"
                   : state.paused.isEmpty ? "DOWNLOADING"
                   : "DOWNLOADS PAUSED")
                    .font(.caption).bold()
                Spacer()
                Text("\(state.filesDone)/\(state.fileCount) files")
                    .font(.caption2).foregroundColor(.secondary)
            }
            Text(state.title).font(.subheadline).bold().lineLimit(1)
            // A frozen bar with no explanation reads as a bug. Say which switch
            // stopped it, so the fix is one tap away instead of a support round-trip.
            if !state.paused.isEmpty {
                Text(state.paused).font(.caption2).foregroundColor(.orange).lineLimit(2)
            }
            ProgressView(value: clampedFraction(state))
                .tint(statusColor(state))
        }
    }
}

/// Paused is a THIRD state, not a flavour of running. It sorts below failure
/// (a failed download is over; a paused one is waiting) and above success.
@available(iOS 16.1, *)
private func statusIcon(_ s: DownloadActivityAttributes.ContentState) -> String {
    if s.failed { return "exclamationmark.triangle.fill" }
    if s.finished { return "checkmark.circle.fill" }
    if !s.paused.isEmpty { return "pause.circle.fill" }
    return "arrow.down.circle.fill"
}

@available(iOS 16.1, *)
private func statusColor(_ s: DownloadActivityAttributes.ContentState) -> Color {
    if s.failed { return .red }
    if !s.paused.isEmpty && !s.finished { return .orange }
    return .green
}

@available(iOS 16.1, *)
private func clampedFraction(_ s: DownloadActivityAttributes.ContentState) -> Double {
    if s.finished { return 1.0 }
    return min(1.0, max(0.0, s.fraction))
}

@available(iOS 16.1, *)
private func percentText(_ s: DownloadActivityAttributes.ContentState) -> String {
    "\(Int(clampedFraction(s) * 100))%"
}
