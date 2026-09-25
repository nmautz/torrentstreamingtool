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
//  player a surface of our own — a UIWindow in the external display's UIWindowScene carrying an
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
//                            awake }
//    setAwake({on})    -> { on }
//    displays()        -> { connected, name, externalPlayback, ownWindow }
//    airplay()         -> { ok, error?, url? }   send THIS playback to an AirPlay
//                         receiver (18.26.0 spike — see "AirPlay" below)
//  Events: nativeStarted, nativeEnded, nativeAdvanced, displayChanged,
//          airplayEnded (the route was never picked, or was dropped),
//          nativeYielded (another device pulled this playback over — see
//          maybePostSession / honourYield)
//
//  See docs/STREAMING.md and docs/GOTCHAS.md ("iOS background playback").
//

import Foundation
import Capacitor
import AVFoundation
import AVKit
import MediaPlayer
import UIKit

/// The build stamp both the launch row and the external-display header carry.
///
/// Bump with any change to this file. It is the ONLY trustworthy version signal
/// the app has — `CFBundleShortVersionString` is pinned at 1.0 and never moves,
/// and the dashboard badge belongs to the host, not to the installed binary.
/// It lived as two separate string literals until 18.7.1; a field that exists to
/// answer "was this really rebuilt" must not be able to disagree with itself.
let NP_BUILD = "18.26.0"

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
    /// The page is the OFFLINE snapshot: there is no host, so progress goes to
    /// OfflineProgressStore — the same record the page writes, which the page
    /// syncs to the host on reconnect. Without this, an episode watched offline
    /// with the phone locked recorded nothing: JS is frozen, and a POST had
    /// nowhere to go. See maybePostProgress.
    var offline = false
    /// Cross-device playback sessions. While the app is backgrounded the
    /// webview's JS timers are frozen, so the web player's own 2 s session beat
    /// stops — this device would drop out of every other device's "playing
    /// elsewhere" banner mid-episode, which is precisely when someone wants to
    /// pull it onto the TV. So we beat for it, off the same time observer that
    /// already writes progress. See maybePostSession.
    var deviceId = ""
    var deviceName = ""
    var sessionProfileId = ""     // lp.profileId ?? the signed-in one (see _pbProfileId)
    var sessionSource = "server"  // "server" | "offline" (playing our own download)
    var canPrev = false
    var canNext = false
    var nextUrl: URL?
    var nextTitle = ""
    /// The next episode's small line ("Show · S01E03"), swapped in with
    /// nextTitle. nil from a page older than 18.24.0 (it never sends the key),
    /// which keeps `series` as before; "" is real and clears it.
    var nextSeries: String?
    var nextFilePath = ""
    var nextItemId = ""
    var handoffEnabled = true
    /// How to reach a wired monitor once locked: "window" (our own UIWindow on the
    /// display's UIWindowScene, replacing mirroring) or "route" (leave mirroring up and
    /// let AVFoundation take the picture over). See "External display surface".
    // "early" (claim the display now) or "route" (leave mirroring up and let
    // AVFoundation route the video). The old "window" — claim it AS the phone
    // locks — is gone: mirroring has already collapsed by then, so it only ever
    // delivered the lock screen. Anything unrecognised reads as "early".
    var extMode = "early"
    /// Smart Skip windows for THIS file, and the profile's auto-skip toggles.
    ///
    /// These ride along because auto-skip belongs to whoever owns the transport,
    /// and while we hold a display that is us. The page's evaluator runs off
    /// `_lpClockTick`, which is driven by the parked <video>'s `timeupdate` and a
    /// pump that returns early on `v.paused` — permanently true during a handoff —
    /// so it never fires here at all, and once the phone locks its timers are
    /// frozen outright. -1 means "no window" (and is what a file with no skip
    /// data, or an offline bundle with no host to ask, sends).
    var introStart: Double = -1
    var introEnd: Double = -1
    var creditsStart: Double = -1
    var autoSkipIntro = false
    var autoSkipCredits = false
    /// Fired or dismissed. Ours to keep: the page re-arms with its own copy, and
    /// while the phone was locked its copy is older than what we did.
    var introDone = false
    var creditsDone = false
    /// The NEXT episode's windows, so an advance that happens with the phone
    /// locked still lands on a file that can skip its own intro. The page cannot
    /// supply them after the fact — its timers are frozen — so it supplies them
    /// before, alongside `nextUrl`. See advanceToNext.
    var nextIntroStart: Double = -1
    var nextIntroEnd: Double = -1
    var nextCreditsStart: Double = -1
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
        CAPPluginMethod(name: "setAwake",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "displays",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "extDiag",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "setPaused", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "seekTo",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "sendLog",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "readLog",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clearLog",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "log",       returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "logStats",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "airplay",   returnType: CAPPluginReturnPromise),
    ]

    private let mgr = NativePlaybackManager.shared
    private var pageLoadObs: NSKeyValueObservation?

    public override func load() {
        // An arm belongs to the page that made it. Watch for that page being
        // replaced, so its arm cannot outlive it (see pageWillLoad).
        pageLoadObs = bridge?.webView?.observe(\.isLoading, options: [.new]) { [weak self] _, ch in
            guard ch.newValue == true else { return }
            DispatchQueue.main.async { self?.mgr.pageWillLoad() }
        }
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
        mgr.disarm(reason: call.getString("reason") ?? "unspecified")
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

    @objc func setAwake(_ call: CAPPluginCall) {
        let on = call.getBool("on") ?? false
        mgr.setAwake(on)
        call.resolve(["on": mgr.awakeOn])
    }

    @objc func displays(_ call: CAPPluginCall) {
        call.resolve(mgr.displayInfo())
    }

    @objc func extDiag(_ call: CAPPluginCall) {
        call.resolve(mgr.extDiagInfo())
    }

    /// Transport control for the page while the NATIVE player owns playback.
    /// In early mode the phone is a remote, not a second engine.
    @objc func setPaused(_ call: CAPPluginCall) {
        mgr.setPaused(call.getBool("paused") ?? false,
                      source: call.getString("source") ?? "js")
        call.resolve(mgr.snapshot())
    }

    @objc func seekTo(_ call: CAPPluginCall) {
        mgr.seekTo(call.getDouble("position") ?? 0)
        call.resolve(mgr.snapshot())
    }

    /// Ship the on-disk log to the host, where it lands in `logs/` and can be
    /// read through /api/admin/logs without touching the phone.
    @objc func sendLog(_ call: CAPPluginCall) {
        let server = call.getString("serverUrl") ?? ""
        let device = call.getString("device") ?? "ios"
        DiagLog.shared.write("sendLog", ["device": device], cat: "app")
        DiagLog.shared.upload(serverUrl: server, device: device) { result in
            call.resolve(["result": result, "bytes": DiagLog.shared.byteCount()])
        }
    }

    /// Fallback for when the host is unreachable: hand the text to the page so
    /// it can be copied or shared.
    @objc func readLog(_ call: CAPPluginCall) {
        call.resolve(["text": DiagLog.shared.contents(),
                      "bytes": DiagLog.shared.byteCount()])
    }

    @objc func clearLog(_ call: CAPPluginCall) {
        DiagLog.shared.clear()
        DiagLog.shared.write("log-cleared", [:], cat: "app")
        call.resolve(["ok": true])
    }

    /// Let the WEB player write into the same transcript.
    ///
    /// Without this the log only knew what the NATIVE player did, which is a
    /// small and unrepresentative slice: on-device playback is mostly the
    /// WKWebView's own `<video>`, and the offline bundle path barely touches
    /// the native player at all. Two logs in two places, neither of which can
    /// be lined up against the other, is how "the picture froze when I plugged
    /// the glasses in" stayed unanswerable. One file, one clock, every writer.
    @objc func log(_ call: CAPPluginCall) {
        let ev = call.getString("ev") ?? "js"
        let cat = call.getString("cat") ?? "js"
        // Rebuilt as [String: Any] rather than handed across as a JSObject: the
        // bridge's value type is its own, and DiagLog's sanitizer is written
        // against plain Foundation values.
        var fields: [String: Any] = [:]
        for (k, v) in (call.getObject("fields") ?? [:]) { fields[k] = v }
        fields["src"] = "js"
        DiagLog.shared.write(ev, fields, cat: cat)
        call.resolve(["ok": true])
    }

    @objc func logStats(_ call: CAPPluginCall) {
        call.resolve(DiagLog.shared.stats())
    }

    @objc func airplay(_ call: CAPPluginCall) {
        mgr.startAirPlay { call.resolve($0) }
    }
}

// MARK: - Persistent diagnostic log

/// An on-disk log that outlives the process.
///
/// The in-memory trail was built for a ten-minute test read off the phone by
/// hand. It cannot answer "I used it for three days, here is what happened":
/// it is capped at 40 rows, it dies with the process, and its timestamps are
/// seconds-since-launch, which say nothing once there have been several
/// launches. This writes newline-delimited JSON to Caches with ABSOLUTE
/// timestamps, survives restarts, and is uploaded to the host on demand.
///
// MARK: - Crash forensics

// WHY THE LOG COULD NOT NAME A CRASH
// Measured 2026-09-23: the app died 2.2 s after `snap at:"background/attached"`
// and the very next row — `startNative`, called sub-millisecond later — is not
// in the transcript. Two separate blindnesses, both fixed here:
//
//   1. NOTHING RECORDED THE DEATH. A crash leaves no row at all, so a reader
//      cannot tell a crash from a user force-quit from an ordinary gap.
//   2. THE LAST ROWS ARE LOST. `DiagLog.write` hands the row to a serial queue
//      and returns; rows still queued when the process dies are gone. The
//      absence of `startNative` therefore proves nothing on its own.
//
// The pieces below are deliberately NOT a crash reporter — they are three
// cheap facts written where a dying process can still write them:
//
//   * a RUN MARKER, created at launch and deleted on a clean terminate, so the
//     next launch knows the last one ended badly even when no signal fired
//     (watchdog, jetsam, OOM — none of which are catchable);
//   * a PENDING-CRASH file, appended from the signal handler itself, carrying
//     the signal and a raw `backtrace_symbols_fd` dump;
//   * a LAST-EVENT breadcrumb, a fixed C buffer overwritten by every
//     `DiagLog.write` BEFORE it queues, so the row the queue never flushed is
//     still named in the crash file.
//
// Both handlers re-raise with the default disposition afterwards, so iOS still
// writes its own .ips report on the device.
//
// EVERYTHING THE SIGNAL HANDLER TOUCHES IS PRE-ALLOCATED. A handler may not
// malloc, may not take a lock and may not format a date — so the file
// descriptor is opened, the per-signal message strings rendered, and the frame
// buffer allocated at install time. `backtrace_symbols_fd` is the one symbol
// dumper that is documented async-signal-safe (it writes, it does not allocate).
// The pending file is plain text, parsed and turned into a proper NDJSON row by
// the NEXT launch, where formatting a timestamp is legal again.

private let SL_CRASH_FRAMES = 48

private var slCrashFD: Int32 = -1
private var slCrashFrames: UnsafeMutablePointer<UnsafeMutableRawPointer?>?
/// A flat C table indexed by signal number, NOT a Swift Dictionary: a handler
/// may not hash, retain or release, and a `[Int32: …]` subscript does all three.
private let SL_SIG_MAX: Int32 = 32
private var slCrashMsgs: UnsafeMutablePointer<UnsafePointer<CChar>?>?
/// `sig_atomic_t`, read and written from the handler. Guards against the second
/// report: an uncaught ObjC exception writes its own record and THEN aborts,
/// which arrives here as SIGABRT.
private var slCrashHandled: sig_atomic_t = 0
/// Last event name handed to `DiagLog.write`, in a fixed buffer so the signal
/// handler can `write(2)` it without formatting anything.
private let slLastEvent = UnsafeMutablePointer<CChar>.allocate(capacity: 64)

private func slNoteEvent(_ name: String) {
    name.withCString { src in
        _ = strlcpy(slLastEvent, src, 64)
    }
}

/// The pending file's path as a global, because the exception handler below is
/// also a C function pointer and so may capture NOTHING — not even `self`, not
/// even a `let` from the enclosing scope. (Swift catches that at SILGen, not at
/// type-check, so `swiftc -typecheck` will happily wave a capturing closure
/// through and the real build will not.)
private var slCrashPendingPath = ""

/// An uncaught ObjC exception — the likeliest shape of a UIKit or AVFoundation
/// crash, and the only one that can say WHY in words. This is ORDINARY code
/// (the `abort()` comes after), so unlike the signal handler it may allocate.
private func slUncaughtExceptionHandler(_ e: NSException) {
    slCrashHandled = 1
    guard !slCrashPendingPath.isEmpty else { return }
    var text = "kind=exception\n"
    text += "name=\(e.name.rawValue)\n"
    text += "reason=\((e.reason ?? "").replacingOccurrences(of: "\n", with: " "))\n"
    text += "last=\(String(cString: slLastEvent))\n"
    text += e.callStackSymbols.prefix(24).joined(separator: "\n")
    text += "\n---\n"
    guard let d = text.data(using: .utf8),
          let h = FileHandle(forWritingAtPath: slCrashPendingPath) else { return }
    _ = try? h.seekToEnd()
    try? h.write(contentsOf: d)
    try? h.close()
}

private func slCrashSignalHandler(_ sig: Int32) {
    if slCrashHandled == 0 {
        slCrashHandled = 1
        let fd = slCrashFD
        if fd >= 0 {
            if sig > 0, sig < SL_SIG_MAX, let tbl = slCrashMsgs, let msg = tbl[Int(sig)] {
                _ = write(fd, msg, strlen(msg))
            }
            _ = write(fd, "last=", 5)
            _ = write(fd, slLastEvent, strlen(slLastEvent))
            _ = write(fd, "\n", 1)
            if let frames = slCrashFrames {
                let n = backtrace(frames, Int32(SL_CRASH_FRAMES))
                backtrace_symbols_fd(frames, n, fd)
            }
            _ = write(fd, "---\n", 4)
        }
    }
    // Hand the corpse back to the system so the device still gets its .ips.
    signal(sig, SIG_DFL)
    raise(sig)
}

/// Caches rather than Documents deliberately: this is disposable, and iOS may
/// reclaim it under storage pressure rather than failing a write.
///
/// Every row carries a `cat`. The categories are the three things this log is
/// meant to explain, plus the two that explain them:
///
///   * `play`    — the native player's transport: what it was told, what it did.
///   * `ext`     — external display: screens, scenes, windows, layers, routes.
///   * `offline` — downloaded bundles: fetch, verify, local server, offline play.
///   * `net`     — anything crossing to the host (progress, session, subtitles).
///   * `app`     — lifecycle: launch, foreground, audio session, interruptions.
///
/// A category is not decoration. A day of use is mostly `snap` rows, and the
/// server can filter on `cat` without shipping the rest — which is the
/// difference between "read the log" and "download nine thousand rows".
final class DiagLog {
    static let shared = DiagLog()

    private let q = DispatchQueue(label: "streamlink.diaglog", qos: .utility)
    // Raised from 3 MB with the categories: the log now covers the web player
    // and the offline path too, so the same wall-clock span costs more rows.
    private let maxBytes = 8 * 1024 * 1024
    private lazy var dir: URL = {
        FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask)[0]
    }()
    private lazy var url: URL = dir.appendingPathComponent("streamlink-diag.log")
    /// Exists for exactly as long as a run does. Present at launch = the last run
    /// did not reach `applicationWillTerminate`.
    private lazy var runMarker: URL = dir.appendingPathComponent("streamlink-run.marker")
    /// Written BY the dying process, read by the next one. See "Crash forensics".
    private lazy var crashPending: URL = dir.appendingPathComponent("streamlink-crash.pending")
    private lazy var iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    /// Set by the last upload the host answered. Purely informational — shown in
    /// Settings so "did my log actually land?" has an answer on the device.
    private(set) var lastUpload = ""
    private(set) var lastUploadResult = ""

    /// One JSON object per line. Never throws into the caller: a diagnostic that
    /// can break playback is worse than no diagnostic.
    func write(_ event: String, _ fields: [String: Any] = [:], cat: String = "play") {
        var row: [String: Any] = fields
        row["t"] = iso.string(from: Date())
        row["ev"] = event
        row["cat"] = cat
        // Breadcrumb first, queue second. A row that is still queued when the
        // process dies is lost, and the one we most want is always the last one.
        slNoteEvent(event)
        q.async { [weak self] in
            guard let self = self else { return }
            // A field the caller passed that JSON cannot represent (an NSNull, a
            // non-finite Double from a CMTime that was indefinite) used to take
            // the WHOLE ROW down silently, because serialization failed and the
            // guard returned. Drop the offending field, keep the row: a row that
            // says "pos: unrepresentable" is evidence; a missing row is not.
            let safe = Self.sanitize(row)
            guard let data = try? JSONSerialization.data(withJSONObject: safe),
                  var line = String(data: data, encoding: .utf8) else { return }
            line += "\n"
            self.append(line)
        }
    }

    // MARK: Run lifecycle (see "Crash forensics" above)

    /// Call ONCE, before the `launch` row: it is what turns the previous run's
    /// death into rows, and the order matters — the report belongs above the
    /// launch it was found at, not below it.
    func openRun(build: String) {
        reportPreviousDeath(build: build)
        writeMarker(state: "launching", build: build)
        installCrashHandlers()
    }

    /// The app reached `applicationWillTerminate`: an ORDINARY exit. Anything
    /// that skips this — a crash, a watchdog kill, a jetsam, a force-quit from
    /// the switcher — leaves the marker behind and is reported next launch.
    func closeRun() {
        try? FileManager.default.removeItem(at: runMarker)
    }

    /// Foreground/background is the single most useful qualifier on a dirty
    /// marker: iOS kills BACKGROUND apps routinely and for reasons that are not
    /// bugs, so `state: "bg"` is noise and `state: "fg"` is a real defect.
    func noteAppState(_ state: String) {
        guard FileManager.default.fileExists(atPath: runMarker.path) else { return }
        markerState = state
        writeMarker(state: state, build: NP_BUILD)
    }

    /// THE MARKER'S `at` USED TO BE THE LAST TRANSITION, NOT THE LAST SIGN OF
    /// LIFE, and on 2026-09-23 that cost the whole overnight test: the app was
    /// killed somewhere inside a five-hour backgrounded window and
    /// `prev-launch-dirty since:` could only name the moment it went background,
    /// which was five hours before the last thing that could have happened.
    /// Refreshing it from the download heartbeat turns `since` into the time of
    /// death ± one beat. Cheap (one small atomic write per 30 s) and only while
    /// there is something running to beat.
    ///
    /// `extra` rides along so a death can be described by whatever was true at
    /// the time — today the age of a continued-processing grant, which otherwise
    /// leaves no trace at all when the process dies underneath it.
    func touchMarker(_ extra: [String: Any] = [:]) {
        guard FileManager.default.fileExists(atPath: runMarker.path) else { return }
        writeMarker(state: markerState, build: NP_BUILD, extra: extra)
    }

    private var markerState = "launching"

    private func writeMarker(state: String, build: String, extra: [String: Any] = [:]) {
        var row: [String: Any] = ["at": iso.string(from: Date()),
                                  "state": state, "build": build]
        for (k, v) in extra { row[k] = v }
        guard let d = try? JSONSerialization.data(withJSONObject: Self.sanitize(row)) else { return }
        try? d.write(to: runMarker, options: .atomic)
    }

    private func reportPreviousDeath(build: String) {
        let fm = FileManager.default
        let marker = (try? Data(contentsOf: runMarker))
            .flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? nil

        // The signal/exception record, if the process got far enough to write one.
        var reported = 0
        if let text = try? String(contentsOf: crashPending, encoding: .utf8), !text.isEmpty {
            for record in text.components(separatedBy: "---\n") where !record.isEmpty {
                guard let row = Self.parseCrashRecord(record) else { continue }
                var r = row
                r["build"] = marker?["build"] as? String ?? build
                r["was"] = marker?["state"] as? String ?? "?"
                r["since"] = marker?["at"] as? String ?? ""
                writeNow("crash", r, cat: "app")
                reported += 1
            }
            try? fm.removeItem(at: crashPending)
        }
        if reported > 0 { try? fm.removeItem(at: runMarker); return }

        // No record, but the marker survived: killed without a catchable signal.
        // Watchdog (a hung main thread), jetsam (memory), or the user swiping the
        // app away. `was` is what tells those apart.
        if let m = marker {
            var row: [String: Any] = [
                "was": m["state"] as? String ?? "?",
                "since": m["at"] as? String ?? "",
                "build": m["build"] as? String ?? "?",
                "last": String(cString: slLastEvent),
            ]
            // Whatever the last heartbeat chose to describe itself with — the
            // grant's age, the in-flight task count, the memory headroom. These
            // are the conditions AT DEATH, which no row written before it can be.
            for k in ["grant", "tasks", "mem", "memLow", "warns", "sess", "jobs",
                      "batt", "low"] where m[k] != nil { row[k] = m[k] }
            writeNow("prev-launch-dirty", row, cat: "app")
            try? fm.removeItem(at: runMarker)
        }
    }

    /// The pending file is line-oriented plain text because a signal handler can
    /// write nothing more structured. `key=value` lines up front, raw
    /// `backtrace_symbols_fd` output after them.
    private static func parseCrashRecord(_ record: String) -> [String: Any]? {
        var out: [String: Any] = [:]
        var stack: [String] = []
        for line in record.components(separatedBy: "\n") {
            let l = line.trimmingCharacters(in: .whitespaces)
            if l.isEmpty { continue }
            if let eq = l.firstIndex(of: "="), l.hasPrefix("kind=") || l.hasPrefix("sig=")
                || l.hasPrefix("name=") || l.hasPrefix("reason=") || l.hasPrefix("last=") {
                out[String(l[l.startIndex..<eq])] = String(l[l.index(after: eq)...])
            } else {
                stack.append(l)
            }
        }
        guard out["kind"] != nil else { return nil }
        // Frame 0 is always this handler; the interesting ones are shallow. Capped
        // because the whole transcript is uploaded over the LAN on every send.
        out["stack"] = String(stack.prefix(24).joined(separator: " | ").prefix(2400))
        return out
    }

    /// Like `write`, but the row is on disk before this returns. Only for the
    /// handful of rows that must survive whatever happens next.
    func writeNow(_ event: String, _ fields: [String: Any] = [:], cat: String = "play") {
        var row: [String: Any] = fields
        row["t"] = iso.string(from: Date())
        row["ev"] = event
        row["cat"] = cat
        slNoteEvent(event)
        q.sync {
            let safe = Self.sanitize(row)
            guard let data = try? JSONSerialization.data(withJSONObject: safe),
                  let line = String(data: data, encoding: .utf8) else { return }
            self.append(line + "\n")
        }
    }

    private func installCrashHandlers() {
        guard slCrashFD < 0 else { return }
        slCrashFD = open(crashPending.path, O_WRONLY | O_APPEND | O_CREAT, 0o644)
        guard slCrashFD >= 0 else { return }
        slCrashFrames = UnsafeMutablePointer<UnsafeMutableRawPointer?>
            .allocate(capacity: SL_CRASH_FRAMES)
        let msgs = UnsafeMutablePointer<UnsafePointer<CChar>?>
            .allocate(capacity: Int(SL_SIG_MAX))
        msgs.initialize(repeating: nil, count: Int(SL_SIG_MAX))
        slCrashMsgs = msgs
        slNoteEvent("launch")

        slCrashPendingPath = crashPending.path
        NSSetUncaughtExceptionHandler(slUncaughtExceptionHandler)

        // A Swift runtime trap (nil force-unwrap, array bounds, a failed `as!`)
        // is a SIGTRAP/SIGILL and never becomes an NSException, so these are not
        // redundant with the handler above.
        for (sig, label) in [(SIGSEGV, "SIGSEGV"), (SIGABRT, "SIGABRT"),
                             (SIGBUS, "SIGBUS"), (SIGILL, "SIGILL"),
                             (SIGFPE, "SIGFPE"), (SIGTRAP, "SIGTRAP")] {
            if let rendered = strdup("kind=signal\nsig=\(label)\n") {
                msgs[Int(sig)] = UnsafePointer(rendered)
            }
            signal(sig, slCrashSignalHandler)
        }
    }

    /// JSON-safe copy of a row. Non-finite doubles become a marker string rather
    /// than failing the encode — `CMTimeGetSeconds` returns NaN more often than
    /// anyone expects, and that is exactly when you want the row.
    private static func sanitize(_ row: [String: Any]) -> [String: Any] {
        var out: [String: Any] = [:]
        for (k, v) in row {
            switch v {
            case let d as Double:
                out[k] = d.isFinite ? d : "nan"
            case let f as Float:
                out[k] = f.isFinite ? Double(f) : "nan"
            case is String, is Int, is Bool:
                out[k] = v
            default:
                out[k] = JSONSerialization.isValidJSONObject([k: v]) ? v : String(describing: v)
            }
        }
        return out
    }

    private func append(_ line: String) {
        let fm = FileManager.default
        if !fm.fileExists(atPath: url.path) {
            try? line.data(using: .utf8)?.write(to: url)
            return
        }
        guard let h = try? FileHandle(forWritingTo: url) else { return }
        defer { try? h.close() }
        _ = try? h.seekToEnd()
        try? h.write(contentsOf: Data(line.utf8))
        // Halve the file when it gets large rather than deleting it: losing the
        // oldest half beats losing the incident that is still being written.
        // The SERVER keeps the rows this drops — it merges each upload into an
        // append-only transcript — so this cap is now a device-storage limit
        // rather than the retention policy it used to be.
        if let size = try? fm.attributesOfItem(atPath: url.path)[.size] as? Int,
           size > maxBytes {
            try? h.close()
            if let all = try? String(contentsOf: url, encoding: .utf8) {
                let lines = all.split(separator: "\n", omittingEmptySubsequences: false)
                let keep = lines.suffix(lines.count / 2).joined(separator: "\n")
                try? keep.data(using: .utf8)?.write(to: url)
            }
        }
    }

    func contents() -> String {
        q.sync { (try? String(contentsOf: url, encoding: .utf8)) ?? "" }
    }

    func clear() { q.sync { try? FileManager.default.removeItem(at: url) } }

    func byteCount() -> Int {
        q.sync {
            (try? FileManager.default.attributesOfItem(atPath: url.path)[.size] as? Int) as? Int ?? 0
        }
    }

    /// What Settings shows without making the user read the log.
    func stats() -> [String: Any] {
        let text = contents()
        let rows = text.split(separator: "\n", omittingEmptySubsequences: true)
        return ["bytes": text.utf8.count,
                "rows": rows.count,
                "first": Self.firstTimestamp(in: rows),
                "lastUpload": lastUpload,
                "lastUploadResult": lastUploadResult]
    }

    private static func firstTimestamp(in rows: [Substring]) -> String {
        guard let first = rows.first,
              let d = first.data(using: .utf8),
              let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any],
              let t = o["t"] as? String else { return "" }
        return t
    }

    /// POST the whole log to the host, which MERGES it into the transcript it
    /// already holds for this device.
    ///
    /// Sending the whole file every time is deliberate: the alternative — the
    /// device tracking what it has already sent — puts the bookkeeping on the
    /// side that gets force-quit, runs out of storage and has its Caches
    /// reclaimed. The server de-duplicates by exact row, so a re-send is free,
    /// and a phone that has been off the network for a week catches up in one
    /// tap.
    ///
    /// The response closes the clear handshake. A reader who is finished with a
    /// device's transcript clears it on the server, which cannot reach the
    /// phone; it parks the instruction and returns `clear_local` here, and we
    /// delete our copy so the next upload does not restore every cleared row.
    func upload(serverUrl: String, device: String, completion: @escaping (String) -> Void) {
        let body = contents()
        guard !body.isEmpty else { completion("nothing to send"); return }
        guard var comps = URLComponents(string: serverUrl) else { completion("bad server url"); return }
        comps.path = "/api/diag/client-log"
        comps.queryItems = [URLQueryItem(name: "device", value: device)]
        guard let url = comps.url else { completion("bad url"); return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("text/plain; charset=utf-8", forHTTPHeaderField: "Content-Type")
        req.httpBody = Data(body.utf8)
        req.timeoutInterval = 30
        URLSession.shared.dataTask(with: req) { [weak self] data, resp, err in
            guard let self = self else { return }
            let stamp = self.iso.string(from: Date())
            if let err = err {
                self.lastUpload = stamp
                self.lastUploadResult = "failed: \(err.localizedDescription)"
                completion(self.lastUploadResult)
                return
            }
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            guard code == 200 else {
                self.lastUpload = stamp
                self.lastUploadResult = "server said \(code)"
                completion(self.lastUploadResult)
                return
            }
            var added = -1, held = -1
            var clearLocal = false
            if let d = data,
               let o = try? JSONSerialization.jsonObject(with: d) as? [String: Any] {
                added = (o["added"] as? Int) ?? -1
                held = (o["rows"] as? Int) ?? -1
                clearLocal = (o["clear_local"] as? Bool) ?? false
            }
            if clearLocal {
                // The reader is done with these rows. Drop ours and start the
                // next transcript with a row saying why it begins here — an
                // unexplained gap in a log is worse than no gap.
                self.clear()
                self.write("log-cleared-by-server", ["device": device], cat: "app")
            }
            self.lastUpload = stamp
            self.lastUploadResult = added >= 0
                ? "sent; \(added) new row(s), \(held) on server" + (clearLocal ? "; device copy cleared" : "")
                : "sent \(body.utf8.count) bytes"
            completion(self.lastUploadResult)
        }.resume()
    }
}

// MARK: - External player view

/// A view whose BACKING layer is the `AVPlayerLayer`.
///
/// The first version added an `AVPlayerLayer` as a sublayer and set
/// `l.frame = root.bounds` once, at attach time — which reads the bounds at
/// exactly the wrong moment. The window carrying it is rebuilt from
/// `sceneDidConnect` while the app is BACKGROUNDED, where no layout pass runs, so
/// `bounds` is whatever it was at init; and a hand-added sublayer never resizes
/// afterwards either. Measured symptom: we held the display with `extLyr=Y`,
/// playback running, and the glasses showed black.
///
/// A backing layer cannot have the wrong size — UIKit sizes it with the view, and
/// the view is pinned to the window by its autoresizing mask.
final class ExternalPlayerView: UIView {
    override class var layerClass: AnyClass { AVPlayerLayer.self }
    var playerLayer: AVPlayerLayer { layer as! AVPlayerLayer }
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
    private var tcsObs: NSKeyValueObservation?
    /// When this app last ASKED for a transport change, and who asked.
    ///
    /// Without it, a pause that the app requested and a pause that happened TO
    /// the app are the same row. That distinction is the whole difference
    /// between "the user paused it" and "something took the audio session and
    /// nothing put it back", which is the 2026-09-23 stall.
    private var lastTransportRequest = (src: "", at: Date.distantPast)
    /// Our own window on the external display, and the layer inside it. See
    /// "External display surface".
    private var extWindow: UIWindow?
    private var extLayer: AVPlayerLayer?
    /// Presentation of last resort when there is no wired screen — AirPlay will
    /// not route video for a player that presents nowhere.
    private var mainLayer: AVPlayerLayer?
    private var bgTask: UIBackgroundTaskIdentifier = .invalid
    private var lastProgressPost = Date.distantPast
    private var lastProgressSkipLog = Date.distantPast
    /// Cross-device session heartbeat. 5 s rather than the web player's 2 s — the
    /// host reaps at 15 s, so three beats is still a comfortable margin, and this
    /// one runs with the screen off.
    private var lastSessionPost = Date.distantPast
    /// Set once a yield has been honoured, so a command that arrives again on a
    /// beat that crossed with our flush cannot stop a second session.
    private var yielded = false
    private var sessionActivated = false
    /// Last audio-session activation failure, surfaced in the diagnostics.
    private var audioSessionError = ""
    /// The header used to read `sessionActivated`, which stopNative() resets — so
    /// it printed INACTIVE however the handoff had actually gone. This one is
    /// sticky for the life of the process and answers the question that was
    /// being asked.
    private var audioEverActivated = false
    private var endedFlag = false
    private var handBackDeadline: DispatchWorkItem?
    /// An AirPlay session is up: the player's URLs point at AirPlayDoor, and the
    /// native player is the presentation exactly as it is on the glasses.
    private(set) var airplayOn = false
    /// The route actually engaged at least once this session. Until it has, a
    /// false `isExternalPlaybackActive` is "not picked yet", not "dropped".
    private var airplayEngaged = false
    private var airplayWatchdog: DispatchWorkItem?
    private var routePicker: AVRoutePickerView?

    /// Whether we are currently holding the idle timer open. See setAwake.
    private(set) var awakeOn = false

    var isNativeActive: Bool { player != nil }

    // MARK: Lifecycle

    func bootstrap() {
        // Before the launch row, so the previous run's death is reported ABOVE
        // the launch that found it. See "Crash forensics".
        DiagLog.shared.openRun(build: NP_BUILD)
        DiagLog.shared.write("launch", [
            "build": NP_BUILD,
            "ios": UIDevice.current.systemVersion,
            "model": UIDevice.current.model,
            // Sideloading can rewrite this, and several iOS APIs key off it —
            // BGTaskScheduler's permitted-identifier prefix most of all.
            "bundle": Bundle.main.bundleIdentifier ?? "?",
        ])
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
        // The SCENE is what we actually need, and it does not arrive with the
        // screen: 18.2.2's trail showed `scene=-` at a screenChange that reported
        // the display present, and `scene=Y` only seconds later. Worse, it was
        // absent again at the willResignActive where the window gets built — so
        // the handoff silently fell through to the route layer. Watch the scene's
        // own lifecycle rather than inferring it from the screen's.
        nc.addObserver(self, selector: #selector(sceneDidConnect),
                       name: UIScene.willConnectNotification, object: nil)
        nc.addObserver(self, selector: #selector(sceneDidDisconnect),
                       name: UIScene.didDisconnectNotification, object: nil)

        // NOBODY WAS WATCHING THE AUDIO SESSION. Measured 2026-09-23: the player
        // was handed off to the glasses, played for three seconds, and was found
        // paused at 5 s with `armPaused` flipped to true — and the log could not
        // say who paused it, because the only writer of that flag that logged
        // anything was the one that did not do it. An interruption (a call, Siri,
        // another app taking the session, WebKit starting its own <video>) pauses
        // an AVPlayer and tells the app through exactly this notification, which
        // we never subscribed to. So it paused, nothing resumed it, and nothing
        // recorded it.
        nc.addObserver(self, selector: #selector(audioInterruption),
                       name: AVAudioSession.interruptionNotification, object: nil)
        // The glasses are an audio route as well as a screen, and plugging them
        // in or out is a route change that can pause playback on its own
        // (`.oldDeviceUnavailable` is the classic "unplugged the headphones").
        nc.addObserver(self, selector: #selector(audioRouteChanged),
                       name: AVAudioSession.routeChangeNotification, object: nil)
        // The session being taken away outright — a second engine in this very
        // process is the likely candidate, and it is silent without this.
        nc.addObserver(self, selector: #selector(mediaServicesReset),
                       name: AVAudioSession.mediaServicesWereResetNotification, object: nil)
    }

    // MARK: Audio session

    @objc private func audioInterruption(_ note: Notification) {
        let info = note.userInfo ?? [:]
        let rawType = (info[AVAudioSessionInterruptionTypeKey] as? UInt) ?? 0
        let type = AVAudioSession.InterruptionType(rawValue: rawType)
        var row: [String: Any] = [
            "type": type == .began ? "began" : (type == .ended ? "ended" : "?"),
            "native": isNativeActive,
            "pos": armed.position,
            "armPaused": armed.paused,
            "title": armed.title,
        ]
        if #available(iOS 14.5, *),
           let r = info[AVAudioSessionInterruptionReasonKey] as? UInt {
            row["reason"] = r
        }
        if let opts = info[AVAudioSessionInterruptionOptionKey] as? UInt {
            row["shouldResume"] = AVAudioSession.InterruptionOptions(rawValue: opts)
                .contains(.shouldResume)
        }
        DiagLog.shared.write("interruption", row, cat: "app")

        guard type == .ended, isNativeActive else { return }
        // RESUME IS OURS TO DO. iOS hands back an interrupted session but does not
        // restart the player, and `shouldResume` is advisory — it is absent
        // whenever the interrupter did not say. Our own intent is the better
        // authority: if the user has not paused anything, we were playing, so
        // play. Reactivating first is required; the session went inactive with the
        // interruption and `play()` against a dead session does nothing silently.
        sessionActivated = false
        activateAudioSession()
        if !armed.paused {
            player?.play()
            DiagLog.shared.write("interruption-resumed", ["pos": armed.position], cat: "play")
        }
    }

    @objc private func audioRouteChanged(_ note: Notification) {
        let info = note.userInfo ?? [:]
        let rawReason = (info[AVAudioSessionRouteChangeReasonKey] as? UInt) ?? 0
        let reason = AVAudioSession.RouteChangeReason(rawValue: rawReason)
        let outs = AVAudioSession.sharedInstance().currentRoute.outputs
            .map { "\($0.portType.rawValue):\($0.portName)" }
            .joined(separator: ",")
        DiagLog.shared.write("route", [
            "reason": Self.routeReasonName(reason),
            "outputs": outs,
            "native": isNativeActive,
            "rate": Double(player?.rate ?? 0),
            "extScreen": externalScreen != nil,
        ], cat: "ext")
    }

    @objc private func mediaServicesReset(_ note: Notification) {
        DiagLog.shared.write("media-services-reset", ["native": isNativeActive], cat: "app")
        sessionActivated = false
        if isNativeActive { activateAudioSession() }
    }

    private static func routeReasonName(_ r: AVAudioSession.RouteChangeReason?) -> String {
        switch r {
        case .newDeviceAvailable:          return "new-device"
        case .oldDeviceUnavailable:        return "old-device-gone"
        case .categoryChange:              return "category-change"
        case .override:                    return "override"
        case .wakeFromSleep:               return "wake"
        case .noSuitableRouteForCategory:  return "no-route"
        case .routeConfigurationChange:    return "reconfigured"
        case .unknown:                     return "unknown"
        default:                           return "other"
        }
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
        a.offline        = call.getBool("offline") ?? false
        a.deviceId       = call.getString("deviceId") ?? ""
        a.deviceName     = call.getString("deviceName") ?? ""
        a.sessionProfileId = call.getString("sessionProfileId") ?? (call.getString("profileId") ?? "")
        a.sessionSource  = call.getString("sessionSource") ?? "server"
        a.canPrev        = call.getBool("canPrev") ?? false
        a.canNext        = call.getBool("canNext") ?? false
        a.nextUrl        = URL(string: call.getString("nextUrl") ?? "")
        a.nextTitle      = call.getString("nextTitle") ?? ""
        a.nextSeries     = call.getString("nextSeries")
        a.nextFilePath   = call.getString("nextFilePath") ?? ""
        a.nextItemId     = call.getString("nextItemId") ?? ""
        a.handoffEnabled = call.getBool("handoffEnabled") ?? true
        a.extMode        = call.getString("extMode") ?? "early"
        a.introStart       = call.getDouble("introStart") ?? -1
        a.introEnd         = call.getDouble("introEnd") ?? -1
        a.creditsStart     = call.getDouble("creditsStart") ?? -1
        a.autoSkipIntro    = call.getBool("autoSkipIntro") ?? false
        a.autoSkipCredits  = call.getBool("autoSkipCredits") ?? false
        a.introDone        = call.getBool("introDone") ?? false
        a.creditsDone      = call.getBool("creditsDone") ?? false
        a.nextIntroStart   = call.getDouble("nextIntroStart") ?? -1
        a.nextIntroEnd     = call.getDouble("nextIntroEnd") ?? -1
        a.nextCreditsStart = call.getDouble("nextCreditsStart") ?? -1
        a.armedAt        = Date()
        // The page arms with the URLs IT can reach (loopback / the box). While
        // AirPlay is up the receiver is fetching, so every URL the player could
        // load — this one or the next episode's — goes through the door.
        if airplayOn {
            if let u = a.url { a.url = AirPlayDoor.shared.lanURL(for: u) ?? u }
            if let u = a.nextUrl { a.nextUrl = AirPlayDoor.shared.lanURL(for: u) ?? u }
        }

        let wasActive = armed.active

        // Does this arm move us to a DIFFERENT file while our player is running?
        // Decided before anything is inherited, because most of what we inherit
        // from the outgoing episode is wrong for an incoming one.
        let switchingFile = isNativeActive
            && !armed.filePath.isEmpty
            && !a.filePath.isEmpty
            && a.filePath != armed.filePath

        // WHAT WE ALREADY SKIPPED, WE DO NOT OFFER TO SKIP AGAIN.
        //
        // The page keeps its own `skipDoneFor` and sends it on every arm, but
        // while the phone is locked its timers are frozen — so the arm that
        // lands the moment it wakes still says `introDone: false` for an intro we
        // skipped ten minutes and two episodes ago. Taking it at face value
        // re-arms a window the viewer has already been carried past. Within one
        // file the flags only ever go true, so OR them; a file switch is the one
        // thing that earns a clean slate, and it gets one below.
        if isNativeActive, !switchingFile, a.filePath == armed.filePath {
            a.introDone   = a.introDone   || armed.introDone
            a.creditsDone = a.creditsDone || armed.creditsDone
        }

        // While WE are the player, the page's `paused` is a stale echo of a web
        // element WebKit paused on our behalf — never a command. Taking it would
        // let a routine arm stop playback the user never touched. That holds
        // across a file switch too: pressing Next is not pressing Pause, so the
        // transport the user left running is what the new episode inherits.
        if isNativeActive {
            a.paused = armed.paused
            // The item knows its own duration better than a page whose element is
            // parked; never let an arm overwrite a good value with 0.
            //
            // But a duration is only lendable WITHIN one file. Measured
            // 2026-09-23: skipping E01 -> E02 -> E03 filed all three under E01's
            // 657.025 s, and E03 is 677.9 s long — so its progress was written
            // against a runtime it does not have. On a switch, leave it at 0 and
            // let adoptDuration() take it from the item that actually knows.
            if a.duration <= 0 && !switchingFile { a.duration = armed.duration }
        }

        // AN ARM CAN CHANGE WHICH FILE THIS IS, AND THAT IS A TEARDOWN.
        //
        // `armed` is both the handoff's configuration and the only record of what
        // the running player is playing. Replacing it wholesale is right for a
        // routine re-arm of the same file; when the page has moved to a DIFFERENT
        // file while our player is still going, it silently retires the outgoing
        // episode with no final write — every second since the 15 s throttle last
        // let a post through simply has nowhere to go afterwards.
        //
        // Measured 2026-09-23 (client_iPhone-app.log). E04 was playing natively on
        // the glasses, last posted at 651 s of 690. The page auto-advanced; the arm
        // for E05 landed while E04's player was still up. Twenty seconds later the
        // teardown flushed — and flushed E05's identity at E05's position (2.2 s),
        // which the near-start guard then correctly dropped. E04's last 39 seconds
        // were never written by anyone, and the row that should have said so said
        // "near-start" instead.
        //
        // So flush the OUTGOING file first, while `armed` still describes it.
        if switchingFile {
            let outgoing = armed.filePath
            maybePostProgress(armed.position, force: true)
            DiagLog.shared.write("rearm-swap", [
                "from": outgoing, "to": a.filePath,
                "flushedAt": armed.position, "dur": armed.duration,
                // The native player is still on the OLD file at this point —
                // swapNativeItem() below is what moves it. A `rearm-swap` with no
                // `native-swap` after it means the player kept the old episode.
                "nativePos": player.map { CMTimeGetSeconds($0.currentTime()) } ?? -1,
            ], cat: "play")
        }
        armed = a

        // AND NOW MOVE THE PLAYER, NOT JUST THE PAPERWORK.
        //
        // Swapping `armed` alone leaves the config and the running player
        // describing different episodes: the monitor keeps playing the old file
        // while every readout, the Now Playing entry and every progress POST is
        // filed under the new one.
        //
        // Measured 2026-09-23 (client iPhone-app log). Glasses connected, Next
        // pressed twice. Both times `rearm-swap` fired and nothing else — no
        // `startNative`, no `native-advanced`. E01 played on the display
        // throughout; 114 s of it was written to E02's progress and 131 s to
        // E03's, which is why the next Resume opened an episode the user had
        // never watched.
        //
        // The page does call `takeover` on this path, and it could never have
        // helped: takeover routes to startNative(), which guards on
        // `!isNativeActive` and so is a no-op in exactly this case. The
        // end-of-episode advance had a working in-place path (itemDidEnd ->
        // replaceItem); a user-driven skip reached none of it.
        if switchingFile {
            if let url = a.url {
                swapNativeItem(to: url, at: a.position, play: !a.paused)
            } else {
                // No URL means the page armed a file the native side cannot play
                // (no master), and the running player is now presenting an
                // episode nothing agrees it is playing. Nothing here can fix it;
                // say so, because the alternative is a silent wrong-episode.
                DiagLog.shared.write("swap-no-url", [
                    "to": a.filePath, "item": a.itemId,
                ], cat: "play")
            }
        }

        // A fresh session can be yielded again. Without this, one takeover would
        // leave the flag set for the life of the process and every later session
        // on this device would refuse to hand over.
        if a.active && !wasActive { yielded = false; lastSessionPost = .distantPast }

        // The Live Activity must be REQUESTED while foreground — a request from
        // a background handler is unreliable. So it starts here, on the first
        // arm of a session, not at handoff time. iOS surfaces it in the Island
        // once the app is minimised (same behaviour TVRemote relies on).
        if a.active && !wasActive {
            PlaybackLiveActivity.shared.start(state: liveActivityState())
        } else if a.active {
            PlaybackLiveActivity.shared.update(state: liveActivityState(), force: false)
        } else if wasActive {
            PlaybackLiveActivity.shared.end("disarm")
        }

        // Claim the audio session while we are still FOREGROUND. This is the fix
        // for "no audio after locking unless I press play".
        //
        // iOS lets a backgrounded app CONTINUE audio under the `audio` background
        // mode; it does not generally let one START audio from the background with
        // a session it did not already hold. startNative() runs from
        // didEnterBackgroundNotification, so activating there was exactly the
        // restricted case: setActive(true) failed, play() did nothing, and `try?`
        // swallowed the error so it never surfaced. Pressing play on the lock
        // screen worked because a remote command is user-initiated — which is
        // precisely the shape of the symptom.
        //
        // The old comment's concern was that `.playback` is process-wide and
        // activating it at LAUNCH would make every WKWebView sound ignore the
        // ringer switch. Still true, and still respected: this fires only once an
        // episode is actually armed with the handoff enabled, and stopNative()
        // deactivates it again when playback ends.
        if armed.active, armed.handoffEnabled { activateAudioSession() }

        // Early mode takes the display now, while the app can still draw and the
        // scene is live. Idempotent, so riding the arm push is enough — no extra
        // JS surface, and it self-heals if the glasses are plugged in mid-episode.
        //
        // The scene itself only exists if we asked for it (iOS 27 scene accessory;
        // see ExternalDisplayAccessory). Offer it in Early mode, withdraw it in
        // Mirrored, so the route's takeover has nothing of ours to fight. If this
        // arm is what enables it, the scene lands later and sceneDidConnect claims.
        let wantScene = armed.extMode != "route"
        onMain { ExternalDisplayAccessory.setEnabled(wantScene) }
        maybeClaimEarly()
    }

    /// Idempotent. Records why it failed rather than swallowing it: a silent
    /// `try?` here is what hid this bug for the life of the feature.
    private func activateAudioSession() {
        guard !sessionActivated else { return }
        let s = AVAudioSession.sharedInstance()
        do {
            try s.setCategory(.playback, mode: .moviePlayback)
            try s.setActive(true)
            sessionActivated = true
            audioSessionError = ""
            audioEverActivated = true
        } catch {
            audioSessionError = "\(Self.stateName(UIApplication.shared.applicationState)): \(error.localizedDescription)"
            DiagLog.shared.write("audio-session-failed", ["reason": audioSessionError], cat: "app")
        }
    }

    func tick(position: Double, paused: Bool, duration: Double) {
        guard armed.active else { return }
        // Same guard as arm(), now extended to the POSITION — which was always
        // the same mistake and was simply not noticed, because `paused` broke
        // loudly and this breaks quietly. While native holds the display the
        // page's <video> is parked at wherever the handoff left it, so a tick
        // from it drags `armed.position` back there once a second; the teardown
        // flush then writes that stale position as the episode's final one, and
        // a seek made through the native player is undone within a second.
        if !isNativeActive {
            // `armedAt` is the timestamp OF `position` — extrapolatedPosition()
            // multiplies the gap between them by the rate — so the two move
            // together or not at all. Refreshing the stamp while holding the
            // position would claim a fresh sample of a stale playhead.
            armed.position = position
            armed.paused = paused
            armed.armedAt = Date()
        }
        // The page may still contribute a duration, but never OVER a good one
        // the item reported itself: the element's and the item's differ in the
        // last decimal (measured: 690.147 vs 690.1477694), so an unguarded write
        // made the value flap once a second for no reason. Only fill a gap.
        if duration > 0, !isNativeActive || armed.duration <= 0 {
            armed.duration = duration
        }
        if !isNativeActive {
            PlaybackLiveActivity.shared.update(state: liveActivityState(), force: false)
        }
    }

    /// Drive the native player directly. Only meaningful while it is the
    /// presentation; a no-op otherwise, so the page can call it unconditionally.
    func setPaused(_ paused: Bool, source: String = "unknown") {
        guard let p = player else { return }
        // WHO ASKED. `armed.paused` is the flag the whole handoff reads, and for
        // the life of the feature nothing recorded who set it — so a player found
        // paused could not be told apart from a player the user paused. The
        // 2026-09-23 stall is unexplainable for exactly this reason. Every caller
        // now names itself.
        lastTransportRequest = (src: source, at: Date())
        DiagLog.shared.write("setPaused", [
            "paused": paused, "src": source, "was": armed.paused,
            "pos": armed.position, "title": armed.title,
        ], cat: "play")
        armed.paused = paused
        if paused { p.pause() } else { p.play() }
        updateNowPlaying()
        PlaybackLiveActivity.shared.update(state: liveActivityState(), force: true)
    }

    /// Seek the native player. The page's seek bar and ±10 s buttons route here
    /// while native holds the display; acting on the parked <video> would move a
    /// player nobody is watching.
    func seekTo(_ t: Double) {
        guard let p = player else { return }
        let clamped = armed.duration > 1 ? min(max(t, 0), armed.duration - 0.5) : max(t, 0)
        armed.position = clamped
        armed.armedAt = Date()
        p.seek(to: CMTime(seconds: clamped, preferredTimescale: 600),
               toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            guard let self = self else { return }
            if !self.armed.paused { p.play() }
            self.updateNowPlaying()
            self.maybePostProgress(clamped, force: true)
        }
    }

    /// True while the native player is the presentation and the page must not
    /// touch its own element.
    var isHolding: Bool { isNativeActive && (extWindow != nil || airplayOn) }

    func disarm(reason: String = "unspecified") {
        // ORDER IS LOAD-BEARING: flush, then stop, then wipe.
        //
        // This used to reset `armed` as its FIRST statement, which quietly made
        // it the only teardown path that saves nothing. The hand-back deadline
        // and reclaim() both post a final forced progress before stopNative();
        // this one wiped itemId/filePath/serverUrl/duration first, so the last
        // up-to-15 s of every episode — everything since the throttle last let a
        // post through — had nowhere to go. Worse, `lpUnloadCurrent` calls it on
        // a normal episode ADVANCE, so it fired on every file change.
        //
        // Measured on 2026-09-22 (client_iPhone-app.log): stopNative logged
        // `title:"" pos:0`, and 13 ms later a trailing observer tick reported a
        // real position of 322.26 s against an all-MISSING guard and dropped it.
        let hadPlayer = isNativeActive
        if hadPlayer { maybePostProgress(armed.position, force: true) }
        DiagLog.shared.write("disarm", ["reason": reason,
                                        "pos": armed.position,
                                        "title": armed.title,
                                        "native": hadPlayer,
                                        "flushed": hadPlayer], cat: "play")
        stopNative(endActivity: true)   // reads armed.title/position for its own row
        armed = ArmedPlayback()
        // Deliberately does NOT exit TV Mode. The two are orthogonal: disarm
        // fires on every file teardown (including a normal episode advance,
        // which briefly has no armed URL), and dropping the blackout there would
        // flash the phone's screen back on mid-episode. Only lpStop() — real
        // "playback is over" — exits TV Mode.
    }

    /// The page is being replaced (a reload, or the recovery load after its
    /// content process was jettisoned). Whatever it armed, the page that comes
    /// up next knows nothing about, and nothing will ever disarm it — the
    /// orphan then waits for the next display connect to be claimed onto the
    /// glasses with no controls behind it. Measured 2026-09-24, see the
    /// hand-back deadline above.
    ///
    /// Only an IDLE arm is dropped. A player that is running is the one thing
    /// the viewer can see, and killing it because WebKit reloaded a page behind
    /// a locked phone would stop the episode they are watching; the page's own
    /// boot check (`_npReconcileOrphan`) decides about that one, and the row
    /// here says it happened.
    func pageWillLoad() {
        guard armed.active else { return }
        if isNativeActive {
            DiagLog.shared.write("orphan-native", ["why": "page-load",
                                                   "pos": armed.position,
                                                   "paused": armed.paused,
                                                   "holding": isHolding,
                                                   "title": armed.title], cat: "play")
            return
        }
        disarm(reason: "page-load")
    }

    // MARK: Handoff in

    @objc private func appDidEnterBackground() {
        DiagLog.shared.noteAppState("bg")
        if isNativeActive { diagSnap("background"); scheduleBackgroundSnaps() }
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

        // Normally already done at arm time, while foreground — see
        // activateAudioSession(). Kept here as a fallback for the paths that reach
        // startNative without an arm (takeover from a remote command).
        activateAudioSession()

        endedFlag = false
        // THE DECISION IS MADE HERE, NOT IN THE SEEK COMPLETION.
        // `p.seek` is asynchronous, and arms keep arriving while it runs. Measured:
        // armPaused=- at background/attached, then armPaused=Y by locked+3s — a
        // late arm flipped it mid-seek, the completion's `if !armed.paused` read
        // the new value, play() was skipped, and the player sat paused at a
        // perfectly correct position. Capture intent at the instant of handoff.
        let shouldPlay = !armed.paused
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
        // `takeover` reaches startNative off the main thread, and the trail is
        // main-only. Same queue as attachVideoSurface's own hop, so it still
        // lands after the surface work.
        onMain { [weak self] in self?.diagSnap("background/attached") }
        scheduleLockedSnaps()

        // WRITTEN HERE, NOT AT THE END OF THIS FUNCTION. It used to be the last
        // statement, which made it a report that the whole handoff succeeded —
        // and therefore useless for the one question a transcript most needs to
        // answer, "how far did we get?". Measured 2026-09-23: the app died with
        // `snap at:"background/attached"` as its final row and no `startNative`,
        // which narrowed the fault to the ten lines below but could not say
        // whether it was those lines or a row the queue never flushed. The row
        // now means "the player exists and the surface is attached"; everything
        // after it is separately visible through the observers it installs.
        DiagLog.shared.write("startNative", ["reason": reason, "at": startAt,
                                             "shouldPlay": shouldPlay,
                                             "title": armed.title,
                                             "extWindow": extWindow != nil], cat: "play")

        statusObs = it.observe(\.status, options: [.new]) { [weak self] obs, _ in
            guard let self = self, obs.status == .readyToPlay else { return }
            self.adoptDuration(from: obs)
            self.applyTrackSelection(on: obs)
            self.seekAndPlay(to: startAt, play: shouldPlay)
        }
        externalObs = p.observe(\.isExternalPlaybackActive, options: [.new]) { [weak self] pl, _ in
            guard let self = self else { return }
            // KVO lands on whatever queue flipped it; airplay state is main-only.
            let ext = pl.isExternalPlaybackActive
            self.onMain { self.airplayRouteChanged(ext) }
            self.emit("displayChanged", self.displayInfo())
            PlaybackLiveActivity.shared.update(state: self.liveActivityState(), force: true)
        }
        // EVERY transport change, whether or not we asked for it.
        //
        // The 1 Hz time observer already mirrors the transport into
        // `armed.paused`, which means a pause that happened TO us is adopted as
        // if it were the user's wish — and once adopted, nothing ever resumes:
        // the page's intent flag only clears when its own element is visible and
        // playing, which in early mode it never is. Measured 2026-09-23: E05
        // started on the glasses, played for three seconds, and was paused at
        // ~5 s; twenty rows of `armPaused: true` later it was still sitting
        // there. Nothing in the log named a cause because nothing watched this.
        tcsObs = p.observe(\.timeControlStatus, options: [.new]) { [weak self] pl, _ in
            guard let self = self else { return }
            let requested = Date().timeIntervalSince(self.lastTransportRequest.at) < 1.5
            var row: [String: Any] = [
                "tcs": Self.tcsName(pl.timeControlStatus),
                "rate": Double(pl.rate),
                "pos": pl.currentTime().seconds,
                "requested": requested,
                "by": requested ? self.lastTransportRequest.src : "",
                "armPaused": self.armed.paused,
                "waitReason": (pl.reasonForWaitingToPlay?.rawValue as String?) ?? "",
                "itemStatus": Self.itemStatusName(pl.currentItem?.status),
                "itemErr": pl.currentItem?.error?.localizedDescription ?? "",
                "sess": self.sessionActivated,
                "extWindow": self.extWindow != nil,
            ]
            // The unrequested STOP is the interesting one and it deserves its own
            // event name, so a reader can pull just these out of a day of rows.
            let stalled = pl.timeControlStatus == .paused && !requested && !self.armed.paused
            if stalled {
                row["route"] = AVAudioSession.sharedInstance().currentRoute.outputs
                    .map { $0.portType.rawValue }.joined(separator: ",")
            }
            DiagLog.shared.write(stalled ? "transport-stopped-itself" : "transport",
                                 row, cat: "play")
        }

        NotificationCenter.default.addObserver(
            self, selector: #selector(itemDidEnd(_:)),
            name: .AVPlayerItemDidPlayToEndTime, object: it)
        // A file that cannot finish, and a file that keeps running dry, both look
        // like "it froze" and neither said anything before.
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemFailedToEnd(_:)),
            name: .AVPlayerItemFailedToPlayToEndTime, object: it)
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemStalled(_:)),
            name: .AVPlayerItemPlaybackStalled, object: it)

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

    /// Take the duration from the item itself.
    ///
    /// It used to come only from the page (`_npPayload`), and every progress POST
    /// is gated on `armed.duration > 0`. In early mode the handoff happens the
    /// instant playback starts, when the `<video>` often has no duration yet — so
    /// `armed.duration` stayed 0, `maybePostProgress` returned at its first guard
    /// every time, and **nothing was ever saved**. `itemDidEnd` then posted
    /// `armed.duration` (0) and failed the `t >= 5` guard too, so completion was
    /// lost as well. `_npTick` would have repaired it, but it rides
    /// `_lpClockTick`, which does not run while the element is parked.
    ///
    /// Also fixes the advance case: `replaceItem` never updated duration at all,
    /// so every episode after the first inherited the wrong one.
    private func adoptDuration(from it: AVPlayerItem) {
        let d = CMTimeGetSeconds(it.duration)
        if d.isFinite, d > 0 { armed.duration = d }
    }

    private func seekAndPlay(to t: Double, play shouldPlay: Bool) {
        guard let p = player else { return }
        let target = CMTime(seconds: t, preferredTimescale: 600)
        p.seek(to: target, toleranceBefore: .zero, toleranceAfter: .zero) { [weak self] _ in
            guard let self = self else { return }
            if shouldPlay { p.play() }
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
            // removeTimeObserver does not cancel blocks already queued on .main,
            // so one more tick can land AFTER teardown. Running it would report
            // a live position against a wiped `armed` — which is exactly the
            // all-MISSING progress-skipped row that hid the disarm bug.
            guard self.isNativeActive else { return }
            let t = time.seconds
            guard t.isFinite else { return }
            self.armed.position = t
            self.armed.armedAt = Date()
            if let it = p.currentItem { self.adoptDuration(from: it) }
            // `.waitingToPlayAtSpecifiedRate` is the player TRYING to play, not a
            // pause — mirroring it as one made `armed.paused` flap true for a
            // sample every time a bundle segment ran the buffer down. Harmless
            // while it only fed a readout; not harmless now that an arm inherits
            // it (`a.paused = armed.paused`) and a file switch starts the new item
            // with `play: !a.paused`, because a skip is exactly when the player is
            // most likely to be buffering. Only a real stop is a pause.
            //
            // AND NOT BEFORE THE ITEM IS READY. Until `.readyToPlay` we have not
            // issued play() at all — that happens in the status observer — so the
            // player really is `.paused`, and it means nothing about intent. This
            // window is not brief: measured 2026-09-23, a host-streamed bundle sat
            // `itemStatus=unknown` for six seconds after the handoff, during which
            // one observer tick turned `armed.paused` true. `seekAndPlay` survived
            // it because `shouldPlay` is captured at handoff time (see
            // startNative) — but nothing else was defended. In the same log an
            // `interruption` with `type: ended` landed inside the window, and
            // `audioInterruption`'s `if !armed.paused { play() }` therefore
            // declined to resume; playback only continued because the deferred
            // seekAndPlay was still coming. A real interruption there — a call
            // ending — would have left the glasses silent.
            if p.currentItem?.status == .readyToPlay {
                self.armed.paused = (p.timeControlStatus == .paused)
            }
            self.updateNowPlaying()
            PlaybackLiveActivity.shared.update(state: self.liveActivityState(), force: false)
            // Push the transport to the page. While native holds the display the
            // page's own element is parked, so this is the ONLY source for its
            // clock and seek bar. An event, not a poll: nativeStarted already
            // proves this channel works, and a 1 Hz state() round-trip is both
            // slower and one more thing to be wrong.
            self.emit("nativeProgress", ["position": t,
                                         "duration": self.armed.duration,
                                         "paused": p.timeControlStatus != .playing])
            self.maybePostProgress(t)
            self.maybePostSession(t)
            // LAST, and deliberately after the progress write: a credits skip
            // re-points `armed` at the next episode, and a post issued after
            // that would file this episode's position under the next one's path.
            self.maybeAutoSkip(t)
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
              armed.offline || !armed.serverUrl.isEmpty else {
            // The duration-0 bug was invisible for a day because this guard is
            // silent. Log the refusal, throttled so a stopped player cannot flood.
            if force || Date().timeIntervalSince(lastProgressSkipLog) >= 60 {
                lastProgressSkipLog = Date()
                // Name the guard that actually fired rather than making a reader
                // re-derive it from five fields. `near-start` in particular is
                // correct behaviour (it mirrors saveProgress's near-zero guard),
                // so it must not read as a failure — which it did when the row
                // only showed "everything else looks ok".
                let why: String = t < 5 ? "near-start"
                    : armed.duration <= 0 ? "duration-0"
                    : armed.itemId.isEmpty ? "no-item"
                    : armed.filePath.isEmpty ? "no-file"
                    : "no-server"
                DiagLog.shared.write("progress-skipped", [
                    "why": why,
                    "pos": t, "dur": armed.duration,
                    "item": armed.itemId.isEmpty ? "MISSING" : "ok",
                    "file": armed.filePath.isEmpty ? "MISSING" : "ok",
                    "server": armed.serverUrl.isEmpty ? "MISSING" : "ok",
                    // Whether a player was still up separates the two cases that
                    // look identical in the log: a refusal during playback (a
                    // bug, something never got armed) from one after teardown
                    // (harmless, the state is meant to be gone).
                    "native": isNativeActive,
                    "final": force,
                ], cat: "net")
            }
            return
        }
        guard force || Date().timeIntervalSince(lastProgressPost) >= 15 else { return }
        lastProgressPost = Date()

        if armed.offline {
            // Same record, same accrual rule (watchrule) the page's own offline
            // save uses; the page's reconnect sync pushes it. nil track picks
            // leave the page's stored picks untouched.
            let a = armed
            DispatchQueue.global(qos: .utility).async {
                OfflineProgressStore.shared.saveProgress(
                    profileId: a.profileId, itemId: a.itemId, filePath: a.filePath,
                    positionSec: t, durationSec: a.duration,
                    subtitleSel: nil, audioSel: nil,
                    localAudioIdx: nil, localSubtitleIdx: nil)
                DiagLog.shared.write("progress-local", [
                    "pos": t, "dur": a.duration, "item": a.itemId,
                    "file": a.filePath, "final": force,
                ], cat: "offline")
            }
            return
        }

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
        // `final` marks the one post that matters most when reading a log: the
        // forced flush at teardown. The routine 15 s beats are interchangeable;
        // if this one is missing or non-200, that episode lost its tail.
        let logged: [String: Any] = ["pos": t, "dur": armed.duration,
                                    "item": armed.itemId, "profile": armed.profileId,
                                    "file": armed.filePath, "final": force]
        send(req, logged: logged, retriesLeft: force ? 3 : 0, attempt: 1)
    }

    /// POST a progress write, retrying a FORCED one.
    ///
    /// "Best-effort, but no longer silent" was an improvement on silent, and the
    /// log it produced promptly showed why it is not enough: of twenty progress
    /// posts in one day, three came back `The request timed out` or `The network
    /// connection was lost`, and two of those were the FINAL flush of an episode.
    /// A routine beat that fails is replaced by the next one fifteen seconds
    /// later; a final flush has no successor, so the failure is simply the tail
    /// of that episode going missing.
    ///
    /// Wi-Fi on a phone that is being locked, unlocked and plugged into a pair of
    /// glasses drops constantly, and it comes back within seconds — so the retry
    /// is short and stubborn rather than clever. Only forced posts retry: a
    /// stale routine beat arriving late would be worse than not arriving.
    private func send(_ req: URLRequest, logged: [String: Any],
                      retriesLeft: Int, attempt: Int) {
        URLSession.shared.dataTask(with: req) { [weak self] _, resp, err in
            var row = logged
            let code = (resp as? HTTPURLResponse)?.statusCode ?? 0
            row["status"] = code
            row["try"] = attempt
            if let err = err { row["err"] = err.localizedDescription }
            let ok = (200...299).contains(code)
            row["retrying"] = !ok && retriesLeft > 0
            DiagLog.shared.write("progress", row, cat: "net")
            guard !ok, retriesLeft > 0, let self = self else { return }
            // 2 s, 6 s, 18 s. Long enough to outlast a lock/unlock or a route
            // change, short enough that the app is still alive to finish it —
            // the background task assertion the handoff holds covers the window.
            let delay = pow(3.0, Double(attempt - 1)) * 2.0
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) {
                self.send(req, logged: logged,
                          retriesLeft: retriesLeft - 1, attempt: attempt + 1)
            }
        }.resume()
    }

    // MARK: Cross-device playback session

    /// Keep this device visible in the household's "playing elsewhere" banner,
    /// and listen for a request to hand playback over.
    ///
    /// Both halves have to live here rather than in JS. A backgrounded WKWebView
    /// has its timers frozen and its EventSource dead, so the web player can
    /// neither beat nor hear an SSE `playback_command` — a phone in a pocket
    /// would silently disappear from every other device's banner, and a yield
    /// broadcast at it would go nowhere. We are already posting, so the answer to
    /// our own POST is the one channel that always arrives.
    private func maybePostSession(_ t: Double, force: Bool = false) {
        guard !armed.deviceId.isEmpty, !armed.serverUrl.isEmpty,
              !armed.itemId.isEmpty, !armed.filePath.isEmpty, !yielded else { return }
        guard force || Date().timeIntervalSince(lastSessionPost) >= 5 else { return }
        lastSessionPost = Date()

        guard let base = URL(string: armed.serverUrl),
              let url = URL(string: "/api/playback/session", relativeTo: base)
        else { return }
        var req = URLRequest(url: url)
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if !armed.token.isEmpty { req.setValue(armed.token, forHTTPHeaderField: "X-Device-Token") }
        req.timeoutInterval = 8
        let paused = (player?.timeControlStatus != .playing)
        // No continuity payload: the web player sent its playlist / shuffle on the
        // beat that opened this session and the host keeps the last value it was
        // given, so a pull still gets the whole run. Nothing about it can change
        // while the phone is locked — an auto-advance moves the file, which the
        // fields below carry, not the run itself.
        req.httpBody = try? JSONSerialization.data(withJSONObject: [
            "device_id":    armed.deviceId,
            "device_name":  armed.deviceName,
            "profile_id":   armed.sessionProfileId.isEmpty ? armed.profileId : armed.sessionProfileId,
            "active":       true,
            "item_id":      armed.itemId,
            "file_path":    armed.filePath,
            "title":        armed.title,
            "position_sec": t,
            "duration_sec": armed.duration,
            "playback":     paused ? "paused" : "playing",
            "source":       armed.sessionSource,
        ])
        URLSession.shared.dataTask(with: req) { [weak self] data, _, _ in
            guard let self = self, let data = data,
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  (obj["yield"] as? Bool) == true else { return }
            let to = (obj["yield_to"] as? String) ?? "another device"
            self.onMain { self.honourYield(to: to) }
        }.resume()
    }

    /// Hand the session over: flush the exact playhead, stop, tell JS.
    ///
    /// The flush is what the puller is blocked on (up to 2.5 s server-side) — it
    /// is the difference between the other device resuming on this frame and
    /// resuming up to a beat behind. Progress is written first and forced, so the
    /// host's own resume position is correct even if the other device never
    /// actually starts.
    private func honourYield(to: String) {
        guard !yielded, !armed.deviceId.isEmpty, !armed.serverUrl.isEmpty else { return }
        yielded = true
        let t = extrapolatedPosition()
        maybePostProgress(t, force: true)

        if let base = URL(string: armed.serverUrl),
           let url = URL(string: "/api/playback/session/\(armed.deviceId)/yield", relativeTo: base) {
            var req = URLRequest(url: url)
            req.httpMethod = "POST"
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
            if !armed.token.isEmpty { req.setValue(armed.token, forHTTPHeaderField: "X-Device-Token") }
            req.timeoutInterval = 8
            req.httpBody = try? JSONSerialization.data(withJSONObject: [
                "device_id":    armed.deviceId,
                "position_sec": t,
                "duration_sec": armed.duration,
            ])
            URLSession.shared.dataTask(with: req).resume()
        }
        // Stop WITHOUT clearing `armed` — the JS side is about to run lpStop(),
        // and disarming here first would race it into a hand-back against a
        // player that no longer exists.
        stopNative(endActivity: true)
        emit("nativeYielded", ["to": to, "filePath": armed.filePath])
    }

    // MARK: End of file / auto-advance

    @objc private func itemFailedToEnd(_ note: Notification) {
        let err = note.userInfo?[AVPlayerItemFailedToPlayToEndTimeErrorKey] as? Error
        DiagLog.shared.write("item-failed", [
            "err": err?.localizedDescription ?? "unknown",
            "file": armed.filePath, "pos": armed.position,
        ], cat: "play")
    }

    @objc private func itemStalled(_ note: Notification) {
        DiagLog.shared.write("item-stalled", [
            "pos": armed.position, "file": armed.filePath,
            "likely": player?.currentItem?.isPlaybackLikelyToKeepUp ?? false,
            "waitReason": (player?.reasonForWaitingToPlay?.rawValue as String?) ?? "",
        ], cat: "play")
    }

    @objc private func itemDidEnd(_ note: Notification) {
        maybePostProgress(armed.duration, force: true)

        // Advance in place if the web player armed a next episode — it already
        // preps it (_lpWarmNextEp), so this costs no extra host round-trip.
        if advanceToNext(reason: "ended") { return }
        endedFlag = true
        DiagLog.shared.write("ended", ["file": armed.filePath, "dur": armed.duration,
                                       "hadNext": false], cat: "play")
        emit("nativeEnded", ["filePath": armed.filePath])
        stopNative(endActivity: true)
    }

    /// Move to the armed next episode without letting go of anything.
    ///
    /// Two callers reach this and they differ only in how they got here: the
    /// natural end of an item, and a credits auto-skip. Returns false when
    /// nothing is armed to advance into, which is a real and common state — a
    /// fully downloaded local bundle still cannot be armed (see
    /// `next-arm-skipped` in the log), and the last episode of a run never has a
    /// next at all.
    @discardableResult
    private func advanceToNext(reason: String) -> Bool {
        guard let next = armed.nextUrl else { return false }
        armed.position = 0
        armed.armedAt = Date()
        armed.title = armed.nextTitle
        // The small line carries the episode code now, so it moves too.
        if let ns = armed.nextSeries { armed.series = ns }
        armed.filePath = armed.nextFilePath
        if !armed.nextItemId.isEmpty { armed.itemId = armed.nextItemId }
        armed.url = next
        armed.nextUrl = nil
        // A NEW FILE GETS THE NEW FILE'S WINDOWS. The page armed them alongside
        // nextUrl precisely so this moment doesn't need it — it may be frozen,
        // and an episode advanced into with the phone locked would otherwise
        // carry the PREVIOUS episode's intro window and seek into the middle of
        // a cold open. Promote, then clear, then let the page correct us when it
        // next wakes.
        armed.introStart   = armed.nextIntroStart
        armed.introEnd     = armed.nextIntroEnd
        armed.creditsStart = armed.nextCreditsStart
        armed.nextIntroStart = -1
        armed.nextIntroEnd = -1
        armed.nextCreditsStart = -1
        armed.introDone = false
        armed.creditsDone = false
        DiagLog.shared.write("advance", ["to": armed.filePath, "item": armed.itemId,
                                         "reason": reason,
                                         "introEnd": armed.introEnd,
                                         "creditsAt": armed.creditsStart], cat: "play")
        emit("nativeAdvanced", ["filePath": armed.filePath, "url": next.absoluteString])
        replaceItem(with: next)
        return true
    }

    // MARK: Auto-skip

    /// Mirrors LP_SKIP_INTRO_START_PAD / LP_SKIP_INTRO_PAD in static/index.html
    /// and SKIP_INTRO_START_PAD_SEC / SKIP_INTRO_PAD_SEC in main.py. Three copies
    /// of two numbers now; change one, change all three.
    private static let skipIntroStartPad = 1.5
    private static let skipIntroPad = 0.25

    /// Fire the profile's auto-skip while WE are the player.
    ///
    /// The page keeps the visible offer tile and its countdown — it is the only
    /// surface that has one — but it must not also fire, or the same skip is
    /// seeked twice. See `remote` in lpEvaluateSkipOffer.
    private func maybeAutoSkip(_ t: Double) {
        // Not before the item is ready, and not while paused. The first is the
        // 18.11.1 rule applied to a second reader of the transport: before
        // `.readyToPlay` the position is not yet the position. The second is
        // manners — someone who paused inside the intro did not ask to be moved.
        guard isNativeActive, !armed.paused,
              player?.currentItem?.status == .readyToPlay else { return }

        if armed.autoSkipIntro, !armed.introDone,
           armed.introStart >= 0, armed.introEnd > armed.introStart {
            // Fire a beat AFTER the detected start, like the page and VLC: late
            // costs a moment of theme, early cuts real content. And only while
            // there is more than a second of intro left — past that a "skip" is
            // just a jolt.
            let at = min(armed.introStart + Self.skipIntroStartPad, armed.introEnd)
            if t >= at, armed.introEnd - t > 1 {
                armed.introDone = true
                let to = armed.introEnd + Self.skipIntroPad
                DiagLog.shared.write("auto-skip", ["type": "intro", "from": t, "to": to,
                                                   "file": armed.filePath], cat: "play")
                emit("nativeSkipped", ["type": "intro", "position": to,
                                       "filePath": armed.filePath])
                seekTo(to)
                return
            }
        }

        if armed.autoSkipCredits, !armed.creditsDone,
           armed.creditsStart > 0, t >= armed.creditsStart {
            // NOTHING TO ADVANCE INTO IS NOT A REASON TO STOP. The page's
            // equivalent ends the session here, which is fine on a screen
            // someone is looking at and wrong with the phone in a pocket: the
            // glasses would simply go dark mid-credits with no way to ask why.
            // Let it play out — itemDidEnd owns the real end.
            guard armed.nextUrl != nil else { return }
            armed.creditsDone = true
            // Credits reached counts as watched, exactly as the page's
            // _lpAdvanceOrEnd writes duration/duration before it moves on.
            maybePostProgress(armed.duration, force: true)
            DiagLog.shared.write("auto-skip", ["type": "credits", "from": t,
                                               "file": armed.filePath], cat: "play")
            emit("nativeSkipped", ["type": "credits", "position": 0,
                                   "filePath": armed.filePath])
            advanceToNext(reason: "credits")
        }
    }

    /// Move the RUNNING player onto a different file without letting go of
    /// anything — the item changes, the player, its layer, the external window
    /// and the audio session do not. That is the whole point: releasing them is
    /// what drops the picture back onto the phone.
    private func swapNativeItem(to url: URL, at start: Double, play: Bool) {
        guard isNativeActive, player != nil else { return }
        DiagLog.shared.write("native-swap", [
            "to": armed.filePath, "item": armed.itemId,
            "at": start, "play": play,
            "extWindow": extWindow != nil,
        ], cat: "play")
        replaceItem(with: url, at: start, play: play)
    }

    private func replaceItem(with url: URL, at start: Double = 0, play: Bool = true) {
        guard let p = player else { return }
        if let old = item {
            for n in [Notification.Name.AVPlayerItemDidPlayToEndTime,
                      .AVPlayerItemFailedToPlayToEndTime,
                      .AVPlayerItemPlaybackStalled] {
                NotificationCenter.default.removeObserver(self, name: n, object: old)
            }
        }
        statusObs?.invalidate()
        let it = AVPlayerItem(url: url)
        item = it
        statusObs = it.observe(\.status, options: [.new]) { [weak self] obs, _ in
            guard let self = self, obs.status == .readyToPlay else { return }
            self.adoptDuration(from: obs)
            self.applyTrackSelection(on: obs)
            // A fresh item already starts at 0, so only a real resume needs the
            // seek — and it has to wait for readiness like startNative's does.
            if start > 1 { self.seekAndPlay(to: start, play: play) }
        }
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemDidEnd(_:)),
            name: .AVPlayerItemDidPlayToEndTime, object: it)
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemFailedToEnd(_:)),
            name: .AVPlayerItemFailedToPlayToEndTime, object: it)
        NotificationCenter.default.addObserver(
            self, selector: #selector(itemStalled(_:)),
            name: .AVPlayerItemPlaybackStalled, object: it)
        p.replaceCurrentItem(with: it)
        // The end-of-episode advance always plays; a skip carries the transport
        // the user left running, which can legitimately be paused.
        if play { p.play() } else { p.pause() }
        PlaybackLiveActivity.shared.update(state: liveActivityState(), force: true)
    }

    // MARK: Hand back

    @objc private func appDidBecomeActive() {
        DiagLog.shared.noteAppState("fg")
        // Coming back to the foreground is when the user LOOKS at the Island, so
        // it is the right moment to record what is actually on it against what we
        // think is playing. `live: 1, playing: false` is the stale-activity
        // report, observed rather than described.
        PlaybackLiveActivity.shared.audit("becomeActive", playing: isNativeActive || armed.active)
        restoreStrandedBrightness()
        drainPendingCommand()
        // Hand the external display back to mirroring: the web player is about to
        // become primary again, and TV Mode's whole premise is that the monitor
        // mirrors it. Safe when nothing was ever claimed.
        diagSnap("becomeActive")   // read BEFORE the hand-back undoes the evidence
        // Early mode HOLDS the display across foreground/background. Handing it back
        // here would return it to mirroring on every unlock, and the next lock would
        // kill it again — which is the exact failure this mode exists to escape.
        // stopNative() still releases it when playback really ends.
        if !earlyClaim { detachExternalWindow() }
        guard isNativeActive else { return }

        // HOLDING THE DISPLAY MEANS "active" IS NOT A HAND-BACK CUE.
        // With a live external-display scene iOS reports the app active while the
        // phone is still locked — measured: a whole run of app=act rows with the
        // phone's screen dark throughout. The deadline below then fired ~5 s into
        // every locked session and tore the native player down, taking the picture
        // on the display with it. That is the "showed a frame for a second or two,
        // black otherwise" symptom. While our window is up, the native player IS
        // the presentation; only disarm/stop ends it.
        if extWindow != nil || airplayOn { return }

        // If the webview never calls resume() — it reloaded, crashed, or the
        // page was replaced — we'd be left playing invisible audio with no UI.
        // Tear down after a grace period so the app can't get into that state.
        //
        // AND WIPE THE ARM, NOT JUST THE PLAYER. A page that never answered is a
        // page that no longer knows it armed anything, so nothing will ever
        // disarm it. This used to stop the player and keep `armed` — measured
        // 2026-09-24: the deadline fired at 13:03:04 with E32 paused at 1163 s,
        // the arm sat there `active` for 2h22m, and when the glasses were
        // plugged in at 15:25 maybeClaimEarly() started it on the display from
        // that orphaned arm — a frozen frame, and a freshly booted page with no
        // player and so no controls. disarm() flushes before it wipes.
        handBackDeadline?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.isNativeActive else { return }
            self.disarm(reason: "handback-timeout")
        }
        handBackDeadline = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 5, execute: work)
    }

    func reclaim() -> [String: Any] {
        handBackDeadline?.cancel(); handBackDeadline = nil
        // Same reasoning as the deadline above: a foreground JS hand-back would
        // stop the player that is currently feeding the external display. Tell the
        // page we are holding it and let it leave the web element alone.
        if isHolding {
            return ["holding": true, "active": true, "position": armed.position,
                    "paused": armed.paused, "ended": false,
                    "itemId": armed.itemId, "filePath": armed.filePath]
        }
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
        if isNativeActive {
            DiagLog.shared.write("stopNative", ["pos": armed.position,
                                               "title": armed.title,
                                               "ended": endedFlag], cat: "play")
        }
        handBackDeadline?.cancel(); handBackDeadline = nil
        if let p = player, let obs = timeObserver { p.removeTimeObserver(obs) }
        timeObserver = nil
        statusObs?.invalidate(); statusObs = nil
        externalObs?.invalidate(); externalObs = nil
        tcsObs?.invalidate(); tcsObs = nil
        if let old = item {
            for n in [Notification.Name.AVPlayerItemDidPlayToEndTime,
                      .AVPlayerItemFailedToPlayToEndTime,
                      .AVPlayerItemPlaybackStalled] {
                NotificationCenter.default.removeObserver(self, name: n, object: old)
            }
        }
        detachExternalWindow()      // mirroring resumes; TV Mode gets its screen back
        detachFallbackLayer()
        endAirPlaySession()
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
        if endActivity { PlaybackLiveActivity.shared.end("teardown") }
    }

    // MARK: Transport (remote commands + Live Activity intents share this)

    func handlePlaybackCommand(_ cmd: PlaybackCommand) {
        switch cmd {
        case .playPause:
            // Through setPaused rather than touching the player directly, so the
            // flip is logged with its source like every other one. Reading
            // timeControlStatus BEFORE deciding is still the right test: intent
            // and transport can disagree after an interruption, and the user is
            // pressing the button they can see, which reflects the transport.
            if let p = player { setPaused(p.timeControlStatus == .playing,
                                          source: "transport-toggle") }
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
        DiagLog.shared.write("remote", ["cmd": raw, "src": "live-activity-pending"], cat: "play")
        handlePlaybackCommand(cmd)
    }

    private func installRemoteCommands() {
        let c = MPRemoteCommandCenter.shared()
        // These arrive from the lock screen, the Island, CarPlay, a headset
        // button — and from a pair of glasses with transport keys on the frame.
        // They are the one way playback changes that leaves no trace anywhere
        // else in the app, so each one says so. `playCommand`/`pauseCommand`
        // also now move `armed.paused`: driving the player without recording the
        // intent left the flag disagreeing with the transport, and the next
        // handoff inherited the disagreement.
        _ = c.playCommand.addTarget { [weak self] _ in
            self?.setPaused(false, source: "remote-play"); return .success
        }
        _ = c.pauseCommand.addTarget { [weak self] _ in
            self?.setPaused(true, source: "remote-pause"); return .success
        }
        _ = c.togglePlayPauseCommand.addTarget { [weak self] _ in
            DiagLog.shared.write("remote", ["cmd": "togglePlayPause"], cat: "play")
            self?.handlePlaybackCommand(.playPause); return .success
        }
        c.skipForwardCommand.preferredIntervals = [15]
        _ = c.skipForwardCommand.addTarget { [weak self] _ in
            DiagLog.shared.write("remote", ["cmd": "skipForward"], cat: "play")
            self?.handlePlaybackCommand(.skipForward); return .success
        }
        c.skipBackwardCommand.preferredIntervals = [15]
        _ = c.skipBackwardCommand.addTarget { [weak self] _ in
            DiagLog.shared.write("remote", ["cmd": "skipBack"], cat: "play")
            self?.handlePlaybackCommand(.skipBack); return .success
        }
        _ = c.changePlaybackPositionCommand.addTarget { [weak self] ev in
            guard let self = self, let p = self.player,
                  let e = ev as? MPChangePlaybackPositionCommandEvent else { return .commandFailed }
            DiagLog.shared.write("remote", ["cmd": "seek", "to": e.positionTime], cat: "play")
            self.armed.position = e.positionTime
            self.armed.armedAt = Date()
            p.seek(to: CMTime(seconds: e.positionTime, preferredTimescale: 600),
                   toleranceBefore: .zero, toleranceAfter: .zero)
            return .success
        }
        _ = c.nextTrackCommand.addTarget { [weak self] _ in
            DiagLog.shared.write("remote", ["cmd": "next"], cat: "play")
            self?.handlePlaybackCommand(.next); return .success
        }
        _ = c.previousTrackCommand.addTarget { [weak self] _ in
            DiagLog.shared.write("remote", ["cmd": "prev"], cat: "play")
            self?.handlePlaybackCommand(.prev); return .success
        }
    }

    // MARK: TV Mode

    /// Blank the phone's own screen while keeping the app FOREGROUND, so
    /// mirroring keeps feeding the monitor with the full custom player and the
    /// libass subtitle overlay — neither of which can survive a real lock.
    /// Hold the idle timer open while the PHONE ITSELF is presenting the
    /// picture, so screen mirroring is not killed by an auto-lock.
    ///
    /// This is all that survives of TV Mode (removed 18.13.2). That feature
    /// bundled five behaviours behind one switch — backlight to 0, a
    /// transparent tap-swallowing shield with double-tap to exit, force-hiding
    /// the transport, a three-second countdown that engaged the lot by itself,
    /// and this. The first four existed to make a MIRRORED phone pleasant, and
    /// mirroring has been superseded by the real external-display handoff for
    /// the glasses; worse, the countdown raced the handoff (it was armed on
    /// display-connect, which precedes `isHolding` by ~60 ms, and never
    /// re-checked) so it dimmed the phone and locked touch during glasses
    /// playback, which is exactly what it was written not to do.
    ///
    /// Keeping the screen awake was the one piece with no replacement: a
    /// `<video>` playing inline in WKWebView does not reliably hold iOS awake,
    /// and if the phone sleeps while mirroring, the monitor goes with it. It is
    /// now driven automatically from the page (`_npSyncAwake`) rather than
    /// bundled behind a mode the user has to remember to engage, and it is NOT
    /// wanted during a native handoff — there the AVPlayer owns the external
    /// window and playback survives a lock by design.
    func setAwake(_ on: Bool) {
        guard on != awakeOn else { return }
        awakeOn = on
        DispatchQueue.main.async { UIApplication.shared.isIdleTimerDisabled = on }
    }

    /// Upgrade safety net. Nothing dims the screen any more (see setAwake), but
    /// a device that force-quit or crashed while TV Mode had it at 0 keeps that
    /// brightness — iOS does not put it back, and neither does installing a new
    /// build. Kept so the first launch of 18.13.2 un-strands anyone it caught,
    /// and harmless forever after because the key is never written again.
    private func restoreStrandedBrightness() {
        guard let b = AppGroupConfig.strandedBrightness else { return }
        AppGroupConfig.strandedBrightness = nil
        DispatchQueue.main.async { UIScreen.main.brightness = CGFloat(b) }
    }

    @objc private func appWillResignActive() {
        // Claim the external display NOW, while the app can still draw. A window
        // created inside didEnterBackground may never get its first composite
        // pass, and the monitor would sit on the mirrored lock screen for the rest
        // of the episode. If this turns out to be a transient resign (Control
        // Centre, a banner) didBecomeActive hands the screen straight back.
        if armed.active, armed.handoffEnabled, wantsOwnExternalWindow {
            onMain { [weak self] in self?.ensureExternalWindow() }
        }
        onMain { [weak self] in self?.diagSnap("resignActive") }
    }

    @objc private func appWillTerminate() {
        // The ONLY clean way out. Everything else leaves the marker behind and
        // is reported as `prev-launch-dirty` on the next launch.
        DiagLog.shared.closeRun()
        if awakeOn { UIApplication.shared.isIdleTimerDisabled = false }
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
    // A UIWindow in the external display's UIWindowScene is a real surface AND it REPLACES
    // mirroring for that screen. Replacing mirroring is exactly right once the
    // phone is locked, and exactly wrong while TV Mode is running (TV Mode needs
    // mirroring to carry the custom player + the libass overlay). So the window is
    // built on the way OUT (willResignActive, one composite pass before we lose
    // the ability to draw) and destroyed on the way BACK IN.

    private var externalScreen: UIScreen? { UIScreen.screens.first { $0 !== UIScreen.main } }

    /// The external-display scene UIKit hands us — and it DOES hand us one.
    ///
    /// Measured on iOS 27 with **no `UIApplicationSceneManifest`** (18.2.0's trail):
    /// `connectedScenes` carried `UIWindowSceneSessionRoleExternalDisplayNonInteractive`
    /// alongside the application scene. The compatibility path connects it even though
    /// this app never opted into scenes, and even though Apple's own article says that
    /// from iOS 27 the role arrives only after registering a `UISceneAccessory`. So the
    /// scene migration 18.2.0 called for is unnecessary: the scene is already there,
    /// mirroring the phone, waiting for something to put a window in it.
    ///
    /// Matched on the role's raw value rather than the `.windowExternalDisplayNonInteractive`
    /// constant so this still compiles at the project's iOS 15 deployment target (the
    /// constant is iOS 16+), and so the deprecated pre-16 `…RoleExternalDisplay` is
    /// picked up by the same prefix.
    private var externalScene: UIWindowScene? {
        UIApplication.shared.connectedScenes
            .lazy
            .compactMap { $0 as? UIWindowScene }
            .first { $0.session.role.rawValue.hasPrefix("UIWindowSceneSessionRoleExternalDisplay") }
    }

    /// UIKit from wherever we are called. Capacitor delivers plugin methods off
    /// the main thread (`takeover`, `resume`), the lifecycle notifications on it —
    /// and every window/layer touch below must run on main either way. Synchronous
    /// when already on main, because `ensureExternalWindow` is racing the last
    /// composite pass before the app loses the ability to draw.
    private func onMain(_ work: @escaping () -> Void) {
        if Thread.isMainThread { work() } else { DispatchQueue.main.async(execute: work) }
    }

    /// True when we should claim the external display with a window of our own,
    /// rather than leaving mirroring up for AVFoundation's route to take over.
    /// Gated on the SCENE, not the screen: the window has to go somewhere, and the
    /// scene is the only thing that can hold it.
    private var wantsOwnExternalWindow: Bool {
        externalScene != nil && armed.extMode != "route"
    }

    /// Claim the display as soon as an episode is armed, rather than waiting for
    /// the lock.
    ///
    /// WHY THIS MODE EXISTS. Claiming at `willResignActive` cannot work, and the
    /// trails say why: by then mirroring is already collapsing and the scene is
    /// gone (`scene=-` at resignActive, and the external UIScreen itself vanishes
    /// for the whole locked stretch). That looked like "iOS cuts the display at
    /// lock, nothing to be done" — but Viture's own app keeps content on the
    /// glasses through a lock, so it plainly can be done.
    ///
    /// The distinction is MIRRORING vs OWNERSHIP. A mirrored display is slaved to
    /// the phone's screen, so locking kills it. A display an app owns through its
    /// external-display scene is not — which is the state we never reached,
    /// because we only ever tried to reach it at the one moment it is unreachable.
    /// So: take the display while the app is comfortably foreground and the scene
    /// is live, and still hold it when the lock arrives.
    ///
    /// The cost is visible and is why this is a setting rather than the default:
    /// claiming the display stops mirroring immediately, so the glasses go BLACK
    /// until the handoff puts a player layer in the window. That black screen is
    /// also the confirmation that the takeover happened.
    private var earlyClaim: Bool { armed.extMode == "early" }

    /// Idempotent; safe to call on every arm.
    private func maybeClaimEarly() {
        guard earlyClaim, armed.active, armed.handoffEnabled else { return }
        onMain { [weak self] in
            guard let self = self, self.extWindow == nil,
                  self.externalScene != nil else { return }
            self.detachFallbackLayer()
            self.ensureExternalWindow()
            self.attachExternalLayer()   // no-op until startNative makes a player
            self.diagSnap("earlyClaim")
            self.emit("displayChanged", self.displayInfo())

            // EARLY HANDOFF. Start the native player NOW, while foreground.
            //
            // The relief-pitcher model creates the AVPlayer at
            // didEnterBackground and calls play() there. iOS lets a backgrounded
            // app CONTINUE audio; it does not let one START a fresh player. That
            // is what the trails kept showing: session active, seek landing
            // exactly right, play() issued, and tcs=pause anyway.
            //
            // Claiming the display already happens here, for the same class of
            // reason — the moment of the lock is the one moment the thing cannot
            // be done. So take playback here too. By the time the phone locks
            // there is nothing left to hand off: it is already ours and already
            // playing, which is a state iOS is happy to continue.
            //
            // The web element must stop, or two engines play at once; JS does
            // that on the nativeStarted event.
            if !self.isNativeActive, self.armed.url != nil {
                self.startNative(reason: "early")
                self.diagSnap("earlyHandoff")
            }
        }
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
            }
            // Falls through to the route layer in two cases: "Mirrored" was chosen,
            // or "Direct" was chosen and there was no scene to put a window in. The
            // second is the degradation that matters — without it, a Direct-mode
            // handoff on a device that never offers the scene attaches NO surface at
            // all, which is the audio-only bug 14.1.1 fixed.
            if self.extLayer == nil, self.externalScreen != nil || self.airplayOn {
                self.attachFallbackLayer()
            }
        }
    }

    /// Black window on the external display. Deliberately separate from
    /// `attachExternalLayer` — at resign-active time there is no player yet, but
    /// that is the last moment the app is guaranteed a composite pass.
    private func ensureExternalWindow() {
        guard extWindow == nil, let scene = externalScene else { return }
        let vc = UIViewController()
        // A view whose BACKING layer is the AVPlayerLayer, rather than a layer
        // hand-added as a sublayer. See ExternalPlayerView for why that matters.
        let pv = ExternalPlayerView(frame: scene.screen.bounds)
        pv.autoresizingMask = [.flexibleWidth, .flexibleHeight]
        pv.backgroundColor = .black
        vc.view = pv
        // `UIWindow(windowScene:)` is the whole fix. The old code built the window
        // with a frame and then set `w.screen`, which since iOS 13 means "move me to
        // the window scene on that screen" — a resolution step that found nothing,
        // because nothing ever told UIKit which scene we meant. The window stayed
        // unattached, was never presented, and mirroring was never displaced, so
        // Direct behaved exactly like Mirrored. Naming the scene directly is what
        // kicks the system out of mirroring for that display.
        let w = UIWindow(windowScene: scene)
        w.frame = scene.screen.bounds     // contextual bounds; never UIScreen.main's
        w.backgroundColor = .black
        w.rootViewController = vc
        w.isHidden = false
        extWindow = w
    }

    private func attachExternalLayer() {
        guard extLayer == nil, let p = player,
              let pv = extWindow?.rootViewController?.view as? ExternalPlayerView
        else { return }
        // Belt and braces on the geometry: the autoresizing mask keeps the view
        // matched to the window, but that window may never have been laid out, so
        // pin it to the scene's screen explicitly as well.
        if pv.bounds.isEmpty, let b = externalScene?.screen.bounds { pv.frame = b }
        let l = pv.playerLayer
        l.videoGravity = .resizeAspect
        l.backgroundColor = UIColor.black.cgColor
        l.player = p
        extLayer = l
        // Our window IS the external presentation. Letting AVFoundation also try
        // to seize the screen would have the two fighting over it.
        p.usesExternalPlaybackWhileExternalScreenIsActive = false
    }

    private func detachExternalWindow() {
        onMain { [weak self] in
            guard let self = self else { return }
            // Backing layer of ExternalPlayerView now — detaching the player IS
            // the teardown; removeFromSuperlayer() would strip the view's own
            // layer out from under it.
            self.extLayer?.player = nil
            self.extLayer = nil
            self.extWindow?.isHidden = true
            self.extWindow?.rootViewController = nil
            // Detaching from the scene is what hands the display BACK to mirroring
            // — the documented inverse of putting a window in it. Dropping the
            // reference alone would leave the scene holding our window.
            self.extWindow?.windowScene = nil
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
        // The app's window lives on the APPLICATION scene since 18.20.1 adopted
        // scenes — `AppDelegate.window` is nil for good, and reading it is what left
        // Mirrored mode with no presenting layer (mainLayer:false on every row).
        guard mainLayer == nil, let p = player,
              let root = UIApplication.shared.connectedScenes
                  .compactMap({ $0 as? UIWindowScene })
                  .first(where: { $0.session.role == .windowApplication })?
                  .windows.first?.rootViewController?.view else { return }
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

    // MARK: AirPlay (18.26.0 spike)

    // WHAT IS DIFFERENT FROM THE GLASSES
    // The glasses are a SCREEN: we draw into a window on it, and the bytes never
    // leave the phone. An AirPlay receiver is a separate computer that is handed
    // the URL and fetches the stream itself, so the whole question is whether it
    // can reach that URL — and none of ours are reachable from it (loopback, or
    // the box behind Tailscale). AirPlayDoor answers that; everything here is
    // the same takeover the glasses use: native becomes the presentation, the
    // page becomes the remote, progress/skip/advance run off the native clock.
    //
    // WHY THE WEB PLAYER CANNOT DO THIS ITSELF
    // It is hls.js over ManagedMediaSource. AirPlay carries MSE only as
    // mirroring, and mirroring dies with the lock — the exact failure the
    // native handoff exists to escape.
    //
    // The route needs a PRESENTING player (same rule as the monitor: a layerless
    // AVPlayer is audio-only and never enters external playback), so the
    // fallback layer behind the webview is attached for the session.

    func startAirPlay(_ done: @escaping ([String: Any]) -> Void) {
        onMain { [weak self] in
            guard let self = self else { return }
            guard self.armed.active, let url = self.armed.url else {
                done(["ok": false, "error": "Nothing is playing that AirPlay can take."]); return
            }
            if self.extWindow != nil {
                done(["ok": false, "error": "Already playing on a connected display."]); return
            }
            AirPlayDoor.shared.open(for: url, bearer: self.armed.token) { [weak self] result in
                guard let self = self else { return }
                if case .failure(let err) = result {
                    DiagLog.shared.write("airplay-refused", ["err": err.localizedDescription], cat: "ext")
                    done(["ok": false, "error": err.localizedDescription]); return
                }
                guard let lan = AirPlayDoor.shared.lanURL(for: url) else {
                    done(["ok": false, "error": "Could not share this stream."]); return
                }
                self.airplayOn = true
                self.airplayEngaged = false
                self.armed.url = lan
                if let n = self.armed.nextUrl { self.armed.nextUrl = AirPlayDoor.shared.lanURL(for: n) ?? n }
                DiagLog.shared.write("airplay-start", [
                    "native": self.isNativeActive, "at": self.armed.position,
                    "title": self.armed.title,
                    // Scheme + host only: the token in the path is the door's key.
                    "upstream": "\(url.scheme ?? "?")://\(url.host ?? "?")",
                    "lanHost": AirPlayDoor.shared.host ?? "",
                ], cat: "ext")
                if self.isNativeActive {
                    // Already native (backgrounded earlier and came back): move
                    // the running player onto the door without dropping the playhead.
                    let at = self.player.map { CMTimeGetSeconds($0.currentTime()) } ?? self.armed.position
                    self.replaceItem(with: lan, at: at, play: !self.armed.paused)
                    self.attachFallbackLayer()
                } else {
                    self.startNative(reason: "airplay")
                }
                self.presentRoutePicker()
                self.armAirPlayWatchdog()
                done(["ok": true])
            }
        }
    }

    /// The system route sheet, with video receivers first. AVRoutePickerView has
    /// no "present" API — it is a button — so it lives invisibly in the app's
    /// window and we press it.
    private func presentRoutePicker() {
        guard let root = UIApplication.shared.connectedScenes
                .compactMap({ $0 as? UIWindowScene })
                .first(where: { $0.session.role == .windowApplication })?
                .windows.first?.rootViewController?.view else {
            DiagLog.shared.write("airplay-picker-missing", ["why": "no-root"], cat: "ext")
            return
        }
        let pv = routePicker ?? AVRoutePickerView(frame: CGRect(x: root.bounds.midX, y: root.bounds.midY,
                                                                width: 1, height: 1))
        pv.prioritizesVideoDevices = true
        pv.alpha = 0.02
        if pv.superview == nil { root.addSubview(pv) }
        routePicker = pv
        func button(in v: UIView) -> UIButton? {
            if let b = v as? UIButton { return b }
            for s in v.subviews { if let b = button(in: s) { return b } }
            return nil
        }
        if let b = button(in: pv) {
            b.sendActions(for: .touchUpInside)
        } else {
            DiagLog.shared.write("airplay-picker-missing", ["why": "no-button"], cat: "ext")
        }
    }

    /// A picker dismissed without a choice leaves native playing through the
    /// phone's speaker behind a page that thinks it is a remote. Give the viewer
    /// time to choose, then hand back.
    private func armAirPlayWatchdog() {
        airplayWatchdog?.cancel()
        let work = DispatchWorkItem { [weak self] in
            guard let self = self, self.airplayOn, !self.airplayEngaged else { return }
            self.endAirPlay(reason: "never-picked")
        }
        airplayWatchdog = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 45, execute: work)
    }

    private func airplayRouteChanged(_ external: Bool) {
        guard airplayOn else { return }
        DiagLog.shared.write("airplay-route", ["external": external,
                                               "engaged": airplayEngaged,
                                               "pos": armed.position], cat: "ext")
        if external {
            airplayEngaged = true
            airplayWatchdog?.cancel(); airplayWatchdog = nil
        } else if airplayEngaged {
            endAirPlay(reason: "route-lost")
        }
    }

    /// The session is over but the player is not: tell the page, whose resume()
    /// flushes, stops native and puts the episode back on the phone.
    private func endAirPlay(reason: String) {
        guard airplayOn else { return }
        airplayOn = false
        airplayWatchdog?.cancel(); airplayWatchdog = nil
        DiagLog.shared.write("airplay-end", ["reason": reason, "pos": armed.position,
                                             "engaged": airplayEngaged], cat: "ext")
        emit("airplayEnded", ["reason": reason, "position": armed.position])
    }

    /// Teardown half, from stopNative. Closing the door is what makes the
    /// receiver's URLs dead, so it happens only once no player needs them.
    private func endAirPlaySession() {
        airplayOn = false
        airplayEngaged = false
        airplayWatchdog?.cancel(); airplayWatchdog = nil
        AirPlayDoor.shared.close()
        let pv = routePicker
        routePicker = nil
        onMain { pv?.removeFromSuperview() }
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
        diagSnap("screenChange")
        emit("displayChanged", displayInfo())
    }

    /// The external-display scene arriving is the moment Direct mode becomes
    /// possible at all, and it can land AFTER we have already backgrounded and
    /// picked the route layer. Take the display over now rather than waiting for
    /// a `willResignActive` that has already happened.
    @objc private func sceneDidConnect(_ note: Notification) {
        guard (note.object as? UIWindowScene) != nil else { return }
        onMain { [weak self] in
            guard let self = self else { return }
            if self.isNativeActive, self.wantsOwnExternalWindow,
               self.extWindow == nil, self.armed.active {
                // Drop the consolation prize first — two surfaces for one player
                // would have AVFoundation's route and our window fighting over
                // the same display.
                self.detachFallbackLayer()
                self.ensureExternalWindow()
                self.attachExternalLayer()
            }
            self.diagSnap("sceneConnect")
            self.emit("displayChanged", self.displayInfo())

            // PLUGGING IN MID-EPISODE IS AN ARM, NOT JUST A SCENE.
            //
            // This used to inline the CLAIM half of maybeClaimEarly() and stop
            // there, so a display that arrived while the page was already playing
            // got a window of ours with no player layer in it — the glasses went
            // black (mirroring replaced) while the episode carried on down on the
            // phone. Measured 2026-09-23: sceneConnect at 02:52:37 with
            // native=false, and nothing changed until the user stopped and
            // restarted playback, which is what "connecting mid playback needs a
            // stop/start" was.
            //
            // Claiming and handing off are one act and always were; the copy was
            // the bug. maybeClaimEarly() is idempotent and guards on
            // `extWindow == nil`, so the branch above (native already running,
            // its window died with an earlier scene) still wins and this is then
            // a no-op. Called AFTER the snap so the transcript keeps a reading of
            // the moment the scene landed, before the claim rewrites it.
            let wasNative = self.isNativeActive
            self.maybeClaimEarly()
            if !wasNative, self.isNativeActive {
                DiagLog.shared.write("display-handoff", [
                    "at": self.armed.position, "title": self.armed.title,
                ], cat: "ext")
            }
        }
    }

    @objc private func sceneDidDisconnect(_ note: Notification) {
        guard (note.object as? UIWindowScene) != nil else { return }
        onMain { [weak self] in
            guard let self = self else { return }
            // Our window died with the scene; clear the references so a later
            // reconnect rebuilds instead of seeing a non-nil `extWindow` and
            // deciding there is nothing to do.
            if self.externalScene == nil, self.extWindow != nil {
                self.extLayer?.player = nil
                self.extLayer = nil
                self.extWindow = nil
            }
            self.diagSnap("sceneDisconnect")
        }
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

    // MARK: External-display diagnostics

    // WHY THIS EXISTS
    // Direct mode had never once displaced mirroring on a real device — it behaved
    // identically to Mirrored. Apple's current documentation points at the cause:
    //
    //   * `UIScreen.mirrored`: "To disable mirroring and present unique content
    //     on the external display, REGISTER A SCENE ACCESSORY."
    //   * "Presenting content on a connected display": "To present content on a
    //     connected display, you attach windows to UIWindowScene objects that
    //     the system provides and respond to life-cycle events using scene
    //     delegates." No UIScreen-based path is documented any more.
    //   * `UIWindow.screen` — deprecated, "Use windowScene instead".
    //     `UIScreen.screens` / `UIScreen.didConnectNotification` — deprecated
    //     at iOS 16.0, "use UIApplication.shared.openSessions" / a scene delegate.
    //
    // `ensureExternalWindow` set `w.screen`, which since iOS 13 means "move me to
    // the window scene on that screen" — and nothing ever named a scene, so the
    // window was never presented.
    //
    // WHAT THIS INSTRUMENT FOUND, WHICH IS NOT WHAT IT WAS BUILT TO FIND
    // The reasoning above ended in a wrong prediction: that an app with no
    // UIApplicationSceneManifest is never handed a windowExternalDisplayNonInteractive
    // scene, and that fixing this meant migrating the app's whole launch path to
    // scenes. The first trail off a real iOS 27 device said `externalDisplayScene:
    // true` — the compatibility path connects that scene regardless, and regardless
    // of the UISceneAccessory registration Apple's article says iOS 27 requires. The
    // scene was there all along, mirroring the phone, waiting for a window. See
    // `externalScene`. THE LESSON IS THE INSTRUMENT: do not re-derive the scene's
    // absence from the manifest's absence — read `connectedScenes`.
    //
    // Keep this armed, because the fix's own effect is only observable from a locked
    // phone. The fields that decide it —
    //   `winScene` : false => our UIWindow belongs to no scene, i.e. never presented.
    //   `mirrored` : false at "locked+3s" => the takeover happened and the monitor is
    //                showing our window rather than the lock screen.
    // Both only mean anything WHILE THE PHONE IS LOCKED, which is exactly when nothing
    // can display them — hence a buffered trail read back after unlocking. A trail with
    // no "locked+3s" row in it tested nothing at all; the first one was exactly that.

    private var diagTrail: [[String: Any]] = []
    private var diagT0 = Date()

    private static func itemStatusName(_ s: AVPlayerItem.Status?) -> String {
        switch s {
        case .some(.readyToPlay): return "ready"
        case .some(.failed):      return "FAILED"
        case .some(.unknown):     return "unknown"
        default:                  return "-"
        }
    }

    private static func tcsName(_ s: AVPlayer.TimeControlStatus?) -> String {
        switch s {
        case .some(.playing):                    return "play"
        case .some(.paused):                     return "pause"
        case .some(.waitingToPlayAtSpecifiedRate): return "wait"
        default:                                 return "-"
        }
    }

    private static func stateName(_ s: UIApplication.State) -> String {
        switch s {
        case .active:   return "act"
        case .inactive: return "inact"
        case .background: return "bg"
        @unknown default: return "?"
        }
    }

    private func diagSnap(_ label: String) {
        let ext = externalScreen
        // Both of these are OPTIONAL properties reached through OPTIONAL chaining,
        // so the naive `ext?.mirrored != nil` is a double optional and is true
        // whenever `ext` exists — i.e. exactly the always-true reading that would
        // make this whole instrument useless. Flatten first.
        let mirroredFrom: UIScreen? = ext.flatMap { $0.mirrored }
        let winScene: UIWindowScene? = extWindow.flatMap { $0.windowScene }
        var row: [String: Any] = [
            "at":          label,
            "t":           String(format: "%.1f", Date().timeIntervalSince(diagT0)),
            "screens":     UIScreen.screens.count,
            "extScreen":   ext != nil,
            "extScene":    externalScene != nil,
            "mirrored":    mirroredFrom != nil,
            "extWindow":   extWindow != nil,
            "winScene":    winScene != nil,
            "winOnExt":    extWindow != nil && ext != nil && extWindow?.screen === ext,
            "extLayer":    extLayer != nil,
            "mainLayer":   mainLayer != nil,
            "extPlayback": player?.isExternalPlaybackActive ?? false,
            "native":      isNativeActive,
            "mode":        armed.extMode,
            // iOS 27 scene-accessory registration: "on/avail", "off/unavail", "-".
            // `extScene:false` beside `accessory:"-"` means we never asked.
            "accessory":   ExternalDisplayAccessory.state,
            // "act" / "inact" / "bg". Run 4 could not distinguish "the phone was
            // locked" from "the app came back and the reading is meaningless", and
            // the user had no way to know either. There is no public API for WHY a
            // device woke, but there is one for whether we were foreground when the
            // sample was taken, and that is the part that invalidates a reading.
            "appState":    Self.stateName(UIApplication.shared.applicationState),
            // Is the PLAYER actually running? A frozen picture has two very
            // different causes and the columns above cannot tell them apart:
            // a paused player (rate 0, position static) versus a playing one
            // whose video decode has been suspended (rate 1, position advancing,
            // last frame stuck on screen). `pos` across locked+3/10/20 settles it.
            "rate":        Double(player?.rate ?? 0),
            "tcs":         Self.tcsName(player?.timeControlStatus),
            "pos":         player.map { CMTimeGetSeconds($0.currentTime()) }
                             .flatMap { $0.isFinite ? Double(round($0 * 10) / 10) : nil } ?? -1,
            // Per row, because the header's copy is read after stopNative() has
            // already reset the flag — it reported INACTIVE no matter what
            // happened at the handoff, which is the moment that matters.
            "sess":        sessionActivated,
            "likely":       player?.currentItem?.isPlaybackLikelyToKeepUp ?? false,
            // AVPlayer states its own reason for not playing. We have never asked.
            "waitReason":  (player?.reasonForWaitingToPlay?.rawValue as String?) ?? "",
            "itemStatus":  Self.itemStatusName(player?.currentItem?.status),
            "itemErr":     player?.currentItem?.error?.localizedDescription ?? "",
            "armPaused":   armed.paused,
        ]
        if let b = ext?.bounds { row["extBounds"] = "\(Int(b.width))x\(Int(b.height))" }
        diagTrail.append(row)
        DiagLog.shared.write("snap", row, cat: "ext")
        // A lock/unlock cycle produces ~6 rows; keep a few cycles, drop the rest.
        if diagTrail.count > 40 { diagTrail.removeFirst(diagTrail.count - 40) }
    }

    /// Snapshots taken from the background, where nothing else can run. The app
    /// is alive here (the `audio` background mode plus the handoff's bg task),
    /// so these fire; if they are MISSING from the trail, that is itself the
    /// finding — the process was suspended instead of playing.
    /// Samples after a HANDOFF. Early mode starts the player while foreground, so
    /// these are no longer "into a lock" — the label said locked+Ns and meant
    /// nothing of the sort once 18.5.0 landed.
    private func scheduleLockedSnaps() {
        for d in [3.0, 10.0, 20.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in
                guard let self = self, self.isNativeActive else { return }
                self.diagSnap("handoff+\(Int(d))s")
            }
        }
    }

    /// Samples after the app actually BACKGROUNDS. With the handoff moved earlier
    /// this is the only thing that measures a real lock.
    private func scheduleBackgroundSnaps() {
        for d in [3.0, 10.0, 20.0] {
            DispatchQueue.main.asyncAfter(deadline: .now() + d) { [weak self] in
                guard let self = self, self.isNativeActive else { return }
                self.diagSnap("locked+\(Int(d))s")
            }
        }
    }

    /// Capacitor delivers plugin methods off the main thread, and everything read
    /// here is UIKit. Blocking on main from a background queue is safe (main is
    /// not waiting on us); the guard is only for the already-on-main case.
    private func onMainSync<T>(_ work: () -> T) -> T {
        if Thread.isMainThread { return work() }
        return DispatchQueue.main.sync(execute: work)
    }

    func extDiagInfo() -> [String: Any] {
        onMainSync { extDiagInfoOnMain() }
    }

    private func extDiagInfoOnMain() -> [String: Any] {
        diagSnap("read")
        let app = UIApplication.shared
        let roles = app.connectedScenes.map { $0.session.role.rawValue }
        let openRoles = app.openSessions.map { $0.role.rawValue }
        return [
            // nil => pre-scene lifecycle => no external-display scene can exist.
            "sceneManifest": Bundle.main.object(forInfoDictionaryKey: "UIApplicationSceneManifest") != nil,
            "connectedScenes": roles,
            "openSessions": openRoles,
            "externalDisplayScene": roles.contains(where: { $0.contains("ExternalDisplay") }),
            "build": NP_BUILD,
            "audioSession": sessionActivated ? "active now"
                             : (audioEverActivated ? "released (was active)" : "NEVER ACTIVATED"),
            "audioError": audioSessionError,
            "iosVersion": UIDevice.current.systemVersion,
            "extMode": armed.extMode,
            "trail": diagTrail,
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
            "awake":    awakeOn,
            "holding":  isHolding,
            "airplay":  airplayOn,
        ]
    }

    // MARK: Helpers

    private func liveActivityState() -> PlaybackLiveState {
        PlaybackLiveState(
            title: armed.title, series: armed.series,
            isPaused: armed.paused || (player.map { $0.timeControlStatus != .playing } ?? armed.paused),
            position: armed.position, duration: armed.duration,
            external: player?.isExternalPlaybackActive ?? false,
            canPrev: armed.canPrev, canNext: armed.canNext)
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
