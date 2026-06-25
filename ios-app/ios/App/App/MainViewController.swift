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
        bridge?.registerPluginInstance(TVRemote())
        injectCapacitorRuntime()
        injectViewportLock()
    }

    // Lock the viewport so the WebView never zooms. iOS auto-zooms when an input
    // with font-size < 16px gets focus (the PIN pad, the search box, the profile/
    // episode fields…), and because the host dashboard's viewport doesn't pin
    // `maximum-scale`, the zoom-in sticks with no reliable way to zoom back out —
    // users were double-tapping to escape. WKWebView (unlike mobile Safari) honors
    // `user-scalable=no`, so forcing it kills both the focus auto-zoom and pinch
    // zoom. Injected natively (not in the page markup) so it applies to the
    // remote-served dashboard too, and only inside the app — browser users keep
    // pinch-to-zoom. Runs at document-end and on every navigation; re-asserts after
    // load in case the page (re)writes its own viewport meta.
    private func injectViewportLock() {
        guard let ucc = bridge?.webView?.configuration.userContentController else { return }
        let js = """
        (function(){
          function lock(){
            var c='width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover';
            var m=document.querySelector('meta[name=viewport]');
            if(!m){ m=document.createElement('meta'); m.setAttribute('name','viewport'); (document.head||document.documentElement).appendChild(m); }
            if(m.getAttribute('content')!==c) m.setAttribute('content',c);
          }
          lock();
          document.addEventListener('DOMContentLoaded', lock);
        })();
        """
        ucc.addUserScript(WKUserScript(source: js, injectionTime: .atDocumentEnd, forMainFrameOnly: true))
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
