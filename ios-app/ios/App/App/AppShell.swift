//
//  AppShell.swift
//  StreamLink iOS — small app-level conveniences the page can't reach itself:
//  haptics and the home-screen quick actions.
//
//  HAPTICS
//  `navigator.vibrate` does not exist on iOS, so the page asks us. Every call is
//  fire-and-forget; the page throttles its own calls (see _hap in
//  static/index.html). Download completion buzzes from here directly, because
//  the downloader already knows and the page may be downloads.html, the offline
//  snapshot or the host dashboard.
//
//  QUICK ACTIONS (long-press the app icon)
//  Declared statically in Info.plist (UIApplicationShortcutItems), delivered to
//  SceneDelegate. The action has to outlive a page load: a cold launch lands on
//  the connect shell (www/index.html), which then navigates to the host, and only
//  THAT page can act. So the action waits here in an inbox the page empties on
//  boot (`takeQuickAction`), and a warm launch also fires `quickAction` so a page
//  that is already up doesn't have to poll. Taking clears it — the event is only a
//  doorbell, so a page that gets both can't act twice.
//
//  JS surface (Capacitor plugin "AppShell"):
//    haptic({ kind })     -> {}   kind: light | medium | selection | success | warning
//    takeQuickAction()    -> { action }   "" when none; clears the inbox
//  Events: quickAction { action }
//

import Foundation
import Capacitor
import UIKit

// MARK: - Haptics

enum Haptics {
    private static let light = UIImpactFeedbackGenerator(style: .light)
    private static let medium = UIImpactFeedbackGenerator(style: .medium)
    private static let selection = UISelectionFeedbackGenerator()
    private static let notify = UINotificationFeedbackGenerator()
    private static var lastDownloadBuzz = Date.distantPast

    static func fire(_ kind: String) {
        DispatchQueue.main.async {
            // A backgrounded app's haptics are dropped by the system anyway, and
            // a buzz with the phone in a pocket is not feedback for anything.
            guard UIApplication.shared.applicationState == .active else { return }
            switch kind {
            case "medium":    medium.impactOccurred()
            case "selection": selection.selectionChanged()
            case "success":   notify.notificationOccurred(.success)
            case "warning":   notify.notificationOccurred(.warning)
            default:          light.impactOccurred()
            }
        }
    }

    /// One buzz per burst. A season finishing is dozens of bundles landing
    /// within seconds of each other, and that should feel like one event.
    static func downloadFinished() {
        DispatchQueue.main.async {
            let now = Date()
            guard now.timeIntervalSince(lastDownloadBuzz) > 3 else { return }
            lastDownloadBuzz = now
            fire("success")
        }
    }
}

// MARK: - Quick actions

final class QuickActions {
    static let shared = QuickActions()
    static let arrived = Notification.Name("StreamLinkQuickAction")

    private var pending = ""

    /// `com.streamlink.client.<action>` → `<action>`. The type strings live in
    /// Info.plist; the page only ever sees the short name.
    func receive(_ item: UIApplicationShortcutItem) {
        let action = item.type.components(separatedBy: ".").last ?? item.type
        DispatchQueue.main.async {
            self.pending = action
            DiagLog.shared.write("quick-action", ["action": action], cat: "app")
            NotificationCenter.default.post(name: Self.arrived, object: nil,
                                            userInfo: ["action": action])
        }
    }

    func take() -> String {
        let a = pending
        pending = ""
        return a
    }
}

// MARK: - Plugin

@objc(AppShell)
public class AppShell: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "AppShell"
    public let jsName = "AppShell"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "haptic",          returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "takeQuickAction", returnType: CAPPluginReturnPromise),
    ]

    public override func load() {
        NotificationCenter.default.addObserver(
            forName: QuickActions.arrived, object: nil, queue: .main
        ) { [weak self] note in
            let action = (note.userInfo?["action"] as? String) ?? ""
            self?.notifyListeners("quickAction", data: ["action": action])
        }
    }

    @objc func haptic(_ call: CAPPluginCall) {
        Haptics.fire(call.getString("kind") ?? "light")
        call.resolve()
    }

    @objc func takeQuickAction(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            call.resolve(["action": QuickActions.shared.take()])
        }
    }
}
