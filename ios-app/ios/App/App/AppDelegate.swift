import UIKit
import Capacitor

@UIApplicationMain
class AppDelegate: UIResponder, UIApplicationDelegate {

    var window: UIWindow?

    func application(_ application: UIApplication, didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?) -> Bool {
        // NOTHING TO REGISTER HERE. Every other BGTask type must have its handler
        // registered before launch completes, and 18.15.0 dutifully did that with
        // a wildcard — which the system rejects outright ("Registering a wild card
        // handler ... is specifically blocked"). BGContinuedProcessingTask is
        // exempt from the register-before-launch rule, so BundleDownloadManager
        // registers each unique identifier immediately before submitting it.
        return true
    }

    func applicationWillResignActive(_ application: UIApplication) {
        // Sent when the application is about to move from active to inactive state. This can occur for certain types of temporary interruptions (such as an incoming phone call or SMS message) or when the user quits the application and it begins the transition to the background state.
        // Use this method to pause ongoing tasks, disable timers, and invalidate graphics rendering callbacks. Games should use this method to pause the game.
    }

    func applicationDidEnterBackground(_ application: UIApplication) {
        // Use this method to release shared resources, save user data, invalidate timers, and store enough application state information to restore your application to its current state in case it is terminated later.
        // If your application supports background execution, this method is called instead of applicationWillTerminate: when the user quits.
    }

    func applicationWillEnterForeground(_ application: UIApplication) {
        // Called as part of the transition from the background to the active state; here you can undo many of the changes made on entering the background.
    }

    func applicationDidBecomeActive(_ application: UIApplication) {
        // Restart any tasks that were paused (or not yet started) while the application was inactive. If the application was previously in the background, optionally refresh the user interface.
    }

    func applicationWillTerminate(_ application: UIApplication) {
        // Called when the application is about to terminate. Save data if appropriate. See also applicationDidEnterBackground:.
    }

    func application(_ app: UIApplication, open url: URL, options: [UIApplication.OpenURLOptionsKey: Any] = [:]) -> Bool {
        // Called when the app was launched with a url. Feel free to add additional processing here,
        // but if you want the App API to support tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(app, open: url, options: options)
    }

    func application(_ application: UIApplication, continue userActivity: NSUserActivity, restorationHandler: @escaping ([UIUserActivityRestoring]?) -> Void) -> Bool {
        // Called when the app was launched with an activity, including Universal Links.
        // Feel free to add additional processing here, but if you want the App API to support
        // tracking app url opens, make sure to keep this call
        return ApplicationDelegateProxy.shared.application(application, continue: userActivity, restorationHandler: restorationHandler)
    }

    // The bundle downloader uses a *background* URLSession so downloads keep
    // running (and complete) while the app is suspended. When the system finishes
    // those transfers it relaunches us into the background and calls this; we hand
    // the completion handler to the download manager, which invokes it once the
    // session has flushed its delegate events. Without this the session won't
    // deliver its final callbacks and the OS will eventually kill the transfers.
    func application(_ application: UIApplication,
                     handleEventsForBackgroundURLSession identifier: String,
                     completionHandler: @escaping () -> Void) {
        BundleDownloadManager.shared.handleBackgroundEvents(identifier: identifier,
                                                            completionHandler: completionHandler)
    }

}

// MARK: - Scene lifecycle
//
// THE iOS 27 SDK MAKES THIS MANDATORY, AND FATAL. Apps built against SDK 27 that
// do not adopt the UIScene lifecycle are killed the moment UIKit creates their
// first scene — `EXC_BREAKPOINT` inside
// `__UIApplicationEvaluateRuntimeIssueForNoSceneLifecycleAdoption_block_invoke`,
// on the main thread, from `-[UIApplication workspace:didCreateScene:…]`. Against
// SDK 26.5 the same source only got a console warning, so this arrives looking
// exactly like "the new Xcode produces broken binaries": every build crashes
// instantly, nothing is drawn, and the app dies before it can write its first
// diagnostic row. It is not the compiler and not the optimiser — the trap is
// UIKit's, and it is deliberate. Cost most of 2026-09-23 to find; see
// docs/GOTCHAS.md.
//
// Capacitor's app template still ships the pre-scene layout (AppDelegate +
// `UIMainStoryboardFile`), so this is ours to add and will be missing again from
// any freshly generated iOS project.
//
// `UISceneStoryboardFile` in the manifest means UIKit builds the window and the
// root `CAPBridgeViewController` itself, exactly as `UIMainStoryboardFile` used
// to — so the bridge, the plugins and `MainViewController.capacitorDidLoad()` are
// all untouched.
//
// WHAT DOES CHANGE: with scenes adopted, UIKit stops calling the app-delegate
// lifecycle methods (`applicationDidEnterBackground` and friends). Everything in
// this app that cares listens for the NOTIFICATIONS instead
// (`UIApplication.didEnterBackgroundNotification`, `didBecomeActiveNotification`,
// `willTerminateNotification`), and those are still posted under the scene
// lifecycle — which is why the download manager, NativePlayback and the Live
// Activity code need no changes. The one thing that genuinely moves is URL and
// user-activity delivery, which is why they are forwarded below.
//
// `application(_:handleEventsForBackgroundURLSession:completionHandler:)` stays
// on the APP delegate even under scenes — it must not be moved here, or the
// background download session stops getting its flush callback.
class SceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    // A cold launch opened by a URL delivers it here, not through
    // `application(_:open:options:)`. The download Live Activity's deep link
    // (`streamlink://downloads`) is exactly this case.
    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        for ctx in connectionOptions.urlContexts { forward(ctx.url) }
        if let activity = connectionOptions.userActivities.first { forward(activity) }
    }

    // …and a URL opened while already running arrives here.
    func scene(_ scene: UIScene, openURLContexts URLContexts: Set<UIOpenURLContext>) {
        for ctx in URLContexts { forward(ctx.url) }
    }

    func scene(_ scene: UIScene, continue userActivity: NSUserActivity) {
        forward(userActivity)
    }

    /// Hand it to Capacitor's proxy, which is what the app-delegate methods above
    /// did — so plugins keep seeing opens and Universal Links unchanged.
    private func forward(_ url: URL) {
        _ = ApplicationDelegateProxy.shared.application(
            UIApplication.shared, open: url, options: [:])
    }

    private func forward(_ activity: NSUserActivity) {
        _ = ApplicationDelegateProxy.shared.application(
            UIApplication.shared, continue: activity, restorationHandler: { _ in })
    }
}

// MARK: - External display (the glasses)
//
// 18.20.1 ADOPTING SCENES BROKE THE GLASSES, AND THIS IS WHY. Before it, the app
// had no scene manifest and UIKit's compatibility path handed us a
// `…ExternalDisplayNonInteractive` scene unasked (measured, 18.2.0) — NativePlayback
// put a window in it and that is what displaced mirroring. A scene-BASED app gets
// no such gift: UIKit connects the external-display role only if the app asks for
// it. Measured 2026-09-24 on 18.20.1: `extScreen: true, screens: 2` and
// `extScene: false` on every row, so Early and Mirrored both fell straight
// through to plain mirroring and looked identical.
//
// How to ask differs by OS, so both are here:
//   - iOS 27+: `registerSceneAccessory(.externalNonInteractive(...))` on a view
//     controller (MainViewController does it). The SDK is explicit that the role is
//     delivered for a registered accessory.
//   - iOS 16–26: the manifest's `UIWindowSceneSessionRoleExternalDisplayNonInteractive`
//     entry in Info.plist, pointing at the same delegate.
//
// The delegate is deliberately empty. NativePlayback finds the scene through
// `connectedScenes` / `UIScene.willConnectNotification` and builds its own window
// in it when (and only when) it wants the display; until then the scene has no
// window and the display keeps mirroring the phone.
class ExternalDisplaySceneDelegate: UIResponder, UIWindowSceneDelegate {
    var window: UIWindow?

    func scene(_ scene: UIScene, willConnectTo session: UISceneSession,
               options connectionOptions: UIScene.ConnectionOptions) {
        DiagLog.shared.write("ext-scene", [
            "role": session.role.rawValue,
        ], cat: "ext")
    }
}

/// Owns the iOS 27 scene-accessory registration. `enabled` is toggled by
/// NativePlayback per the Early/Mirrored preference: Mirrored must NOT be offered a
/// scene of ours, or the route's own takeover has something to fight with.
enum ExternalDisplayAccessory {
    private static var registration: AnyObject?

    static func register(on vc: UIViewController) {
        guard registration == nil else { return }
        if #available(iOS 27.0, *) {
            let cfg = UISceneConfiguration(name: "External Display",
                                           sessionRole: .windowExternalDisplayNonInteractive)
            cfg.delegateClass = ExternalDisplaySceneDelegate.self
            let reg = vc.registerSceneAccessory(.externalNonInteractive(sceneConfiguration: cfg))
            registration = reg
            DiagLog.shared.write("ext-accessory", [
                "registered": true, "available": reg.isAvailable, "enabled": reg.isEnabled,
            ], cat: "ext")
        }
    }

    static func setEnabled(_ on: Bool) {
        if #available(iOS 27.0, *) {
            guard let reg = registration as? UISceneAccessoryRegistration,
                  reg.isEnabled != on else { return }
            reg.isEnabled = on
            DiagLog.shared.write("ext-accessory", [
                "enabled": on, "available": reg.isAvailable,
            ], cat: "ext")
        }
    }

    /// For diagSnap: "-" before iOS 27 or before registration.
    static var state: String {
        if #available(iOS 27.0, *),
           let reg = registration as? UISceneAccessoryRegistration {
            return (reg.isEnabled ? "on" : "off") + (reg.isAvailable ? "/avail" : "/unavail")
        }
        return "-"
    }
}
