//
//  AppShell.swift
//  StreamLink iOS — small app-level conveniences the page can't reach itself:
//  haptics, the home-screen quick actions and the player's orientation lock.
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
//  ORIENTATION LOCK (20.3.0)
//  The player's lock button used to be all CSS: the page turned itself 90° inside
//  an interface that was still portrait, so the status bar, the clock and the safe
//  areas stayed portrait on top of it. And with iOS's own rotation lock on, the
//  app could not leave portrait at all. Only the app can ask iOS to turn the
//  interface, so the page asks us. See OrientationLock below.
//
//  JS surface (Capacitor plugin "AppShell"):
//    haptic({ kind })     -> {}   kind: light | medium | selection | success | warning
//    takeQuickAction()    -> { action }   "" when none; clears the inbox
//    setOrientationLock({ on }) -> { on, side }   side: left | right | either | ""
//                            (20.3.0). An older app rejects, and the page falls
//                            back to its CSS turn.
//    info()               -> { version, build }   CFBundleShortVersionString /
//                            CFBundleVersion. build-ipa.sh stamps the dashboard
//                            badge into both; a plain Xcode build says "1.0".
//                            The page's out-of-date check reads it (19.6.0).
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

// MARK: - Orientation lock

/// Holds the interface in ONE landscape side while the player's lock is on.
///
/// `MainViewController.supportedInterfaceOrientations` answers with `mask`, and
/// that is the whole mechanism: UIKit will not turn the interface to anything
/// outside it, and `requestGeometryUpdate` turns it there now, whatever the
/// phone's own rotation lock says.
///
/// One side, not both, because the lock is for watching lying down: on your side
/// the phone is upright as far as gravity knows, and a lock that still flips
/// between the two landscape sides turns the picture over when you shift. Locked
/// while already landscape, that side is pinned. Locked while upright, there is
/// no side to pin yet, so both are allowed until the phone is first turned to
/// one, and then that one is pinned.
final class OrientationLock {
    static let shared = OrientationLock()

    /// nil while unlocked: the view controller answers with Info.plist's list.
    private(set) var mask: UIInterfaceOrientationMask?
    private var awaitingSide = false
    private weak var host: UIViewController?

    private init() {
        UIDevice.current.beginGeneratingDeviceOrientationNotifications()
        NotificationCenter.default.addObserver(
            forName: UIDevice.orientationDidChangeNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.deviceTurned() }
    }

    var side: String {
        guard let m = mask else { return "" }
        if m == .landscapeLeft { return "left" }
        if m == .landscapeRight { return "right" }
        return "either"
    }

    /// Main thread only.
    func set(_ on: Bool, in vc: UIViewController?) {
        if let vc = vc { host = vc }
        if !on {
            guard mask != nil else { return }
            mask = nil
            awaitingSide = false
            // Hand the interface back to where the phone is. Under iOS's rotation
            // lock the device reads as portrait, which is where it belongs.
            apply(turnTo: Self.side(of: UIDevice.current.orientation, landscapeOnly: false))
            DiagLog.shared.write("orient-lock", ["on": false], cat: "app")
            return
        }
        let now = host?.view.window?.windowScene?.interfaceOrientation ?? .unknown
        if now == .landscapeLeft || now == .landscapeRight {
            mask = now == .landscapeLeft ? .landscapeLeft : .landscapeRight
            awaitingSide = false
        } else if let held = Self.side(of: UIDevice.current.orientation, landscapeOnly: true) {
            mask = held
            awaitingSide = false
        } else {
            mask = .landscape
            awaitingSide = true
        }
        apply(turnTo: mask)
        DiagLog.shared.write("orient-lock", ["on": true, "side": side], cat: "app")
    }

    private func deviceTurned() {
        guard awaitingSide,
              let held = Self.side(of: UIDevice.current.orientation, landscapeOnly: true)
        else { return }
        awaitingSide = false
        mask = held
        apply(turnTo: held)
        DiagLog.shared.write("orient-lock", ["on": true, "side": side, "pinned": true], cat: "app")
    }

    /// The interface side that shows upright with the DEVICE held this way. The
    /// two enums name the landscape sides oppositely: the device turned left is
    /// the interface turned right.
    private static func side(of device: UIDeviceOrientation,
                             landscapeOnly: Bool) -> UIInterfaceOrientationMask? {
        switch device {
        case .landscapeLeft:  return .landscapeRight
        case .landscapeRight: return .landscapeLeft
        case .portrait:       return landscapeOnly ? nil : .portrait
        default:              return nil   // face up, face down, upside down, unknown
        }
    }

    private func apply(turnTo target: UIInterfaceOrientationMask?) {
        guard let vc = host else { return }
        if #available(iOS 16.0, *) {
            vc.setNeedsUpdateOfSupportedInterfaceOrientations()
            guard let target = target, let scene = vc.view.window?.windowScene else { return }
            scene.requestGeometryUpdate(.iOS(interfaceOrientations: target)) { err in
                DiagLog.shared.write("orient-lock-refused",
                                     ["err": err.localizedDescription], cat: "app")
            }
        } else {
            // iOS 15 has no way to ask for a turn; setting the device's own
            // orientation is the long-standing stand-in.
            if let target = target {
                let o: UIInterfaceOrientation =
                    target == .portrait ? .portrait
                    : target == .landscapeLeft ? .landscapeLeft : .landscapeRight
                UIDevice.current.setValue(o.rawValue, forKey: "orientation")
            }
            UIViewController.attemptRotationToDeviceOrientation()
        }
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
        CAPPluginMethod(name: "info",            returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setOrientationLock", returnType: CAPPluginReturnPromise),
    ]

    private var loadWatch: NSKeyValueObservation?

    public override func load() {
        NotificationCenter.default.addObserver(
            forName: QuickActions.arrived, object: nil, queue: .main
        ) { [weak self] note in
            let action = (note.userInfo?["action"] as? String) ?? ""
            self?.notifyListeners("quickAction", data: ["action": action])
        }
        // The lock belongs to the page that asked for it. A page that is replaced
        // (a reload, the offline snapshot, the grey-screen recovery) never gets to
        // release it, and the next page would be stuck sideways with no button
        // lit. A new document loading is the one moment that covers them all.
        loadWatch = bridge?.webView?.observe(\.isLoading, options: [.new]) { _, change in
            guard change.newValue == true else { return }
            DispatchQueue.main.async { OrientationLock.shared.set(false, in: nil) }
        }
    }

    @objc func setOrientationLock(_ call: CAPPluginCall) {
        let on = call.getBool("on") ?? false
        DispatchQueue.main.async { [weak self] in
            OrientationLock.shared.set(on, in: self?.bridge?.viewController)
            call.resolve(["on": OrientationLock.shared.mask != nil,
                          "side": OrientationLock.shared.side])
        }
    }

    @objc func haptic(_ call: CAPPluginCall) {
        Haptics.fire(call.getString("kind") ?? "light")
        call.resolve()
    }

    @objc func info(_ call: CAPPluginCall) {
        let d = Bundle.main.infoDictionary ?? [:]
        call.resolve([
            "version": d["CFBundleShortVersionString"] as? String ?? "",
            "build": d["CFBundleVersion"] as? String ?? "",
        ])
    }

    @objc func takeQuickAction(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            call.resolve(["action": QuickActions.shared.take()])
        }
    }
}
