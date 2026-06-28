//
//  BundleDownloader.swift
//  StreamLink iOS — M2 (offline download)
//
//  Downloads a full `.offline_cache/<sha>/` HLS bundle from the host to a
//  non-evictable on-device dir so it can later be served offline by
//  `LocalMediaServer` and played by the existing web player (master_url swap).
//
//  The host's GET /api/library/{item}/bundle-manifest (plan A1) returns a flat
//  list of the bundle's files (name + size). The web glue resolves those against
//  the host origin and hands us `{ cacheKey, baseUrl, files[] }`; we fetch every
//  file via the traversal-guarded /api/library/offline-cache/<sha>/<name> route
//  into Application Support/StreamLinkBundles/<sha>/, keyed by the cache sha so a
//  re-download of an unchanged source is a no-op. A small index.json records each
//  bundle's (itemId, filePath, expected files, complete) so getLocal()/list()
//  work without the network and a partial download resumes by skipping files
//  already on disk at their expected size.
//
//  Transfers ride a HYBRID of two URLSessions sharing one delegate: a fast,
//  in-process *default* session while the app is foreground (iOS throttles
//  background-session traffic even in front, so a pure-background downloader lags
//  far behind in-process AVPlayer/WKWebView streaming), and a *background* session
//  while the app is suspended so downloads keep running and complete across a kill
//  (M2 "survive relaunch"). In-flight tasks migrate between the two on each
//  foreground/background transition (cancel + re-enqueue; segments are small). The
//  sessions + delegate live in a process-wide singleton (`BundleDownloadManager`)
//  independent of the Capacitor plugin's lifecycle, because a background session
//  identifier must be owned by exactly one long-lived delegate.
//
//  JS surface (Capacitor plugin "BundleDownloader"):
//    download({ itemId, filePath, cacheKey, name?, baseUrl, files:[{name,size}], token?, masterContent?, meta? })
//                                   -> { sha, dir, alreadyComplete }
//    `masterContent` (optional) = the host's master.m3u8 trimmed to the highest
//    video rung; written to disk verbatim so the dropped ABR down-rungs (absent
//    from `files`) are never fetched or referenced.
//    getLocal({ itemId, filePath }) -> { found, complete, sha?, dir?, bytesDone, bytesTotal, fileCount, meta? }
//    list()                         -> { items:[{ sha, itemId, filePath, name, complete, bytesTotal, bytesDone, fileCount, meta? }] }
//  `meta` (optional) = { series, title, season, episode, episode_name, overview,
//    tmdb_kind, poster_path, img_base, poster_data_url } — series/episode info +
//    inlined poster so the offline Downloads picker can group/label with no host.
//    remove({ sha? , itemId?, filePath? }) -> {}
//    cancel({ sha })                -> {}
//    bytesUsed()                    -> { bytes }
//    openExternal({ url })          -> {}   // open a host URL in Safari (Clip share)
//
//  Events: "bundleProgress" { sha, itemId, filePath, bytesDone, bytesTotal, fraction, filesDone, fileCount }
//          "bundleComplete" { sha, itemId, filePath, dir }
//          "bundleError"    { sha, itemId, filePath, message }
//

import Foundation
import UIKit
import Capacitor

// MARK: - Plugin

@objc(BundleDownloader)
public class BundleDownloader: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "BundleDownloader"
    public let jsName = "BundleDownloader"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "download",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getLocal",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "list",      returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "remove",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "cancel",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "bytesUsed", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "enqueue",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "dequeue",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "queueList", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "holdBackground",    returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "releaseBackground", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "openExternal",      returnType: CAPPluginReturnPromise),
    ]

    public override func load() {
        // Relay manager events to the JS layer. The manager outlives the plugin,
        // so guard against a stale closure by capturing weakly.
        BundleDownloadManager.shared.onEvent = { [weak self] name, payload in
            self?.notifyListeners(name, data: payload)
        }
    }

    @objc func download(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"), !itemId.isEmpty,
              let filePath = call.getString("filePath"), !filePath.isEmpty,
              let cacheKey = call.getString("cacheKey"), !cacheKey.isEmpty,
              let baseUrl = call.getString("baseUrl"), !baseUrl.isEmpty else {
            call.reject("download() requires itemId, filePath, cacheKey, baseUrl.")
            return
        }
        let rawFiles = call.getArray("files", JSObject.self) ?? []
        var files: [BundleFile] = []
        for f in rawFiles {
            guard let name = f["name"] as? String else { continue }
            // The JS bridge may deliver a JSON number as Int or Double — read both,
            // else a partial file could be mistaken for complete (size 0 ⇒ "any
            // bytes = done" in the resume check).
            let size = (f["size"] as? Int).map { Int64($0) }
                ?? (f["size"] as? Double).map { Int64($0) }
                ?? (f["size"] as? NSNumber).map { $0.int64Value }
                ?? 0
            files.append(BundleFile(name: name, size: size))
        }
        guard !files.isEmpty else { call.reject("download() requires a non-empty files list."); return }

        // Optional series/episode metadata (+ inlined poster) for the offline picker.
        let meta = call.getObject("meta")?.reduce(into: [String: Any]()) { $0[$1.key] = $1.value }

        do {
            let res = try BundleDownloadManager.shared.startDownload(
                itemId: itemId, filePath: filePath, cacheKey: cacheKey,
                name: call.getString("name") ?? filePath,
                baseUrl: baseUrl, files: files, token: call.getString("token"),
                masterContent: call.getString("masterContent"), meta: meta)
            call.resolve(["sha": cacheKey, "dir": res.dir, "alreadyComplete": res.alreadyComplete])
        } catch {
            call.reject("Could not start download: \(error.localizedDescription)")
        }
    }

    @objc func getLocal(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"),
              let filePath = call.getString("filePath") else {
            call.reject("getLocal() requires itemId, filePath."); return
        }
        call.resolve(BundleDownloadManager.shared.getLocal(itemId: itemId, filePath: filePath))
    }

    @objc func list(_ call: CAPPluginCall) {
        call.resolve(["items": BundleDownloadManager.shared.list()])
    }

    @objc func remove(_ call: CAPPluginCall) {
        if let sha = call.getString("sha"), !sha.isEmpty {
            BundleDownloadManager.shared.remove(sha: sha)
        } else if let itemId = call.getString("itemId"), let filePath = call.getString("filePath") {
            BundleDownloadManager.shared.remove(itemId: itemId, filePath: filePath)
        } else {
            call.reject("remove() requires sha, or itemId+filePath."); return
        }
        call.resolve()
    }

    @objc func cancel(_ call: CAPPluginCall) {
        guard let sha = call.getString("sha"), !sha.isEmpty else { call.reject("cancel() requires sha."); return }
        BundleDownloadManager.shared.cancel(sha: sha)
        call.resolve()
    }

    @objc func bytesUsed(_ call: CAPPluginCall) {
        call.resolve(["bytes": BundleDownloadManager.shared.bytesUsed()])
    }

    // Durable download queue (the user's *intent*, persisted before the JS
    // orchestration even fetches the manifest) so a download survives an app kill
    // AND a multi-hour connectivity loss: the dashboard re-drives every queued
    // entry on launch + on `online`, and the entry only clears once the bundle is
    // fully on disk. See `enqueue`/`dequeue`/`queueList` on the manager.
    @objc func enqueue(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"), !itemId.isEmpty,
              let filePath = call.getString("filePath"), !filePath.isEmpty else {
            call.reject("enqueue() requires itemId, filePath."); return
        }
        BundleDownloadManager.shared.enqueue(
            itemId: itemId, filePath: filePath,
            name: call.getString("name") ?? filePath,
            quality: call.getString("quality"),
            profileId: call.getString("profileId"))
        call.resolve()
    }
    @objc func dequeue(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"),
              let filePath = call.getString("filePath") else {
            call.reject("dequeue() requires itemId, filePath."); return
        }
        BundleDownloadManager.shared.dequeue(itemId: itemId, filePath: filePath)
        call.resolve()
    }
    @objc func queueList(_ call: CAPPluginCall) {
        call.resolve(["items": BundleDownloadManager.shared.queueList()])
    }

    // The dashboard orchestrates downloads in JS (host-prep poll + pool driver)
    // BEFORE handing each file to the durable background URLSession. JS can't take
    // a UIApplication background assertion itself, so it brackets that window with
    // these so a brief background mid-prep doesn't immediately suspend the page and
    // stall the pool. Ref-counted: concurrent pool lanes hold/release independently.
    @objc func holdBackground(_ call: CAPPluginCall) {
        BundleDownloadManager.shared.holdOrchestrationBackground()
        call.resolve()
    }
    @objc func releaseBackground(_ call: CAPPluginCall) {
        BundleDownloadManager.shared.releaseOrchestrationBackground()
        call.resolve()
    }

    // Hand a host URL to the system browser (Safari). Used by the Clip feature:
    // the dashboard runs in this WKWebView over a plain-http host origin, which is
    // NOT a secure context, so navigator.share (Web Share file API) is unavailable
    // and <a download> / window.open are no-ops in the WebView — the clip MP4 can't
    // be delivered in-app. Opening the clip's host URL in Safari instead lets iOS
    // preview the MP4 with a native Save-to-Files/Photos + Share sheet. The clip URL
    // carries its own random capability token, so no pairing header is needed.
    @objc func openExternal(_ call: CAPPluginCall) {
        guard let urlStr = call.getString("url"), !urlStr.isEmpty,
              let url = URL(string: urlStr) else {
            call.reject("openExternal() requires a valid `url`.")
            return
        }
        DispatchQueue.main.async {
            UIApplication.shared.open(url, options: [:]) { ok in
                if ok { call.resolve() }
                else { call.reject("Could not open the URL in the system browser.") }
            }
        }
    }
}

// MARK: - Model

struct BundleFile { let name: String; let size: Int64 }

// MARK: - Download manager (process-wide singleton owning the URLSession + delegate)

final class BundleDownloadManager: NSObject, URLSessionDownloadDelegate {
    static let shared = BundleDownloadManager()

    /// Called for "bundleProgress" / "bundleComplete" / "bundleError".
    var onEvent: ((String, [String: Any]) -> Void)?

    override init() {
        super.init()
        // Watch app foreground/background so transfers ride the fast in-process
        // session while active and the background session while suspended. The
        // singleton is created at launch (the plugin's load() touches `.shared`),
        // so these are registered before any download starts.
        let nc = NotificationCenter.default
        nc.addObserver(self, selector: #selector(appDidBecomeActive),
                       name: UIApplication.didBecomeActiveNotification, object: nil)
        nc.addObserver(self, selector: #selector(appDidEnterBackground),
                       name: UIApplication.didEnterBackgroundNotification, object: nil)
    }

    @objc private func appDidBecomeActive() {
        queue.async {
            self.appActive = true
            guard !self.jobs.isEmpty else { return }
            // Pull any still-running suspended-mode transfers back onto the fast
            // session so an open app always downloads at full speed.
            self.migrateTasks(from: self.session, to: self.fgSession)
        }
    }
    @objc private func appDidEnterBackground() {
        queue.async {
            self.appActive = false
            guard !self.jobs.isEmpty else { return }
            // Hand in-flight foreground transfers to the background session so they
            // keep running while suspended. The job-keyed bgTask assertion (held for
            // the whole download) keeps us alive long enough to re-enqueue.
            self.migrateTasks(from: self.fgSession, to: self.session)
        }
    }

    // HYBRID SESSIONS — speed while active, durability while suspended.
    //
    // iOS runs a *background* URLSession's transfers out-of-process (`nsurlsessiond`)
    // at background QoS and rate-limits them even in the foreground with
    // `isDiscretionary = false`. That's the whole reason "HLS streams load fast but
    // downloads lag": AVPlayer/WKWebView fetch in-process at full link speed, while a
    // pure-background downloader crawls. But a background session is also the only
    // thing that keeps transfers running — and completing — while the app is
    // suspended (the preview.6.0.0 background-download feature).
    //
    // So we run BOTH and route each task by app state (`appActive`):
    //   • app foreground → `fgSession` (a default session, in-process, full speed)
    //   • app suspended  → `session`   (the background session, survives suspend)
    // On the foreground/background transition we MIGRATE every in-flight transfer
    // between the two (`migrateTasks`): cancel on the source session, re-enqueue on
    // the destination. HLS segments are small 6 s fmp4 chunks, so a cancelled task
    // just restarts from scratch on the other session — no resume-data dance.
    //
    // History: a foreground default session delivered smooth live progress but died
    // ~30s after backgrounding; an early *pure*-background attempt appeared to sit at
    // 0% until relaunch because the AppDelegate `handleEventsForBackgroundURLSession`
    // hook was missing, so the session never flushed its delegate events. With that
    // hook in place (see AppDelegate + handleBackgroundEvents below) the background
    // session delivers per-file completions on wake. Both sessions share `self` as
    // delegate; the delegate methods key off `taskDescription` (sha + file) and so
    // handle tasks from either session identically.
    private static let bgSessionIdentifier = "com.streamlink.bundledownloader"
    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.background(withIdentifier: BundleDownloadManager.bgSessionIdentifier)
        cfg.allowsCellularAccess = true
        cfg.isDiscretionary = false
        cfg.sessionSendsLaunchEvents = true
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()
    // The fast, in-process default session used whenever the app is foreground.
    // `waitsForConnectivity` lets a task queued during a brief blip wait for the link
    // instead of erroring (our retry loop still covers mid-transfer drops).
    private lazy var fgSession: URLSession = {
        let cfg = URLSessionConfiguration.default
        cfg.allowsCellularAccess = true
        cfg.waitsForConnectivity = true
        cfg.timeoutIntervalForResource = 7 * 24 * 60 * 60
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()
    // True while the app is in the foreground. Only read/written on `queue` (the
    // lifecycle notifications hop onto it), so `enqueue` can pick a session safely.
    // Defaults true: downloads are only ever kicked off from foreground JS.
    private var appActive = true
    private var bgTask: UIBackgroundTaskIdentifier = .invalid
    // Completion handler the OS hands us when it relaunches the app to deliver
    // finished background transfers; invoked once the session flushes its events.
    private var bgEventsCompletion: (() -> Void)?

    private let queue = DispatchQueue(label: "com.streamlink.bundledownloader.state")
    private let fm = FileManager.default

    // Transport errors that mean "the network blipped" rather than "this file is
    // permanently unfetchable" — we retry these (indefinitely) instead of dropping
    // the bundle. See retryOrFail.
    private static let transientURLErrorCodes: Set<Int> = [
        NSURLErrorTimedOut, NSURLErrorCannotConnectToHost, NSURLErrorNetworkConnectionLost,
        NSURLErrorNotConnectedToInternet, NSURLErrorDNSLookupFailed, NSURLErrorCannotFindHost,
        NSURLErrorResourceUnavailable, NSURLErrorInternationalRoamingOff, NSURLErrorDataNotAllowed,
        NSURLErrorSecureConnectionFailed, NSURLErrorRequestBodyStreamExhausted,
    ]

    // Active downloads keyed by cache sha.
    private final class Job {
        let itemId: String, filePath: String, name: String, baseUrl: String, token: String?
        var files: [BundleFile]
        var doneBytes: [String: Int64] = [:]   // fileName -> bytes confirmed on disk
        var liveBytes: [String: Int64] = [:]    // fileName -> bytes written by an in-flight task
        var pending: Set<String> = []           // file names still downloading
        var attempts: [String: Int] = [:]       // fileName -> transient-error retry count
        init(itemId: String, filePath: String, name: String, baseUrl: String, token: String?, files: [BundleFile]) {
            self.itemId = itemId; self.filePath = filePath; self.name = name
            self.baseUrl = baseUrl; self.token = token; self.files = files
        }
        var totalBytes: Int64 { files.reduce(0) { $0 + max($1.size, 0) } }
    }
    private var jobs: [String: Job] = [:]

    // MARK: storage layout

    private var root: URL {
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("StreamLinkBundles", isDirectory: true)
        if !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            excludeFromBackup(dir)
        }
        return dir
    }
    private func bundleDir(_ sha: String) -> URL { root.appendingPathComponent(sha, isDirectory: true) }
    private var indexURL: URL { root.appendingPathComponent("index.json") }
    // The durable download *queue* (intent), separate from `index.json` (which
    // tracks bundles already handed to the URLSession). An entry lives here from
    // the moment the user taps Download until the bundle is fully on disk, so a
    // download survives an app kill / multi-hour outage and the dashboard can
    // re-drive it on launch + reconnect.
    private var queueURL: URL { root.appendingPathComponent("queue.json") }

    private func excludeFromBackup(_ url: URL) {
        var u = url
        var values = URLResourceValues(); values.isExcludedFromBackup = true
        try? u.setResourceValues(values)
    }

    // MARK: index persistence

    private func readIndex() -> [String: [String: Any]] {
        guard let data = try? Data(contentsOf: indexURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] else { return [:] }
        return obj
    }
    private func writeIndex(_ idx: [String: [String: Any]]) {
        if let data = try? JSONSerialization.data(withJSONObject: idx, options: []) {
            try? data.write(to: indexURL, options: .atomic)
        }
    }

    // MARK: download-queue persistence (intent — survives kill + long outage)

    private func queueKey(_ itemId: String, _ filePath: String) -> String {
        return itemId + "\u{0000}" + filePath
    }
    private func readQueue() -> [String: [String: Any]] {
        guard let data = try? Data(contentsOf: queueURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: [String: Any]] else { return [:] }
        return obj
    }
    private func writeQueue(_ q: [String: [String: Any]]) {
        if let data = try? JSONSerialization.data(withJSONObject: q, options: []) {
            try? data.write(to: queueURL, options: .atomic)
        }
    }

    /// Record the user's intent to download a file. Idempotent — re-enqueuing an
    /// already-queued file just refreshes its fields. The dashboard calls this the
    /// instant the user taps Download, BEFORE any network, so the wish is durable.
    func enqueue(itemId: String, filePath: String, name: String, quality: String?, profileId: String?) {
        queue.sync {
            _ = root   // ensure the dir + backup-exclusion exist
            var q = readQueue()
            var e = q[queueKey(itemId, filePath)] ?? [:]
            e["itemId"] = itemId; e["filePath"] = filePath; e["name"] = name
            if let qa = quality { e["quality"] = qa }
            if let p = profileId { e["profileId"] = p }
            if e["addedAt"] == nil { e["addedAt"] = ISO8601DateFormatter().string(from: Date()) }
            q[queueKey(itemId, filePath)] = e
            writeQueue(q)
        }
    }
    /// Clear a file's queued intent — called once the bundle is fully downloaded
    /// (or the user cancels). After this the file is no longer auto-resumed.
    func dequeue(itemId: String, filePath: String) {
        queue.sync {
            var q = readQueue()
            q[queueKey(itemId, filePath)] = nil
            writeQueue(q)
        }
    }
    /// Every still-wanted download. The dashboard drains this on launch and on the
    /// `online` event, re-driving each entry that isn't already complete on disk —
    /// so a queue built before a connectivity drop resumes when the link returns,
    /// even hours later or across an app relaunch.
    func queueList() -> [[String: Any]] {
        queue.sync { Array(readQueue().values) }
    }

    // Read a `files[]` entry's expected byte size across the shapes the JSON
    // bridge / JSONSerialization can hand back (Int / Double / NSNumber). `0`
    // means "unknown" — treated as "any bytes on disk = present", matching the
    // resume scan in startDownload.
    private func expectedSize(_ f: [String: Any]) -> Int64 {
        (f["size"] as? Int).map { Int64($0) }
            ?? (f["size"] as? Double).map { Int64($0) }
            ?? (f["size"] as? NSNumber).map { $0.int64Value }
            ?? 0
    }

    // Self-heal completion from disk. A bundle whose every expected file is
    // present at its expected size IS complete — even if the in-memory `Job`
    // that would have called markComplete was gone when the last file landed.
    // That happens routinely during a BULK download: the app is backgrounded for
    // a long time, the background URLSession keeps finishing files, and iOS
    // relaunches the app headless to deliver those events with `jobs` empty — so
    // `didFinishDownloadingTo` moves the file to disk but bails before
    // markComplete, leaving `complete:false` in index.json forever. downloads.html
    // filters on `complete`, so a fully-downloaded episode never appears in
    // Downloads. This pass (run whenever the index is read for display, and after
    // background events flush) repairs those entries. Returns the set of shas it
    // newly flipped to complete. Assumes `queue`.
    @discardableResult
    private func reconcileIndexLocked() -> [String] {
        var idx = readIndex()
        var healed: [String] = []
        for (sha, entry) in Array(idx) {       // snapshot — we mutate `idx` in the loop
            if (entry["complete"] as? Bool) == true { continue }
            let files = (entry["files"] as? [[String: Any]]) ?? []
            if files.isEmpty { continue }
            let dir = bundleDir(sha)
            var allPresent = true
            for f in files {
                guard let n = f["name"] as? String else { allPresent = false; break }
                let want = expectedSize(f)
                let onDisk = (try? dir.appendingPathComponent(n).resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? -1
                if onDisk < 0 || (want > 0 && onDisk != want) { allPresent = false; break }
            }
            if allPresent {
                var e = entry; e["complete"] = true; idx[sha] = e
                healed.append(sha)
            }
        }
        if !healed.isEmpty { writeIndex(idx) }
        return healed
    }

    // MARK: public API (called from the plugin, hops onto `queue`)

    struct StartResult { let dir: String; let alreadyComplete: Bool }

    func startDownload(itemId: String, filePath: String, cacheKey: String,
                       name: String, baseUrl: String, files: [BundleFile],
                       token: String?, masterContent: String? = nil,
                       meta: [String: Any]? = nil) throws -> StartResult {
        var result: StartResult!
        var thrown: Error?
        queue.sync {
            let dir = bundleDir(cacheKey)
            do {
                try fm.createDirectory(at: dir, withIntermediateDirectories: true)
                excludeFromBackup(root)
            } catch { thrown = error; return }

            // The host trims master.m3u8 to the highest video rung only (dropping the
            // ABR down-rungs from `files`) and ships the rewritten playlist inline so
            // we never fetch — nor reference — a dropped rung. Write it to disk before
            // the resume scan below, where its `files` entry (carrying the trimmed
            // byte size) is then seen as already-complete and skipped.
            if let mc = masterContent, let data = mc.data(using: .utf8) {
                try? data.write(to: dir.appendingPathComponent("master.m3u8"), options: .atomic)
            }

            // Record (or refresh) the index entry up front so getLocal/list see it
            // even mid-download. Preserve any prior `meta` if this call omits it.
            var idx = readIndex()
            var entry: [String: Any] = [
                "itemId": itemId, "filePath": filePath, "name": name,
                "baseUrl": baseUrl, "complete": false,
                "files": files.map { ["name": $0.name, "size": $0.size] },
            ]
            if let m = meta { entry["meta"] = m }
            else if let prev = idx[cacheKey]?["meta"] { entry["meta"] = prev }
            idx[cacheKey] = entry
            writeIndex(idx)

            // Which files are already fully on disk (resume / no-op re-download)?
            let job = Job(itemId: itemId, filePath: filePath, name: name,
                          baseUrl: baseUrl, token: token, files: files)
            var toFetch: [BundleFile] = []
            for f in files {
                let dest = dir.appendingPathComponent(f.name)
                let onDisk = (try? dest.resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? -1
                if onDisk >= 0 && (f.size <= 0 || onDisk == f.size) {
                    job.doneBytes[f.name] = onDisk
                } else {
                    toFetch.append(f)
                }
            }

            if toFetch.isEmpty {
                markComplete(cacheKey, job: job)
                result = StartResult(dir: dir.path, alreadyComplete: true)
                return
            }

            job.pending = Set(toFetch.map { $0.name })
            jobs[cacheKey] = job
            beginBgTaskIfNeeded()
            for f in toFetch { enqueue(sha: cacheKey, file: f, job: job) }
            result = StartResult(dir: dir.path, alreadyComplete: false)
        }
        if let e = thrown { throw e }
        return result
    }

    func getLocal(itemId: String, filePath: String) -> [String: Any] {
        queue.sync {
            reconcileIndexLocked()
            let idx = readIndex()
            guard let (sha, entry) = idx.first(where: {
                ($0.value["itemId"] as? String) == itemId && ($0.value["filePath"] as? String) == filePath
            }) else {
                return ["found": false, "complete": false, "bytesDone": 0, "bytesTotal": 0, "fileCount": 0]
            }
            let files = (entry["files"] as? [[String: Any]]) ?? []
            let total = files.reduce(Int64(0)) { $0 + ((($1["size"] as? Int).map { Int64($0) }) ?? 0) }
            let dir = bundleDir(sha)
            var done: Int64 = 0
            for f in files {
                guard let n = f["name"] as? String else { continue }
                let sz = (try? dir.appendingPathComponent(n).resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? 0
                done += sz
            }
            let complete = (entry["complete"] as? Bool) ?? false
            var res: [String: Any] = ["found": true, "complete": complete, "sha": sha, "dir": dir.path,
                    "bytesDone": done, "bytesTotal": total, "fileCount": files.count]
            if let m = entry["meta"] { res["meta"] = m }
            return res
        }
    }

    func list() -> [[String: Any]] {
        queue.sync {
            reconcileIndexLocked()
            let idx = readIndex()
            return idx.map { sha, entry -> [String: Any] in
                let files = (entry["files"] as? [[String: Any]]) ?? []
                let total = files.reduce(Int64(0)) { $0 + ((($1["size"] as? Int).map { Int64($0) }) ?? 0) }
                let dir = bundleDir(sha)
                var done: Int64 = 0
                for f in files {
                    guard let n = f["name"] as? String else { continue }
                    done += (try? dir.appendingPathComponent(n).resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? 0
                }
                var row: [String: Any] = [
                    "sha": sha,
                    "dir": dir.path,        // the offline player passes this to LocalMediaServer.start
                    "itemId": entry["itemId"] as? String ?? "",
                    "filePath": entry["filePath"] as? String ?? "",
                    "name": entry["name"] as? String ?? "",
                    "complete": (entry["complete"] as? Bool) ?? false,
                    "bytesTotal": total, "bytesDone": done, "fileCount": files.count,
                ]
                if let m = entry["meta"] { row["meta"] = m }
                return row
            }
        }
    }

    func remove(sha: String) {
        queue.sync {
            cancelLocked(sha: sha)
            try? fm.removeItem(at: bundleDir(sha))
            var idx = readIndex()
            // Drop the durable queue intent too, so a removed download is never
            // resurrected by the launch / reconnect resumer.
            if let entry = idx[sha],
               let itemId = entry["itemId"] as? String, let filePath = entry["filePath"] as? String {
                var q = readQueue(); q[queueKey(itemId, filePath)] = nil; writeQueue(q)
            }
            idx[sha] = nil; writeIndex(idx)
        }
    }
    func remove(itemId: String, filePath: String) {
        let sha: String? = queue.sync {
            readIndex().first(where: {
                ($0.value["itemId"] as? String) == itemId && ($0.value["filePath"] as? String) == filePath
            })?.key
        }
        if let s = sha { remove(sha: s) }
    }

    func cancel(sha: String) { queue.sync { cancelLocked(sha: sha) } }

    func bytesUsed() -> Int64 {
        queue.sync {
            var total: Int64 = 0
            if let en = fm.enumerator(at: root, includingPropertiesForKeys: [.fileSizeKey]) {
                for case let url as URL in en {
                    total += (try? url.resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? 0
                }
            }
            return total
        }
    }

    // MARK: internals (assume `queue`)

    private func cancelLocked(sha: String) {
        jobs[sha] = nil
        endBgTaskIfIdle()
        // A file's task can be on either session (depending on app state when it was
        // enqueued / last migrated), so sweep both.
        let cancelMatching: (URLSession) -> Void = { s in
            s.getAllTasks { tasks in
                for t in tasks where (t.taskDescription?.hasPrefix(sha + "\u{0000}") ?? false) { t.cancel() }
            }
        }
        cancelMatching(session)
        cancelMatching(fgSession)
    }

    private func enqueue(sha: String, file: BundleFile, job: Job, session sess: URLSession? = nil) {
        let urlStr = job.baseUrl + file.name
        guard let url = URL(string: urlStr) else {
            emitError(sha: sha, job: job, message: "Bad file URL: \(urlStr)"); return
        }
        var req = URLRequest(url: url)
        if let tok = job.token, !tok.isEmpty { req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization") }
        // Foreground ⇒ fast in-process default session; suspended ⇒ background
        // session that survives suspend. A migration passes the destination explicitly.
        let chosen = sess ?? (appActive ? fgSession : session)
        let task = chosen.downloadTask(with: req)
        task.taskDescription = sha + "\u{0000}" + file.name
        task.resume()
    }

    // Move every in-flight transfer for the active jobs from one session to the
    // other on a foreground/background transition. Cancel on the source (the cancel
    // is swallowed by didCompleteWithError's NSURLErrorCancelled guard, so it never
    // surfaces an error or triggers a retry) and re-enqueue the same file on the
    // destination. Only files still pending are moved; segments are small so the
    // restart-from-scratch is cheap. Assumes it's invoked on `queue`.
    private func migrateTasks(from src: URLSession, to dst: URLSession) {
        src.getAllTasks { tasks in
            guard !tasks.isEmpty else { return }
            self.queue.async {
                for t in tasks {
                    guard t.state == .running || t.state == .suspended else { continue }
                    guard let (sha, file) = self.decode(taskDescription: t.taskDescription),
                          let job = self.jobs[sha], job.pending.contains(file),
                          let f = job.files.first(where: { $0.name == file }) else { continue }
                    t.cancel()
                    job.liveBytes[file] = nil   // this attempt's bytes are gone; the new task re-counts
                    self.enqueue(sha: sha, file: f, job: job, session: dst)
                }
            }
        }
    }

    // One file's download failed. A **transient** failure (network/proxy blip)
    // never drops the bundle: we re-enqueue the file with a capped backoff and keep
    // doing so **indefinitely**, because the user explicitly asked for this download
    // and the intent is persisted in the durable queue — losing connection for
    // hours must not abandon it. On a background URLSession the fresh task waits for
    // connectivity, so it resumes the moment the link returns; the linear backoff
    // (capped at 30 s) just paces the retries while offline. Only a **non-transient**
    // failure (bad URL, 404, on-disk move error) surfaces an error + cancels — those
    // never fix themselves. Assumes `queue` and `job === jobs[sha]`.
    private func retryOrFail(sha: String, fileName: String, job: Job, message: String, transient: Bool) {
        if transient, let f = job.files.first(where: { $0.name == fileName }) {
            let n = (job.attempts[fileName] ?? 0) + 1
            job.attempts[fileName] = n
            job.liveBytes[fileName] = nil               // this attempt's bytes are gone
            let delay = Double(min(n * 3, 30))          // linear backoff, capped at 30 s
            queue.asyncAfter(deadline: .now() + delay) {
                guard self.jobs[sha] != nil else { return }   // cancelled meanwhile
                self.enqueue(sha: sha, file: f, job: job)
            }
            return
        }
        emitError(sha: sha, job: job, message: message)
        cancelLocked(sha: sha)
    }

    private func markComplete(_ sha: String, job: Job) {
        var idx = readIndex()
        if var entry = idx[sha] { entry["complete"] = true; idx[sha] = entry; writeIndex(idx) }
        jobs[sha] = nil
        endBgTaskIfIdle()
        emit("bundleComplete", ["sha": sha, "itemId": job.itemId, "filePath": job.filePath, "dir": bundleDir(sha).path])
        // End the Live Activity (terminal frame) when the last job finishes, else
        // keep it showing the remaining downloads.
        if jobs.isEmpty {
            DownloadLiveActivity.shared.end(title: job.name, finished: true, failed: false,
                                            filesDone: job.files.count, fileCount: job.files.count,
                                            bytesTotal: job.totalBytes)
        } else {
            updateLiveActivity(force: true)
        }
    }

    private func emitProgress(sha: String, job: Job) {
        let confirmed = job.doneBytes.values.reduce(0, +)
        let live = job.liveBytes.values.reduce(0, +)
        let total = max(job.totalBytes, 1)
        let frac = min(1.0, Double(confirmed + live) / Double(total))
        emit("bundleProgress", [
            "sha": sha, "itemId": job.itemId, "filePath": job.filePath,
            "bytesDone": confirmed + live, "bytesTotal": job.totalBytes,
            "fraction": frac, "filesDone": job.doneBytes.count, "fileCount": job.files.count,
        ])
        updateLiveActivity(force: false)
    }

    private func emitError(sha: String, job: Job, message: String) {
        jobs[sha] = nil
        endBgTaskIfIdle()
        emit("bundleError", ["sha": sha, "itemId": job.itemId, "filePath": job.filePath, "message": message])
        if jobs.isEmpty {
            DownloadLiveActivity.shared.end(title: job.name, finished: false, failed: true,
                                            filesDone: job.doneBytes.count, fileCount: job.files.count,
                                            bytesTotal: job.totalBytes)
        } else {
            updateLiveActivity(force: true)
        }
    }

    private func emit(_ name: String, _ payload: [String: Any]) {
        DispatchQueue.main.async { [weak self] in self?.onEvent?(name, payload) }
    }

    // Keep the app alive briefly if it's backgrounded mid-download (a default
    // session is suspended with the app otherwise). Held while any job is active.
    private func beginBgTaskIfNeeded() {
        guard bgTask == .invalid else { return }
        DispatchQueue.main.async {
            self.bgTask = UIApplication.shared.beginBackgroundTask(withName: "StreamLinkBundleDownload") {
                // Expiration: end the assertion (downloads will pause until foreground).
                if self.bgTask != .invalid { UIApplication.shared.endBackgroundTask(self.bgTask); self.bgTask = .invalid }
            }
        }
    }
    private func endBgTaskIfIdle() {
        guard jobs.isEmpty, bgTask != .invalid else { return }
        let id = bgTask; bgTask = .invalid
        DispatchQueue.main.async { UIApplication.shared.endBackgroundTask(id) }
    }

    // A SEPARATE assertion held by the JS download orchestration (manifest fetch +
    // host-prep poll + handoff), independent of the job-keyed bgTask above so the
    // two lifecycles never clobber each other. Ref-counted because several pool
    // lanes can be orchestrating at once. Main-thread state (UIApplication APIs).
    private var orchTask: UIBackgroundTaskIdentifier = .invalid
    private var orchCount = 0
    func holdOrchestrationBackground() {
        DispatchQueue.main.async {
            self.orchCount += 1
            guard self.orchTask == .invalid else { return }
            self.orchTask = UIApplication.shared.beginBackgroundTask(withName: "StreamLinkDownloadPrep") {
                if self.orchTask != .invalid { UIApplication.shared.endBackgroundTask(self.orchTask); self.orchTask = .invalid }
                self.orchCount = 0
            }
        }
    }
    func releaseOrchestrationBackground() {
        DispatchQueue.main.async {
            if self.orchCount > 0 { self.orchCount -= 1 }
            guard self.orchCount == 0, self.orchTask != .invalid else { return }
            let id = self.orchTask; self.orchTask = .invalid
            UIApplication.shared.endBackgroundTask(id)
        }
    }

    // Called from AppDelegate.application(_:handleEventsForBackgroundURLSession:)
    // when the OS relaunches us to deliver finished background transfers. Force
    // our lazy session to exist (so it re-attaches to the running tasks) and stash
    // the handler; we fire it from urlSessionDidFinishEvents once events flush.
    func handleBackgroundEvents(identifier: String, completionHandler: @escaping () -> Void) {
        guard identifier == BundleDownloadManager.bgSessionIdentifier else { completionHandler(); return }
        queue.async {
            self.bgEventsCompletion = completionHandler
            _ = self.session   // ensure the session is recreated and reconnected
        }
    }

    func urlSessionDidFinishEvents(forBackgroundSession session: URLSession) {
        queue.async {
            // Files that finished while the app was suspended/killed were moved to
            // disk, but their in-memory `Job` was gone so markComplete never ran.
            // Now that the batch has flushed, repair index.json from disk and tell
            // any live listener about the bundles that just became complete.
            for sha in self.reconcileIndexLocked() {
                let entry = self.readIndex()[sha] ?? [:]
                self.emit("bundleComplete", [
                    "sha": sha,
                    "itemId": entry["itemId"] as? String ?? "",
                    "filePath": entry["filePath"] as? String ?? "",
                    "dir": self.bundleDir(sha).path,
                ])
            }
            let handler = self.bgEventsCompletion
            self.bgEventsCompletion = nil
            DispatchQueue.main.async { handler?() }
        }
    }

    // MARK: Live Activity (download progress)

    // Build an aggregate snapshot across all in-flight jobs and push it to the
    // download Live Activity. `force` bypasses the throttle (per-file completion).
    private func updateLiveActivity(force: Bool) {
        guard !jobs.isEmpty else { return }
        var done: Int64 = 0, total: Int64 = 0, filesDone = 0, fileCount = 0
        var soleName = ""
        for (_, j) in jobs {
            done += j.doneBytes.values.reduce(0, +) + j.liveBytes.values.reduce(0, +)
            total += j.totalBytes
            filesDone += j.doneBytes.count
            fileCount += j.files.count
            soleName = j.name
        }
        let title = jobs.count == 1 ? soleName : "\(jobs.count) downloads"
        let frac = total > 0 ? min(1.0, Double(done) / Double(total)) : 0
        DownloadLiveActivity.shared.sync(title: title, bytesDone: done, bytesTotal: total,
                                         fraction: frac, filesDone: filesDone,
                                         fileCount: fileCount, force: force)
    }

    private func decode(taskDescription: String?) -> (sha: String, file: String)? {
        guard let d = taskDescription, let r = d.range(of: "\u{0000}") else { return nil }
        return (String(d[..<r.lowerBound]), String(d[r.upperBound...]))
    }

    // MARK: URLSessionDownloadDelegate

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didFinishDownloadingTo location: URL) {
        // MUST move synchronously — `location` is purged after this returns.
        guard let (sha, fileName) = decode(taskDescription: downloadTask.taskDescription) else { return }
        let dir = bundleDir(sha)
        let dest = dir.appendingPathComponent(fileName)
        // Verify HTTP success; a 4xx/5xx still "finishes downloading" (the error body).
        let http = downloadTask.response as? HTTPURLResponse
        let code = http?.statusCode ?? 0
        let ok = code >= 200 && code < 300
        var moveError: String?
        var errTransient = false
        if ok {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            try? fm.removeItem(at: dest)
            do { try fm.moveItem(at: location, to: dest) } catch { moveError = error.localizedDescription }
        } else {
            moveError = "HTTP \(code) for \(fileName)"
            // A flaky tunnel/proxy can return 5xx/429/408 mid-blip — retry those
            // like a network error rather than dropping the bundle.
            errTransient = (code == 408 || code == 429 || (500...599).contains(code))
        }
        queue.async {
            guard let job = self.jobs[sha] else { return }
            if let err = moveError {
                self.retryOrFail(sha: sha, fileName: fileName, job: job, message: err, transient: errTransient)
                return
            }
            let size = (try? dest.resourceValues(forKeys: [.fileSizeKey]).fileSize).flatMap { Int64($0) } ?? 0
            job.doneBytes[fileName] = size
            job.liveBytes[fileName] = nil
            job.pending.remove(fileName)
            self.emitProgress(sha: sha, job: job)
            if job.pending.isEmpty { self.markComplete(sha, job: job) }
        }
    }

    func urlSession(_ session: URLSession, downloadTask: URLSessionDownloadTask,
                    didWriteData bytesWritten: Int64, totalBytesWritten: Int64,
                    totalBytesExpectedToWrite: Int64) {
        guard let (sha, fileName) = decode(taskDescription: downloadTask.taskDescription) else { return }
        queue.async {
            guard let job = self.jobs[sha] else { return }
            job.liveBytes[fileName] = totalBytesWritten
            self.emitProgress(sha: sha, job: job)
        }
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        guard let error = error else { return }   // success handled in didFinishDownloadingTo
        guard let (sha, fileName) = decode(taskDescription: task.taskDescription) else { return }
        // A deliberate cancel (remove/cancel) drops the job first — don't surface it.
        let nsErr = error as NSError
        if nsErr.code == NSURLErrorCancelled { return }
        queue.async {
            guard let job = self.jobs[sha] else { return }
            // A brief connectivity loss mid-transfer must NOT drop the whole bundle.
            let isTransient = nsErr.domain == NSURLErrorDomain
                && Self.transientURLErrorCodes.contains(nsErr.code)
            self.retryOrFail(sha: sha, fileName: fileName, job: job,
                             message: error.localizedDescription, transient: isTransient)
        }
    }
}
