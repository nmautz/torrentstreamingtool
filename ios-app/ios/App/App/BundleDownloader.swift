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
//  A *background* URLSession keeps downloads running while the app is suspended;
//  completed files are durable across an app kill (M2 "survive relaunch"). The
//  session + delegate live in a process-wide singleton (`BundleDownloadManager`)
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
}

// MARK: - Model

struct BundleFile { let name: String; let size: Int64 }

// MARK: - Download manager (process-wide singleton owning the URLSession + delegate)

final class BundleDownloadManager: NSObject, URLSessionDownloadDelegate {
    static let shared = BundleDownloadManager()

    /// Called for "bundleProgress" / "bundleComplete" / "bundleError".
    var onEvent: ((String, [String: Any]) -> Void)?

    // A *background* URLSession so downloads keep running — and complete — while
    // the app is suspended (the whole point of "downloads work when minimized").
    // History: a foreground default session delivered smooth live progress but
    // died ~30s after backgrounding; an early background attempt appeared to sit
    // at 0% until relaunch because the AppDelegate
    // `handleEventsForBackgroundURLSession` hook was missing, so the session never
    // flushed its delegate events. With that hook in place (see AppDelegate +
    // handleBackgroundEvents below) the background session delivers per-file
    // completions on wake. While the app is foreground, didWriteData fires live so
    // in-app progress stays smooth; while suspended, byte progress is coarse
    // (batched) but per-file completion still advances the Live Activity.
    private static let bgSessionIdentifier = "com.streamlink.bundledownloader"
    private lazy var session: URLSession = {
        let cfg = URLSessionConfiguration.background(withIdentifier: BundleDownloadManager.bgSessionIdentifier)
        cfg.allowsCellularAccess = true
        cfg.isDiscretionary = false
        cfg.sessionSendsLaunchEvents = true
        return URLSession(configuration: cfg, delegate: self, delegateQueue: nil)
    }()
    private var bgTask: UIBackgroundTaskIdentifier = .invalid
    // Completion handler the OS hands us when it relaunches the app to deliver
    // finished background transfers; invoked once the session flushes its events.
    private var bgEventsCompletion: (() -> Void)?

    private let queue = DispatchQueue(label: "com.streamlink.bundledownloader.state")
    private let fm = FileManager.default

    // Active downloads keyed by cache sha.
    private final class Job {
        let itemId: String, filePath: String, name: String, baseUrl: String, token: String?
        var files: [BundleFile]
        var doneBytes: [String: Int64] = [:]   // fileName -> bytes confirmed on disk
        var liveBytes: [String: Int64] = [:]    // fileName -> bytes written by an in-flight task
        var pending: Set<String> = []           // file names still downloading
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
            var idx = readIndex(); idx[sha] = nil; writeIndex(idx)
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
        session.getAllTasks { tasks in
            for t in tasks where (t.taskDescription?.hasPrefix(sha + "\u{0000}") ?? false) { t.cancel() }
        }
    }

    private func enqueue(sha: String, file: BundleFile, job: Job) {
        let urlStr = job.baseUrl + file.name
        guard let url = URL(string: urlStr) else {
            emitError(sha: sha, job: job, message: "Bad file URL: \(urlStr)"); return
        }
        var req = URLRequest(url: url)
        if let tok = job.token, !tok.isEmpty { req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization") }
        let task = session.downloadTask(with: req)
        task.taskDescription = sha + "\u{0000}" + file.name
        task.resume()
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
        let ok = (http?.statusCode ?? 0) >= 200 && (http?.statusCode ?? 0) < 300
        var moveError: String?
        if ok {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            try? fm.removeItem(at: dest)
            do { try fm.moveItem(at: location, to: dest) } catch { moveError = error.localizedDescription }
        } else {
            moveError = "HTTP \(http?.statusCode ?? -1) for \(fileName)"
        }
        queue.async {
            guard let job = self.jobs[sha] else { return }
            if let err = moveError {
                self.emitError(sha: sha, job: job, message: err)
                self.cancelLocked(sha: sha)
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
        guard let (sha, _) = decode(taskDescription: task.taskDescription) else { return }
        // A deliberate cancel (remove/cancel) drops the job first — don't surface it.
        let nsErr = error as NSError
        if nsErr.code == NSURLErrorCancelled { return }
        queue.async {
            guard let job = self.jobs[sha] else { return }
            self.emitError(sha: sha, job: job, message: error.localizedDescription)
            self.cancelLocked(sha: sha)
        }
    }
}
