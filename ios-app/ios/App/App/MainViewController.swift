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
import Capacitor

class MainViewController: CAPBridgeViewController {
    override open func capacitorDidLoad() {
        bridge?.registerPluginInstance(LocalMediaServer())
    }
}
