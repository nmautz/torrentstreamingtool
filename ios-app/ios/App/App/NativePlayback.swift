//
//  NativePlayback.swift
//  StreamLink iOS — background + external-display playback.
//
//  WHY THIS EXISTS
//  The on-device player is a <video> in the WKWebView (hls.js over
//  ManagedMediaSource). WebKit PAUSES a video-bearing <video> the instant the
//  app backgrounds, and no amount of audio-session configuration changes that —
//  so locking the phone kills playback. The only way to keep going is a native
//  AVPlayer under the `audio` background mode (Info.plist UIBackgroundModes).
//
//  It also solves the external monitor: screen MIRRORING dies at lock (the
//  monitor gets the lock screen). The fix is to stop mirroring and give the
//  player a surface of our own — a UIWindow on the external UIScreen carrying an
//  AVPlayerLayer. Built on the way out, destroyed on the way back in, so the
//  foreground/TV-Mode story (which NEEDS mirroring) is untouched. See
//  "External display surface" below for why the routing flags alone are not
//  enough.
//
//  THE RELIEF-PITCHER MODEL
//  The web player stays primary. This plugin is armed continuously with the
//  current playback state but starts NOTHING until the app actually backgrounds;
//  on return it hands the playhead back and tears down. Two engines never play
//  at once — handoff-in explicitly pauses the web element first.
//
//  WHY PRE-ARM RATHER THAN "HAND OFF ON visibilitychange"
//  That JS event is not guaranteed to run — nor to finish its bridge round-trip —
//  before the process is suspended. So the NATIVE side owns the transition,
//  triggered from didEnterBackgroundNotification, and reconstructs the playhead
//  by extrapolating from the last armed sample with wall-clock elapsed time. JS
//  running at the critical moment is never required.
//
//  JS surface (Capacitor plugin "NativePlayback"):
//    arm({...})        -> {}      full state push (idempotent; ~on every change)
//    tick({position,paused,duration}) -> {}   1 Hz position-only refresh
//    disarm()          -> {}      playback stopped; never take over
//    takeover()        -> {started}   hand off NOW (explicit button / spike test)
//    resume()          -> { active, position, paused, ended, itemId, filePath }
//    state()           -> { active, native, position, paused, external, extWindow,
//                            tvMode }
//    setTvMode({on})   -> { on }
//    displays()        -> { connected, name, externalPlayback, ownWindow }
//  Events: nativeStarted, nativeEnded, nativeAdvanced, displayChanged
//
//  See docs/STREAMING.md and docs/GOTCHAS.md ("iOS background playback").
//

import Foundation
import Capacitor
import AVFoundation
import MediaPlayer
import UIKit

// MARK: - Armed state

/// What the web player last told us it was doing. Pure data — arming does no
/// AVFoundation work at all, so it's cheap enough to push on every tick.
struct ArmedPlayback {
    var active = false
    var url: URL?
    var position: Double = 0
    var duration: Double = 0
    var paused = false
    var rate: Double = 1
    var title = ""
    var series = ""
    var audioName: String?      // e.g. "audio_0" — the HLS rendition NAME
    var audioLang: String?      // ISO language, the fallback matcher
    var subIndex: Int = -1      // -1 = off
    var subLang: String?
    var itemId = ""
    var filePath = ""
    var profileId = ""
    var serverUrl = ""
    var token = ""
    var canPrev = false
    var canNext = false
    var nextUrl: URL?
    var nextTitle = ""
    var nextFilePath = ""
    var nextItemId = ""
    var handoffEnabled = true
    /// How to reach a wired monitor once locked: "window" (our own UIWindow on the
    /// external UIScreen, replacing mirroring) or "route" (leave mirroring up and
    /// let AVFoundation take the picture over). See "External display surface".
    var extMode = "window"
    /// When `position` was sampled. Handoff extrapolates from this.
    var armedAt = Date()
}

// MARK: - Plugin

@objc(NativePlayback)
public class NativePlayback: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "NativePlayback"
    public let jsName = "NativePlayback"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "arm",       returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "tick",      returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "disarm",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "takeover",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "resume",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "state",     returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setTvMode", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "displays",  returnType: CAPPluginReturnPromise),
    ]

    private let mgr = NativePlaybackManager.shared

    public override func load() {
        mgr.onEvent = { [weak self] name, payload in
            self?.notifyListeners(name, data: payload)
        }
        mgr.bootstrap()
    }

    @objc func arm(_ call: CAPPluginCall) {
        mgr.arm(from: call)
        call.resolve()
    }

    @objc func tick(_ call: CAPPluginCall) {
        mgr.tick(position: call.getDouble("position") ?? 0,
                 paused: call.getBool("paused") ?? false,
                 duration: call.getDouble("duration") ?? 0)
        call.resolve()
    }

    @objc func disarm(_ call: CAPPluginCall) {
        mgr.disarm()
        call.resolve()
    }

    @objc func takeover(_ call: CAPPluginCall) {
        mgr.startNative(reason: call.getString("reason") ?? "manual")
        call.resolve(["started": mgr.isNativeActive])
    }

    @objc func resume(_ call: CAPPluginCall) {
        call.resolve(mgr.reclaim())
    }

    @objc func state(_ call: CAPPluginCall) {
        call.resolve(mgr.snapshot())
    }

    @objc func setTvMode(_ call: CAPPluginCall) {
        let on = call.getBool("on") ?? false
        mgr.setTvMode(on)
        call.resolve(["on": mgr.tvModeOn])
    }

    @objc func displays(_ call: CAPPluginCall) {
        call.resolve(mgr.displayInfo())
    }
}

// MARK: - Manager

/// Process-wide owner of the AVPlayer, the audio session, the Now Playing item
/// and TV Mode. A singleton (not plugin-scoped) because it must outlive any
/// webview reload and because the remote-command / Live Activity entry points
/// can fire while no plugin instance is around.
final class NativePlaybackManager: NSObject, PlaybackCommandSink {
    static let shared = NativePlaybackManager()

    var onEvent: ((String, [String: Any]) -> Void)?

    private var armed = ArmedPlayback()
    private var player: AVPlayer?
    private var item: AVPlayerItem?
    private var timeObserver: Any?
    private var statusObs: NSKeyValueObservation?
    private var externalObs: NSKeyValueObservation?
    /// Our own window on the external display, and the layer inside it. See
    /// "External display surface".
    private var extWindow: UIWindow?
    private var extLayer: AVPlayerLayer?
    /// Presentation of last resort when there is no wired screen — AirPlay will
    /// not route video for a player that presents nowhere.
    private var mainLayer: AVPlayerLayer?
    private var bgTask: UIBackgroundTaskIdentifier = .invalid
    private var lastProgressPost = Date.distantPast
    private var sessionActivated = false
    private var endedFlag = false
    private var handBackDeadline: DispatchWorkItem?

    /// Brightness captured when TV Mode blanked the screen. Also mirrored into
    /// the App Group so a crash can't strand the user at 0 brightness.
    private var savedBrightness: CGFloat?
    private(set) var tvModeOn = false

    var isNativeActive: Bool { player != nil }

    // MARK: Lifecycle

    func bootstrap() {
        PlaybackCommandBus.sink = self
        restoreStrandedBrightness()
        // Cold start: nothing is playing yet, so any playback activity still on
        // screen is a leftover from a session that ended while we were killed.
        PlaybackLiveActivity.shared.reapStrays()
        drainPendingCommand()
        installRemoteCommands()

        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(appDidEnterBackground),
                       name: UIApplication.didEnterBackgroundNotification, object: nil)
        nc.addObserver(self, selector: #selector(appDidBecomeActive),
                       name: UIApplication.didBecomeActiveNotification, object: nil)
        nc.addObserver(self, selector: #selector(appWillResignActive),
                       name: UIApplication.willResignActiveNotification, object: nil)
        nc.addObserver(self, selector: #selector(appWillTerminate),
                       name: UIApplication.willTerminateNotification, object: nil)
        nc.addObserver(self, selector: #selector(screenDidChange),
                       name: UIScreen.didConnectNotification, object: nil)
        nc.addObserver(self, selector: #selector(screenDidChange),
                       name: UIScreen.didDisconnectNotification, object: nil)
    }

    // MARK: Arming

    func arm(from call: CAPPluginCall) {
        var a = ArmedPlayback()
        a.active         = call.getBool("active") ?? false
        a.url            = URL(string: call.getString("url") ?? "")
        a.position       = call.getDouble("position") ?? 0
        a.duration       = call.getDouble("duration") ?? 0
        a.paused         = call.getBool("paused") ?? false
        a.rate           = call.getDouble("rate") ?? 1
        a.title          = call.getString("title") ?? ""
        a.series         = call.getString("series") ?? ""
        a.audioName      = call.getString("audioName")
        a.audioLang      = call.getString("audioLang")
        a.subIndex       = call.getInt("subIndex") ?? -1
        a.subLang        = call.getString("subLang")
        a.itemId         = call.getString("itemId") ?? ""
        a.filePath       = call.getString("filePath") ?? ""
        a.profileId      = call.getString("profileId") ?? ""
        a.serverUrl      = call.getString("serverUrl") ?? ""
        a.token          = call.getString("token") ?? ""
        a.canPrev        = call.getBool("canPrev") ?? false
        a.canNext        = call.getBool("canNext") ?? false
        a.nextUrl        = URL(string: call.getString("nextUrl") ?? "")
        a.nextTitle      = call.getString("nextTitle") ?? ""
        a.nextFilePath   = call.getString("nextFilePath") ?? ""
        a.nextItemId     = call.getString("nextItemId") ?? ""
        a.handoffEnabled = call.getBool("handoffEnabled") ?? true
        a.extMode        = call.getString("extMode") ?? "window"
        a.armedAt        = Date()

        let wasActive = armed.active
        armed = a

        // The Live Activity must be REQUESTED while foreground — a request from
        // a background handler is unreliable. So it starts here, on the first
        // arm of a session, not at handoff time. iOS surfaces it in the Island
        // once the app is minimised (same behaviour TVRemote relies on).
        if a.active && !wasActive {
            PlaybackLiveActivity.shared.start(state: liveActivityState())
        } else if a.active {
            PlaybackLiveActivity.shared.update(state: liveActivityState(), force: false)
        } else if wasActive {
            PlaybackLiveActivity.shared.end()
        }
    }

    func tick(position: Double, paused: Bool, duration: Double) {
        guard armed.active else { return }
        armed.position = position
        armed.paused = paused
        if duration > 0 { armed.duration = duration }
        armed.armedAt = Date()
        if !isNativeActive {
            PlaybackLiveActivity.shared.update(state: liveActivityState(), force: false)
        }
    }

    func disarm() {
        armed = ArmedPlayback()
        stopNative(endActivity: true)
        // Deliberately does NOT exit TV Mode. The two are orthogonal: disarm
        // fires on every file teardown (including a normal episode advance,
        // which briefly has no armed URL), and dropping the blackout there would
        // flash the phone's screen back on mid-episode. Only lpStop() — real
        // "playback is over" — exits TV Mode.
    }

    // MARK: Handoff in

    @objc private func appDidEnterBackground() {
        guard armed.active, armed.handoffEnabled, armed.url != nil else { return }
        guard !isNativeActive else { return }
        startNative(reason: "background")
    }

    func startNative(reason: String) {
        guard let url = armed.url, !isNativeActive else { return }

        // Hold the process up through asset load + first frame. Once the player
        // is actually playing, the `audio` background mode takes over and this
        // assertion is released.
        beginBgTask()

        // Audio session is set LAZILY, here — never at launch. `.playback` is a
        // PROCESS-WIDE setting: activating it early would make every WKWebView
        // sound ignore the ringer switch from boot, which is not what a user
        // who never backgrounds playback signed up for.
        if !sessionActivated {
            let s = AVAudioSession.sharedInstance()
            try? s.setCategory(.playback, mode: .moviePlayback)
            try? s.setActive(true)
            sessionActivated = true
        }

        endedFlag = false
        let startAt = extrapolatedPosition()
        let it = AVPlayerItem(url: url)
        let p = AVPlayer(playerItem: it)
        p.allowsExternalPlayback = true
        p.usesExternalPlaybackWhileExternalScreenIsActive = true
        p.appliesMediaSelectionCriteriaAutomatically = false
        p.actionAtItemEnd = .pause
        item = it
        player = p

        // A player with no layer anywhere is AUDIO-ONLY: it never enters external
        // playback, and a connected monitor just keeps mirroring — which, once the
        // phone is locked, means mirroring the lock screen. Give it a surface.
        attachVideoSurface()

        statusObs = it.observe(\.status, options: [.new]) { [weak self] obs, _ in
            guard let self = self, obs.status == .readyToPlay else { return }
            self.applyTrackSelection(on: obs)
            self.seekAndPlay(to: startAt)
        }
        externalObs = p.observe(\.isExternalPlaybackActive, options: [.new]) { [weak self] _, _ in
            guard let self = self else { return }
            self.emit("displayChanged", self.displayInfo())
            PlaybackLiveActivity.shared.update(state: self.liveActivityState(), force: true)
        }

        NotificationCenter.default.addObserver(
            self, selector: #selector(itemDidEnd(_:)),
            name: .AVPlayerItemDidPlayToEndTime, object: it)

        installTimeObserver(on: p)
        emit("nativeStarted", ["reason": reason, "position": startAt,
                               "extMode": armed.extMode,
                               "extWindow": extWindow != nil,
                               "display": externalScreen != nil])
        PlaybackLiveActivity.shared.update(state: liveActivityState(), force: true)
    }

    /// Where the playhead really is *now*, reconstructed from the last armed
    /// sample. The deliberate 0.3 s rewind guarantees any residual error lands
    /// BEHIND the true position — repeating a third of a second is invisible,
    /// skipping content is not.
    private func extrapolatedPosition() -> Double {
        var t = armed.position
        if !armed.paused {
            t += Date().timeIntervalSince(armed.armedAt) * max(armed.rate, 0)
        }
        t -= 0.3
        if armed.duration > 1 { t = min(t, armed.duration - 1) }
        return max(t, 0)
    }

    private func seekAndPlay(to t: Double) {
        guard let p = player else { return }
        let target = CMTime(seconds: t, preferredTimescale: 600)
        p.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            guard let self = self else { return }
            if !self.armed.paused { p.play() }
            self.updateNowPlaying()
            self.endBgTask()
        }
    }

    /// Map the web player's picks onto AVFoundation media selections.
    ///
    /// Matched by NAME then language — never by index. AVMediaSelectionGroup
    /// ordering is not guaranteed to match meta.json's arrays, so an index match
    /// would silently pick the wrong language on some bundles.
    private func applyTrackSelection(on it: AVPlayerItem) {
        let asset = it.asset
        if let group = asset.mediaSelectionGroup(forMediaCharacteristic: .audible) {
            var pick: AVMediaSelectionOption?
            if let want = armed.audioName {
                pick = group.options.first { $0.displayName == want }
            }
            if pick == nil, let lang = armed.audioLang, !lang.isEmpty {
                pick = group.options.first { $0.locale?.languageCode == lang }
            }
            if let pick = pick { it.select(pick, in: group) }
        }
        if let group = asset.mediaSelectionGroup(forMediaCharacteristic: .legible) {
            if armed.subIndex < 0 {
                it.select(nil, in: group)
            } else {
                var pick: AVMediaSelectionOption?
                if let lang = armed.subLang, !lang.isEmpty {
                    pick = group.options.first { $0.locale?.languageCode == lang }
                }
                if pick == nil, armed.subIndex < group.options.count {
                    pick = group.options[armed.subIndex]
                }
                it.select(pick, in: group)
            }
        }
    }

    // MARK: Clock, Now Playing, progress

    private func installTimeObserver(on p: AVPlayer) {
        timeObserver = p.addPeriodicTimeObserver(
            forInterval: CMTime(seconds: 1, preferredTimescale: 600),
            queue: .main
        ) { [weak self] time in
            guard let self = self else { return }
            let t = time.seconds
            guard t.isFinite else { return }
            self.armed.position = t
            self.armed.armedAt = Date()
            self.armed.paused = (p.timeControlStatus != .playing)
            self.updateNowPlaying()
            PlaybackLiveActivity.shared.update(state: self.liveActivityState(), force: false)
            self.maybePostProgress(t)
        }
    }

    private func updateNowPlaying() {
        guard let p = player else { return }
        var info: [String: Any] = [
            MPMediaItemPropertyTitle: armed.title,
            MPNowPlayingInfoPropertyElapsedPlaybackTime: armed.position,
            MPNowPlayingInfoPropertyPlaybackRate: p.timeControlStatus == .playing ? 1.0 : 0.0,
            MPNowPlayingInfoPropertyIsLiveStream: false,
        ]
        if !armed.series.isEmpty { info[MPMediaItemPropertyArtist] = armed.series }
        if armed.duration > 0 { info[MPMediaItemPropertyPlaybackDuration] = armed.duration }
        MPNowPlayingInfoCenter.default().nowPlayingInfo = info
    }

    /// Write watch progress to the host ourselves.
    ///
    /// This is NOT belt-and-braces: while the app is backgrounded the webview's
    /// JS timers are frozen, so `_lpClockTick`'s 15 s save never fires. Without
    /// this, an episode watched with the phone locked would record nothing.
    /// Mirrors saveProgress()'s endpoint and its near-zero guard.
    private func maybePostProgress(_ t: Double, force: Bool = false) {
        guard t >= 5, armed.duration > 0, !armed.itemId.isEmpty, !armed.filePath.isEmpty,
              !armed.serverUrl.isEmpty else { return }
        guard force || Date().timeIntervalSince(lastProgressPost) >= 15 else { return }
        lastProgressPost = Date()

        guard let base = URL(string: armed.serverUrl),
              let url = URL(string: "/api/library/\(armed.itemId)/progress", relativeTo: base)
        else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !armed.token.isEmpty { req.setValue(armed.token, forHTTPHeaderField: "X-Device-Token") }
        req.timeoutInterval = 8
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "profile_id":   armed.profileId,
            "file_path":    armed.filePath,
            "position_sec": t,
            "duration_sec": armed.duration,
        ])
        URLSession.shared.dataTask(with: req).resume()   // best-effort
    }

    // MARK: End of file / auto-advance

    @objc private func itemDidEnd(_ note: Notification) {
        maybePostProgress(armed.duration, force: true)

        // Advance in place if the web player armed a next episode — it already
        // preps it (_lpWarmNextEp), so this costs no extra host round-trip.
        if let next = armed.nextUrl {
            armed.position = 0
            armed.armedAt = Date()
            armed.title = armed.nextTitle
            armed.filePath = armed.nextFilePath
            if !armed.nextItemId.isEmpty { armed.itemId = armed.nextItemId }
            armed.url = next
            armed.nextUrl = nil
            emit("nativeAdvanced", ["filePath": armed.filePath, "url": next.absoluteString])
            replaceItem(with: next)
            return
        }
        endedFlag = true
        emit("nativeEnded", ["filePath": armed.filePath])
        stopNative(endActivity: true)
    }

    private func replaceItem(with url: URL) {
        guard let p = player else { return }
        if let old = item {
            NotificationCenter.default.removeObserver(
                self, name: .AVPlayerItemDidPlayToEndTime, object: old)
        }
        statusObs?.invalidate()
        let it = AVPlayerItem(url: url)
        item = it
        statusObs = it.observe(\.status, options: [.new]) { [weak self] obs, _ in
            guard let self = self, obs.status == .readyToPlay else { return }
            self.applyTrackSelection(on: obs)
        }
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemDidEnd(_:)),
            name: .AVPlayerItemDidPlayToEndTime, object: it)
        p.replaceCurrentItem(with: it)
        p.play()
        PlaybackLiveActivity.shared.update(state: liveActivityState(), force: true)
    }

    // MARK: Hand back

    @objc private func appDidBecomeActive() {
        restoreStrandedBrightness()
        drainPendingCommand()
        // Hand the external display back to mirroring: the web player is about to
        // become primary again, and TV Mode's whole premise is that the monitor
        // mirrors it. Safe when nothing was ever claimed.
        detachExternalWindow()
        if tvModeOn { applyBlank(true) }   // re-dim after a transient interruption
        guard isNativeActive else { return }

        // If the webview never calls resume() — it reloaded, crashed, or the
        // page was replaced — we'd be left playing invisible audio with no UI.
        // Tear down after a grace period so the app can't get into that state.
        handBackDeadline?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.isNativeActive else { return }
            self.maybePostProgress(self.armed.position, force: true)
            self.stopNative(endActivity: true)
        }
        handBackDeadline = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }

    func reclaim() -> [String: Any] {
        handBackDeadline?.cancel(); handBackDeadline = nil
        let wasActive = isNativeActive
        let pos = armed.position
        let paused = armed.paused
        if wasActive {
            maybePostProgress(pos, force: true)
            stopNative(endActivity: false)
        }
        return [
            "active":   wasActive,
            "position": pos,
            "paused":   paused,
            "ended":    endedFlag,
            "itemId":   armed.itemId,
            "filePath": armed.filePath,
        ]
    }

    private func stopNative(endActivity: Bool) {
        handBackDeadline?.cancel(); handBackDeadline = nil
        if let p = player, let obs = timeObserver { p.removeTimeObserver(obs) }
        timeObserver = nil
        statusObs?.invalidate(); statusObs = nil
        externalObs?.invalidate(); externalObs = nil
        if let old = item {
            NotificationCenter.default.removeObserver(
                self, name: .AVPlayerItemDidPlayToEndTime, object: old)
        }
        detachExternalWindow()      // mirroring resumes; TV Mode gets its screen back
        detachFallbackLayer()
        player?.pause()
        player?.replaceCurrentItem(with: nil)
        player = nil
        item = nil
        MPNowPlayingInfoCenter.default().nowPlayingInfo = nil
        if sessionActivated {
            // Let WebKit have the session back for foreground playback.
            try? AVAudioSession.sharedInstance().setActive(
                false, options: .notifyOthersOnDeactivation)
            sessionActivated = false
        }
        endBgTask()
        if endActivity { PlaybackLiveActivity.shared.end() }
    }

    // MARK: Transport (remote commands + Live Activity intents share this)

    func handlePlaybackCommand(_ cmd: PlaybackCommand) {
        switch cmd {
        case .playPause:
            if let p = player {
                if p.timeControlStatus == .playing { p.pause() } else { p.play() }
                armed.paused = (p.timeControlStatus != .playing)
            }
        case .skipForward: skip(by: 15)
        case .skipBack:    skip(by: -15)
        case .next:        emit("nativeAdvanced", ["request": "next"])
        case .prev:        emit("nativeAdvanced", ["request": "prev"])
        }
        updateNowPlaying()
        PlaybackLiveActivity.shared.update(state: liveActivityState(), force: true)
    }

    private func skip(by delta: Double) {
        guard let p = player else { return }
        var t = armed.position + delta
        if armed.duration > 1 { t = min(t, armed.duration - 1) }
        t = max(t, 0)
        armed.position = t
        armed.armedAt = Date()
        p.seek(to: CMTime(seconds: t, preferredTimescale: 600),
               toleranceBefore: .zero, toleranceAfter: .zero)
    }

    private func drainPendingCommand() {
        guard let raw = AppGroupConfig.pendingPlaybackCommand,
              let cmd = PlaybackCommand(rawValue: raw) else { return }
        AppGroupConfig.pendingPlaybackCommand = nil
        handlePlaybackCommand(cmd)
    }

    private func installRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        _ = c.playCommand.addTarget { [weak self] _ in
            self?.player?.play(); return .success
        }
        _ = c.pauseCommand.addTarget { [weak self] _ in
            self?.player?.pause(); return .success
        }
        _ = c.togglePlayPauseCommand.addTarget { [weak self] _ in
            self?.handlePlaybackCommand(.playPause); return .success
        }
        c.skipForwardCommand.preferredIntervals = [15]
        _ = c.skipForwardCommand.addTarget { [weak self] _ in
            self?.handlePlaybackCommand(.skipForward); return .success
        }
        c.skipBackwardCommand.preferredIntervals = [15]
        _ = c.skipBackwardCommand.addTarget { [weak self] _ in
            self?.handlePlaybackCommand(.skipBack); return .success
        }
        _ = c.changePlaybackPositionCommand.addTarget { [weak self] ev in
            guard let self = self, let p = self.player,
                  let e = ev as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            self.armed.position = e.positionTime
            self.armed.armedAt = Date()
            p.seek(to: CMTime(seconds: e.positionTime, preferredTimescale: 600),
                   toleranceBefore: .zero, toleranceAfter: .zero)
            return .success
        }
        _ = c.nextTrackCommand.addTarget { [weak self] _ in
            self?.handlePlaybackCommand(.next); return .success
        }
        _ = c.previousTrackCommand.addTarget { [weak self] _ in
            self?.handlePlaybackCommand(.prev); return .success
        }
    }

    // MARK: TV Mode

    /// Blank the phone's own screen while keeping the app FOREGROUND, so
    /// mirroring keeps feeding the monitor with the full custom player and the
    /// libass subtitle overlay — neither of which can survive a real lock.
    func setTvMode(_ on: Bool) {
        if on {
            if savedBrightness == nil {
                savedBrightness = UIScreen.main.brightness
                AppGroupConfig.strandedBrightness = Double(UIScreen.main.brightness)
            }
            tvModeOn = true
            applyBlank(true)
        } else {
            tvModeOn = false
            applyBlank(false)
        }
    }

    private func applyBlank(_ on: Bool) {
        DispatchQueue.main.async {
            if on {
                UIScreen.main.brightness = 0.0
                UIApplication.shared.isIdleTimerDisabled = true
            } else {
                if let b = self.savedBrightness { UIScreen.main.brightness = b }
                UIApplication.shared.isIdleTimerDisabled = false
                self.savedBrightness = nil
                AppGroupConfig.strandedBrightness = nil
            }
        }
    }

    /// A crash or force-quit while dimmed leaves the screen at 0 — iOS does not
    /// put it back. Recover on the next launch/foreground.
    private func restoreStrandedBrightness() {
        guard !tvModeOn, let b = AppGroupConfig.strandedBrightness else { return }
        AppGroupConfig.strandedBrightness = nil
        savedBrightness = nil
        DispatchQueue.main.async {
            UIScreen.main.brightness = CGFloat(b)
            UIApplication.shared.isIdleTimerDisabled = false
        }
    }

    @objc private func appWillResignActive() {
        // A call, Control Centre, or the power button. Put the brightness back
        // immediately — `tvModeOn` is kept so didBecomeActive can re-dim.
        if tvModeOn { applyBlank(false); tvModeOn = true }

        // Claim the external display NOW, while the app can still draw. A window
        // created inside didEnterBackground may never get its first composite
        // pass, and the monitor would sit on the mirrored lock screen for the rest
        // of the episode. If this turns out to be a transient resign (Control
        // Centre, a banner) didBecomeActive hands the screen straight back.
        if armed.active, armed.handoffEnabled, wantsOwnExternalWindow {
            onMain { [weak self] in self?.ensureExternalWindow() }
        }
    }

    @objc private func appWillTerminate() {
        if tvModeOn || savedBrightness != nil {
            if let b = savedBrightness { UIScreen.main.brightness = b }
            UIApplication.shared.isIdleTimerDisabled = false
            AppGroupConfig.strandedBrightness = nil
        }
        maybePostProgress(armed.position, force: true)
    }

    // MARK: External display surface

    // WHY A WINDOW OF OUR OWN
    // `allowsExternalPlayback` / `usesExternalPlaybackWhileExternalScreenIsActive`
    // only describe how an already-PRESENTED video is ROUTED. An AVPlayer with no
    // AVPlayerLayer anywhere presents nothing: it decodes audio, its
    // `isExternalPlaybackActive` never flips, and a wired monitor goes on
    // mirroring — so at lock it shows the lock screen. That was the bug: the
    // handoff kept the audio and lost the picture.
    //
    // A UIWindow on the external UIScreen is a real surface AND it REPLACES
    // mirroring for that screen. Replacing mirroring is exactly right once the
    // phone is locked, and exactly wrong while TV Mode is running (TV Mode needs
    // mirroring to carry the custom player + the libass overlay). So the window is
    // built on the way OUT (willResignActive, one composite pass before we lose
    // the ability to draw) and destroyed on the way BACK IN.

    private var externalScreen: UIScreen? { UIScreen.screens.first { $0 !== UIScreen.main } }

    /// UIKit from wherever we are called. Capacitor delivers plugin methods off
    /// the main thread (`takeover`, `resume`), the lifecycle notifications on it —
    /// and every window/layer touch below must run on main either way. Synchronous
    /// when already on main, because `ensureExternalWindow` is racing the last
    /// composite pass before the app loses the ability to draw.
    private func onMain(_ work: @escaping () -> Void) {
        if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) }
    }

    /// True when we should claim the external screen with a window of our own,
    /// rather than leaving mirroring up for AVFoundation's route to take over.
    private var wantsOwnExternalWindow: Bool {
        externalScreen != nil && armed.extMode != "route"
    }

    /// Give the player a video surface when — and ONLY when — there is a monitor to
    /// put it on. A layerless AVPlayer is audio-only, which is the bug with a
    /// display attached and exactly the behaviour we want without one: attaching an
    /// AVPlayerLayer on the PHONE's own screen is the classic way to make
    /// AVFoundation suspend video on background (the documented cure for
    /// background audio is `playerLayer.player = nil`), so doing it unconditionally
    /// would risk the plain locked-phone-listening case to serve a monitor that
    /// isn't there.
    private func attachVideoSurface() {
        onMain { [weak self] in
            guard let self = self else { return }
            if self.wantsOwnExternalWindow {
                self.ensureExternalWindow()
                self.attachExternalLayer()
            } else if self.externalScreen != nil {
                self.attachFallbackLayer()     // "Mirrored" mode
            }
        }
    }

    /// Black window on the external display. Deliberately separate from
    /// `attachExternalLayer` — at resign-active time there is no player yet, but
    /// that is the last moment the app is guaranteed a composite pass.
    private func ensureExternalWindow() {
        guard extWindow == nil, let screen = externalScreen else { return }
        let vc = UIViewController()
        vc.view.backgroundColor = .black
        let w = UIWindow(frame: screen.bounds)
        w.screen = screen                 // deprecated in iOS 13, but this app is
                                          // non-scene-based, so it is still the path
        w.backgroundColor = .black
        w.rootViewController = vc
        w.isHidden = false
        extWindow = w
    }

    private func attachExternalLayer() {
        guard extLayer == nil, let p = player,
              let root = extWindow?.rootViewController?.view else { return }
        let l = AVPlayerLayer(player: p)
        l.videoGravity = .resizeAspect
        l.backgroundColor = UIColor.black.cgColor
        l.frame = root.bounds
        root.layer.addSublayer(l)
        extLayer = l
        // Our window IS the external presentation. Letting AVFoundation also try
        // to seize the screen would have the two fighting over it.
        p.usesExternalPlaybackWhileExternalScreenIsActive = false
    }

    private func detachExternalWindow() {
        onMain { [weak self] in
            guard let self = self else { return }
            self.extLayer?.player = nil
            self.extLayer?.removeFromSuperlayer()
            self.extLayer = nil
            self.extWindow?.isHidden = true
            self.extWindow?.rootViewController = nil
            self.extWindow = nil
            self.player?.usesExternalPlaybackWhileExternalScreenIsActive = true
        }
    }

    /// "Mirrored" mode's surface: mirroring stays up and AVFoundation's
    /// external-screen route takes the picture over — but it will only do that for
    /// a player that presents somewhere. The layer sits at the BACK of the app's
    /// own window, behind the opaque webview, so it is never visible locally.
    /// Only ever attached while a display is actually connected (see
    /// `attachVideoSurface`).
    private func attachFallbackLayer() {
        guard mainLayer == nil, let p = player,
              let root = (UIApplication.shared.delegate?.window ?? nil)?
                  .rootViewController?.view else { return }
        let l = AVPlayerLayer(player: p)
        l.videoGravity = .resizeAspect
        l.frame = root.bounds
        root.layer.insertSublayer(l, at: 0)
        mainLayer = l
        // This layer exists to BE the route's presentation, so make sure the route
        // is open (attachExternalLayer closes it for the window mode).
        p.allowsExternalPlayback = true
        p.usesExternalPlaybackWhileExternalScreenIsActive = true
    }

    private func detachFallbackLayer() {
        onMain { [weak self] in
            guard let self = self else { return }
            self.mainLayer?.player = nil
            self.mainLayer?.removeFromSuperlayer()
            self.mainLayer = nil
        }
    }

    // MARK: Displays

    @objc private func screenDidChange() {
        // A display that appears or vanishes mid-handoff has to be picked up or
        // dropped right away — otherwise we keep a window on a screen that is gone,
        // or leave a freshly-plugged monitor mirroring a locked phone.
        if isNativeActive {
            if wantsOwnExternalWindow { attachVideoSurface() } else { detachExternalWindow() }
        } else if externalScreen == nil {
            detachExternalWindow()
        }
        emit("displayChanged", displayInfo())
    }

    func displayInfo() -> [String: Any] {
        let external = externalScreen
        return [
            "connected": external != nil || (player?.isExternalPlaybackActive ?? false),
            "name": external.map { "\(Int($0.bounds.width))x\(Int($0.bounds.height))" } ?? "",
            "externalPlayback": player?.isExternalPlaybackActive ?? false,
            "ownWindow": extWindow != nil,
        ]
    }

    func snapshot() -> [String: Any] {
        [
            "active":   armed.active,
            "native":   isNativeActive,
            "position": armed.position,
            "paused":   armed.paused,
            "external": player?.isExternalPlaybackActive ?? false,
            "extWindow": extWindow != nil,
            "tvMode":   tvModeOn,
        ]
    }

    // MARK: Helpers

    private func liveActivityState() -> PlaybackLiveState {
        PlaybackLiveState(
            title: armed.title, series: armed.series,
            isPaused: armed.paused || (player.map { $0.timeControlStatus != .playing } ?? armed.paused),
            position: armed.position, duration: armed.duration,
            external: player?.isExternalPlaybackActive ?? false,
            tvMode: tvModeOn, canPrev: armed.canPrev, canNext: armed.canNext)
    }

    private func emit(_ name: String, _ payload: [String: Any]) {
        DispatchQueue.main.async { [weak self] in self?.onEvent?(name, payload) }
    }

    private func beginBgTask() {
        guard bgTask == .invalid else { return }
        DispatchQueue.main.async {
            self.bgTask = UIApplication.shared.beginBackgroundTask(withName: "StreamLinkHandoff") {
                if self.bgTask != .invalid {
                    UIApplication.shared.endBackgroundTask(self.bgTask); self.bgTask = .invalid
                }
            }
        }
    }

    private func endBgTask() {
        guard bgTask != .invalid else { return }
        let id = bgTask; bgTask = .invalid
        DispatchQueue.main.async { UIApplication.shared.endBackgroundTask(id) }
    }
}
