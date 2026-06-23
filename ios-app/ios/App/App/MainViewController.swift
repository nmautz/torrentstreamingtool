//
//  MainViewController.swift
//  StreamLink iOS
//
//  Capacitor 8 does NOT auto-discover native plugins by scanning the Obj-C
//  runtime. `CapacitorBridge.registerPlugins()` only registers the classes listed
//  in the generated `capacitor.config.json` `packageClassList`, which `cap sync`
//  populates from installed Capacitor *npm packages* only. An app-local plugin
//  (a Swift class in this target, not a package) is never in that list, so without
//  this it fails at the JS call with: `"<Name>" plugin is not implemented on ios`.
//
//  We register our app-local plugins explicitly via `registerPluginInstance` in
//  `capacitorDidLoad()` (called right after the bridge is created). The storyboard
//  points its root view controller at this subclass.
//

import UIKit
import WebKit
import Capacitor

class MainViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(LocalMediaServer())
        bridge?.registerPluginInstance(BundleDownloader())
        bridge?.registerPluginInstance(OfflineStore())
        injectCapacitorRuntime()
    }

    // The native bridge (native-bridge.js) is injected into every page including
    // the remote dashboard, but in Capacitor 8 it does NOT create the ergonomic
    // `Capacitor.registerPlugin` / `Capacitor.Plugins` API — that lives in
    // @capacitor/core. The dashboard (static/index.html, served by the host) has
    // no bundler and can't load capacitor.js itself, so the M2 web glue's `isApp`
    // detection would be false and all offline features inert. We inject the
    // vendored core runtime at document-start for every navigation, so
    // `Capacitor.Plugins.{LocalMediaServer,BundleDownloader}` resolve on the
    // remote origin too. (Idempotent on the local `www/` pages, which also load
    // capacitor.js via a <script>.) See docs/GOTCHAS.md "iOS client app".
    private func injectCapacitorRuntime() {
        guard let ucc = bridge?.webView?.configuration.userContentController,
              let url = Bundle.main.url(forResource: "capacitor", withExtension: "js", subdirectory: "public"),
              let src = try? String(contentsOf: url, encoding: .utf8) else { return }
        let script = WKUserScript(source: src, injectionTime: .atDocumentStart, forMainFrameOnly: true)
        ucc.addUserScript(script)
    }
}
