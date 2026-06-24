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
                    Image(systemName: context.state.failed ? "exclamationmark.triangle.fill"
                                    : context.state.finished ? "checkmark.circle.fill"
                                    : "arrow.down.circle.fill")
                        .foregroundColor(context.state.failed ? .red : .green)
                        .font(.title2)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    Text("\(context.state.filesDone)/\(context.state.fileCount)")
                        .font(.caption).monospacedDigit().foregroundColor(.secondary)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(context.state.title).font(.caption).lineLimit(1)
                        ProgressView(value: clampedFraction(context.state))
                            .tint(context.state.failed ? .red : .green)
                    }
                }
            } compactLeading: {
                Image(systemName: "arrow.down")
                    .foregroundColor(.green)
            } compactTrailing: {
                Text(percentText(context.state))
                    .font(.caption2).monospacedDigit()
            } minimal: {
                Image(systemName: "arrow.down.circle.fill")
                    .foregroundColor(.green)
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
                Image(systemName: state.failed ? "exclamationmark.triangle.fill"
                                : state.finished ? "checkmark.circle.fill"
                                : "arrow.down.circle.fill")
                    .foregroundColor(state.failed ? .red : .green)
                Text(state.failed ? "DOWNLOAD FAILED"
                   : state.finished ? "DOWNLOAD COMPLETE"
                   : "DOWNLOADING")
                    .font(.caption).bold()
                Spacer()
                Text("\(state.filesDone)/\(state.fileCount) files")
                    .font(.caption2).foregroundColor(.secondary)
            }
            Text(state.title).font(.subheadline).bold().lineLimit(1)
            ProgressView(value: clampedFraction(state))
                .tint(state.failed ? .red : .green)
        }
    }
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
