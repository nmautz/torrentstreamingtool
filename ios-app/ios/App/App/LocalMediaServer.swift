//
//  LocalMediaServer.swift
//  StreamLink iOS — M1 / Gate 1b
//
//  A dependency-free localhost static-file HTTP server (Network.framework
//  NWListener) that serves a downloaded `.offline_cache/<sha>/` HLS bundle so
//  the existing web player can swap its `master_url` to http://127.0.0.1:<port>/.
//
//  It is a faithful native port of the validated Python spike (Gate 1a): GET/HEAD
//  + correct HLS MIME (mirrors `_HLS_MIME` in main.py) + byte-range support, which
//  iOS native HLS (<video>.src) requires for fmp4 segments. Bound to the loopback
//  interface only, so nothing on the LAN can reach it.
//
//  JS surface (Capacitor plugin "LocalMediaServer"):
//    start({ path? , bundledPath? , playerRoot? , proxyHost? , proxyToken? }) -> { url, port, root }
//    stop()                          -> {}
//    info()                          -> { running, url?, port?, root? }
//    ensureRunning()                 -> { running, url?, port?, healed }
//
//  `path`        absolute filesystem dir (a downloaded bundle, M2+).
//  `bundledPath` dir relative to the app's bundled web assets ("public/<bundledPath>"),
//                used by the Gate 1b self-test to serve the shipped sample bundle.
//  `playerRoot`  offline cached-dashboard snapshot dir (docs/PLAYER_CACHE_PLAN.md):
//                served at /, with the StreamLinkBundles storage dir mounted at
//                /StreamLinkBundles/.
//  `proxyHost`   + `proxyToken` (v8.7 — proxied playback session): when set, ANY
//                request that isn't a local snapshot/bundle file is reverse-proxied
//                to this host (e.g. https://192.168.1.20:8000) with the bearer token
//                injected — so the loopback page behaves as a full online dashboard
//                (`/api/*`, SSE, server-stream media) while downloaded bundles play
//                same-origin. See docs/STREAMING.md § proxied playback.
//
//  Only one server runs at a time: start() stops any previous instance first
//  (one bundle is played at a time — matches the plan).
//

import Foundation
import Capacitor
import Network
import Security
import UIKit

@objc(LocalMediaServer)
public class LocalMediaServer: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "LocalMediaServer"
    public let jsName = "LocalMediaServer"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "info",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "ensureRunning", returnType: CAPPluginReturnPromise),
    ]

    private var server: HLSStaticServer?

    // MARK: - JS methods

    @objc func start(_ call: CAPPluginCall) {
        // Resolve the directory root to serve.
        let root: URL
        var bundlesMount: URL? = nil
        if let pr = call.getString("playerRoot"), !pr.isEmpty {
            // Player mode (offline cached dashboard — docs/PLAYER_CACHE_PLAN.md):
            // serve the snapshot dir at /, and mount its parent (the
            // StreamLinkBundles storage dir) at /StreamLinkBundles/ so every
            // downloaded bundle is same-origin and the server never restarts
            // while it's serving the page itself.
            root = URL(fileURLWithPath: pr, isDirectory: true)
            bundlesMount = root.deletingLastPathComponent()
        } else if let p = call.getString("path"), !p.isEmpty {
            root = URL(fileURLWithPath: p, isDirectory: true)
        } else if let bp = call.getString("bundledPath"), !bp.isEmpty {
            guard let base = Bundle.main.url(forResource: "public", withExtension: nil) else {
                call.reject("Bundled web assets not found.")
                return
            }
            root = base.appendingPathComponent(bp, isDirectory: true)
        } else {
            call.reject("start() requires `playerRoot`, `path` or `bundledPath`.")
            return
        }

        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDir), isDir.boolValue else {
            call.reject("Directory does not exist: \(root.path)")
            return
        }

        // Proxied playback session (v8.7): any non-local request is forwarded to
        // this host so the loopback page is a full online dashboard.
        var proxyURL: URL? = nil
        if let s = call.getString("proxyHost"), !s.isEmpty {
            var trimmed = s
            while trimmed.hasSuffix("/") { trimmed.removeLast() }
            proxyURL = URL(string: trimmed)
        }
        let proxyToken = call.getString("proxyToken")

        // Single active server — tear down any previous one first.
        server?.stop()
        let srv = HLSStaticServer(root: root, bundlesMount: bundlesMount,
                                  proxyHost: proxyURL, proxyToken: proxyToken)
        server = srv
        srv.start { [weak self] result in
            switch result {
            case .success(let port):
                let url = "http://127.0.0.1:\(port)/"
                // Offline playback is a chain — bundle on disk, loopback server
                // up, page pointed at it — and a break anywhere in it reaches the
                // user as one symptom: the episode does not play. The log knew
                // about the first link and the last; this is the middle one.
                DiagLog.shared.write("lms-start", [
                    "port": Int(port), "root": root.path,
                    "mode": bundlesMount != nil ? "player" : "bundle",
                    "proxied": proxyURL != nil,
                ], cat: "offline")
                call.resolve(["url": url, "port": Int(port), "root": root.path])
            case .failure(let err):
                self?.server = nil
                DiagLog.shared.write("lms-failed", [
                    "root": root.path, "err": err.localizedDescription,
                ], cat: "offline")
                call.reject("Failed to start localhost server: \(err.localizedDescription)")
            }
        }
    }

    @objc func stop(_ call: CAPPluginCall) {
        DiagLog.shared.write("lms-stop", ["running": server != nil], cat: "offline")
        server?.stop()
        server = nil
        call.resolve()
    }

    /// Prove the loopback port still answers and rebind it (SAME port) if it
    /// doesn't. See `HLSStaticServer.heal` for why a suspended app needs this.
    /// The native side already heals itself on every foreground; this is the
    /// JS-visible form, so the page can sequence its own recovery (reloading a
    /// starved <video>, reconnecting SSE) *after* the server is back.
    @objc func ensureRunning(_ call: CAPPluginCall) {
        guard let srv = server else {
            call.resolve(["running": false, "healed": false])
            return
        }
        srv.heal { healed in
            if let port = srv.boundPort {
                call.resolve(["running": true, "healed": healed,
                              "url": "http://127.0.0.1:\(port)/", "port": Int(port)])
            } else {
                call.resolve(["running": false, "healed": healed])
            }
        }
    }

    @objc func info(_ call: CAPPluginCall) {
        if let srv = server, let port = srv.boundPort {
            call.resolve([
                "running": true,
                "url": "http://127.0.0.1:\(port)/",
                "port": Int(port),
                "root": srv.root.path,
            ])
        } else {
            call.resolve(["running": false])
        }
    }
}

// MARK: - NWListener static HLS server

final class HLSStaticServer {
    let root: URL
    /// Player mode only (offline cached dashboard): the StreamLinkBundles storage
    /// dir mounted at /StreamLinkBundles/ alongside the snapshot root. Nil for
    /// normal single-bundle serving. See resolve().
    let bundlesMount: URL?
    /// Proxied playback session (v8.7): when set, requests that don't resolve to a
    /// local snapshot/bundle file are reverse-proxied to this host. See `proxy()`.
    let proxyHost: URL?
    let proxyToken: String?
    private let queue = DispatchQueue(label: "com.streamlink.localmediaserver", attributes: .concurrent)
    private var listener: NWListener?
    private(set) var boundPort: UInt16?
    /// The port this server has claimed for its lifetime. A rebind (see `heal`)
    /// MUST come back on the same number: the page being served is sitting at
    /// `http://127.0.0.1:<port>/`, and a different port would leave its origin
    /// pointing at nothing — every fetch, every media segment, dead.
    private var desiredPort: UInt16 = 0
    /// True once `stop()` was called for real (plugin stop / server swap), so a
    /// lifecycle heal can't resurrect a server we deliberately took down.
    private var stopped = false
    private var foregroundObserver: NSObjectProtocol?

    // Mirror of main.py `_HLS_MIME` plus a safe default.
    private static let mime: [String: String] = [
        "m3u8": "application/vnd.apple.mpegurl",
        "m4s":  "video/iso.segment",
        "mp4":  "video/mp4",
        "vtt":  "text/vtt",
        "json": "application/json",
        "ts":   "video/mp2t",
        // Styled-subtitle assets (libass overlay): raw ASS + embedded fonts.
        // MIME is advisory (SubtitlesOctopus fetches these as text / ArrayBuffer)
        // but mirror `_HLS_MIME` so they're not served as octet-stream.
        "ass":   "text/plain; charset=utf-8",
        "ssa":   "text/plain; charset=utf-8",
        "ttf":   "font/ttf",
        "otf":   "font/otf",
        "ttc":   "font/collection",
        "woff":  "font/woff",
        "woff2": "font/woff2",
        // Player-snapshot assets (offline cached dashboard — docs/PLAYER_CACHE_PLAN.md).
        // WKWebView won't RENDER an octet-stream page, so html at least is load-bearing.
        "html":  "text/html; charset=utf-8",
        "js":    "text/javascript; charset=utf-8",
        "css":   "text/css; charset=utf-8",
        "wasm":  "application/wasm",
        "svg":   "image/svg+xml",
        "ico":   "image/x-icon",
        "map":   "application/json",
    ]

    init(root: URL, bundlesMount: URL? = nil, proxyHost: URL? = nil, proxyToken: String? = nil) {
        self.root = root.standardizedFileURL
        self.bundlesMount = bundlesMount?.standardizedFileURL
        self.proxyHost = proxyHost
        self.proxyToken = proxyToken
    }

    enum StartError: Error, LocalizedError {
        case listenerFailed(Error)
        case noPort
        var errorDescription: String? {
            switch self {
            case .listenerFailed(let e): return e.localizedDescription
            case .noPort: return "No port assigned by the system."
            }
        }
    }

    /// Starts the listener on an ephemeral loopback port. Calls back on the main
    /// thread with the bound port, or an error, exactly once.
    func start(completion: @escaping (Result<UInt16, StartError>) -> Void) {
        stopped = false
        observeForeground()
        bind(port: 0, completion: completion)
    }

    /// Bind the listener. `port` 0 = ephemeral (first start); a real number is a
    /// REBIND of the port we already own (`heal`), which is the only way the
    /// page's origin survives.
    private func bind(port: UInt16, completion: @escaping (Result<UInt16, StartError>) -> Void) {
        let params = NWParameters.tcp
        // Loopback-only: nothing on the LAN can reach the media server.
        params.requiredInterfaceType = .loopback
        params.allowLocalEndpointReuse = true

        let listener: NWListener
        do {
            if port != 0, let p = NWEndpoint.Port(rawValue: port) {
                listener = try NWListener(using: params, on: p)
            } else {
                listener = try NWListener(using: params)   // ephemeral port (OS-assigned)
            }
        } catch {
            DispatchQueue.main.async { completion(.failure(.listenerFailed(error))) }
            return
        }
        self.listener = listener

        var finished = false
        let finishOnce: (Result<UInt16, StartError>) -> Void = { result in
            guard !finished else { return }
            finished = true
            DispatchQueue.main.async { completion(result) }
        }

        listener.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                if let port = listener.port?.rawValue {
                    self?.boundPort = port
                    self?.desiredPort = port
                    finishOnce(.success(port))
                } else {
                    finishOnce(.failure(.noPort))
                }
            case .failed(let error):
                finishOnce(.failure(.listenerFailed(error)))
                // Drop the dead listener but KEEP `desiredPort` — a foreground
                // heal rebinds it. Only a real stop() forgets the port. Identity
                // check: a rebind may already have installed a newer listener,
                // and this handler belongs to the old one.
                listener.cancel()
                if self?.listener === listener {
                    self?.listener = nil
                    self?.boundPort = nil
                }
            default:
                break
            }
        }

        listener.newConnectionHandler = { [weak self] conn in
            self?.handle(conn)
        }
        listener.start(queue: queue)
    }

    func stop() {
        stopped = true
        if let obs = foregroundObserver {
            NotificationCenter.default.removeObserver(obs)
            foregroundObserver = nil
        }
        listener?.cancel()
        listener = nil
        boundPort = nil
        desiredPort = 0
    }

    // MARK: - Surviving a suspension

    // WHY THIS EXISTS
    // iOS closes an app's sockets when it SUSPENDS the process — listeners
    // included. Nothing in `NWListener` necessarily reports that: the object can
    // sit in `.ready` accepting nothing. For a plain single-bundle server that is
    // harmless (playback is over anyway), but player/proxy mode is serving the
    // PAGE ITSELF from this port, so a dead listener is catastrophic and silent:
    // every `/api/*` call through the reverse proxy dies (the dashboard shows
    // "Lost connection to host — reconnecting…" forever), and the episode keeps
    // playing only as far as its buffer reaches. Until 17.14.0 the sole cure was
    // ending playback, which navigates off the loopback origin entirely.
    //
    // The app is normally kept ALIVE through a lock by NativePlayback's AVPlayer
    // (the `audio` background mode), which is why a streamed episode never hit
    // this — but a device bundle had no native master to hand it (fixed in the
    // same version), and background playback is a setting the user can switch
    // off, so the server has to be able to come back on its own regardless.

    private func observeForeground() {
        guard foregroundObserver == nil else { return }
        foregroundObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.didBecomeActiveNotification,
            object: nil, queue: .main
        ) { [weak self] _ in
            self?.heal(completion: nil)
        }
    }

    /// Prove the port still answers; rebind it if it doesn't. `completion` gets
    /// true when a rebind actually happened, on the main thread.
    func heal(completion: ((Bool) -> Void)?) {
        let done: (Bool) -> Void = { healed in
            guard let completion = completion else { return }
            DispatchQueue.main.async { completion(healed) }
        }
        guard !stopped, desiredPort != 0 else { done(false); return }
        probe(port: desiredPort) { [weak self] alive in
            guard let self = self, !self.stopped else { done(false); return }
            if alive { done(false); return }
            let port = self.desiredPort
            self.listener?.cancel()
            self.listener = nil
            self.boundPort = nil
            self.bind(port: port) { [weak self] result in
                // Losing the exact port means the page's origin is unreachable
                // whatever we do; take an ephemeral one anyway so the NEXT play
                // (which starts its own server) isn't poisoned too.
                if case .failure = result, let self = self, !self.stopped {
                    self.bind(port: 0) { _ in }
                }
                done(true)
            }
        }
    }

    /// One loopback TCP connect against our own port. `.waiting` counts as dead:
    /// on loopback that is a refused connection being retried, not a slow one.
    private func probe(port: UInt16, completion: @escaping (Bool) -> Void) {
        guard let p = NWEndpoint.Port(rawValue: port) else { completion(false); return }
        let params = NWParameters.tcp
        params.requiredInterfaceType = .loopback
        let conn = NWConnection(host: .ipv4(.loopback), port: p, using: params)
        var finished = false
        let finish: (Bool) -> Void = { ok in
            guard !finished else { return }
            finished = true
            conn.stateUpdateHandler = nil
            conn.cancel()
            completion(ok)
        }
        conn.stateUpdateHandler = { state in
            switch state {
            case .ready:                     finish(true)
            case .failed, .cancelled, .waiting: finish(false)
            default: break
            }
        }
        conn.start(queue: queue)
        queue.asyncAfter(deadline: .now() + 1.5) { finish(false) }
    }

    // MARK: connection handling

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        receiveRequest(conn, buffer: Data())
    }

    /// Accumulate bytes until the end of the HTTP request headers (\r\n\r\n).
    private func receiveRequest(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            guard let self = self else { conn.cancel(); return }
            var buf = buffer
            if let data = data { buf.append(data) }

            if let headerEnd = self.rangeOfHeaderTerminator(in: buf) {
                let headerData = buf.subdata(in: 0..<headerEnd.lowerBound)
                // Bytes already received past the header terminator are the start of
                // the request body (proxied POST/PUT) — hand them along.
                let initialBody = headerEnd.upperBound < buf.count
                    ? buf.subdata(in: headerEnd.upperBound..<buf.count) : Data()
                self.respond(conn, headerData: headerData, initialBody: initialBody)
                return
            }
            if error != nil || isComplete || buf.count > 64 * 1024 {
                conn.cancel()
                return
            }
            self.receiveRequest(conn, buffer: buf)
        }
    }

    private func rangeOfHeaderTerminator(in data: Data) -> Range<Data.Index>? {
        let term = Data("\r\n\r\n".utf8)
        return data.range(of: term)
    }

    private func respond(_ conn: NWConnection, headerData: Data, initialBody: Data) {
        guard let header = String(data: headerData, encoding: .utf8) else {
            sendStatus(conn, 400, "Bad Request"); return
        }
        let lines = header.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { sendStatus(conn, 400, "Bad Request"); return }
        let parts = requestLine.split(separator: " ")
        guard parts.count >= 2 else { sendStatus(conn, 400, "Bad Request"); return }

        let method = String(parts[0]).uppercased()
        // The full request target (path + query) — the query must survive for
        // proxied API calls. `decoded` (path only) is for local-file resolution.
        let rawTarget = String(parts[1])
        var rawPath = rawTarget
        if let q = rawPath.firstIndex(where: { $0 == "?" || $0 == "#" }) {
            rawPath = String(rawPath[..<q])
        }
        let decoded = rawPath.removingPercentEncoding ?? rawPath

        // 1) Local snapshot / bundle file (GET/HEAD) — the same-origin page + its
        //    downloaded bundles. A missing local asset falls through to the proxy,
        //    which fetches the host's copy, so this is graceful.
        if method == "GET" || method == "HEAD", let fileURL = resolve(path: decoded) {
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isDir), !isDir.boolValue {
                let rangeHeader = headerValue(in: lines, name: "range")
                serveFile(conn, url: fileURL, method: method, rangeHeader: rangeHeader)
                return
            }
            // Derived views of a bundle that exist only at serve time — the same
            // two `main.py` generates (`offline_cache_bundle_file`). They are
            // resolved BEFORE the proxy fallthrough on purpose: the host has no
            // idea what `/StreamLinkBundles/<sha>/…` means and would 404 them.
            if let body = derivedPlaylist(for: fileURL) {
                sendText(conn, body, mime: HLSStaticServer.mime["m3u8"]!, method: method)
                return
            }
        }

        // 2) Everything else → reverse-proxy to the host (proxied playback session).
        if let host = proxyHost {
            let clen = Int(headerValue(in: lines, name: "content-length") ?? "") ?? 0
            if clen > initialBody.count {
                readBody(conn, have: initialBody, need: clen) { [weak self] body in
                    self?.proxy(conn, method: method, target: rawTarget, headerLines: lines, body: body, host: host)
                }
            } else {
                let body = clen > 0 ? Data(initialBody.prefix(clen)) : Data()
                proxy(conn, method: method, target: rawTarget, headerLines: lines, body: body, host: host)
            }
            return
        }

        // 3) No proxy configured (offline player mode) and not a local file.
        if method == "GET" || method == "HEAD" { sendStatus(conn, 404, "Not Found") }
        else { sendStatus(conn, 405, "Method Not Allowed") }
    }

    /// Read the rest of a request body up to `need` bytes (some may already be in
    /// `have`). Best-effort: an early close proxies whatever arrived.
    private func readBody(_ conn: NWConnection, have: Data, need: Int, completion: @escaping (Data) -> Void) {
        if have.count >= need { completion(Data(have.prefix(need))); return }
        conn.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, isComplete, error in
            guard let self = self else { conn.cancel(); return }
            var buf = have
            if let data = data { buf.append(data) }
            if buf.count >= need { completion(Data(buf.prefix(need))); return }
            if error != nil || isComplete { completion(buf); return }
            self.readBody(conn, have: buf, need: need, completion: completion)
        }
    }

    /// Reverse-proxy a request to the configured host, streaming the response back
    /// (JSON, SSE, or ranged media) with the device bearer token injected.
    private func proxy(_ conn: NWConnection, method: String, target: String,
                       headerLines: [String], body: Data, host: URL) {
        guard let url = URL(string: host.absoluteString + target) else {
            sendStatus(conn, 502, "Bad Gateway"); return
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        if !body.isEmpty { req.httpBody = body }
        // Forward client headers except hop-by-hop / managed ones. We override
        // Authorization (device token) and Accept-Encoding (identity → the host's
        // Content-Length stays accurate to relay).
        let drop: Set<String> = ["host", "connection", "keep-alive", "proxy-connection",
                                 "transfer-encoding", "te", "upgrade", "content-length",
                                 "accept-encoding", "authorization"]
        for line in headerLines.dropFirst() {
            guard let idx = line.firstIndex(of: ":") else { continue }
            let name = String(line[..<idx]).trimmingCharacters(in: .whitespaces)
            let value = String(line[line.index(after: idx)...]).trimmingCharacters(in: .whitespaces)
            if name.isEmpty || drop.contains(name.lowercased()) { continue }
            req.setValue(value, forHTTPHeaderField: name)
        }
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if let tok = proxyToken, !tok.isEmpty {
            req.setValue("Bearer \(tok)", forHTTPHeaderField: "Authorization")
        }

        let fwd = ProxyForwarder(conn: conn)
        // Stop pulling from the host the moment the client goes away (SSE / nav).
        conn.stateUpdateHandler = { state in
            switch state { case .failed, .cancelled: fwd.clientGone(); default: break }
        }
        fwd.start(req)
    }

    /// Map a request path to a file inside `root`, rejecting traversal.
    /// Player mode (`bundlesMount` set — the offline cached dashboard): paths
    /// under `/StreamLinkBundles/` resolve against the bundles storage dir
    /// instead, so the snapshot page and every downloaded bundle share ONE
    /// origin and the server never restarts mid-session. Same guard both ways.
    private func resolve(path: String) -> URL? {
        var rel = path
        while rel.hasPrefix("/") { rel.removeFirst() }
        if rel.isEmpty { rel = "" }    // a request for "/" maps to the root dir (will 404 as a dir)
        let bundlesPrefix = "StreamLinkBundles/"
        if let mount = bundlesMount, rel.hasPrefix(bundlesPrefix) {
            let sub = String(rel.dropFirst(bundlesPrefix.count))
            return contained(mount.appendingPathComponent(sub).standardizedFileURL, in: mount)
        }
        return contained(root.appendingPathComponent(rel).standardizedFileURL, in: root)
    }

    /// Containment check: candidate must be `base` itself or a descendant.
    private func contained(_ candidate: URL, in base: URL) -> URL? {
        let basePath = base.path.hasSuffix("/") ? base.path : base.path + "/"
        if candidate.path == base.path || candidate.path.hasPrefix(basePath) {
            return candidate
        }
        return nil
    }

    private func headerValue(in lines: [String], name: String) -> String? {
        let target = name.lowercased() + ":"
        for line in lines.dropFirst() {
            if line.lowercased().hasPrefix(target) {
                return String(line.dropFirst(target.count)).trimmingCharacters(in: .whitespaces)
            }
        }
        return nil
    }

    private func contentType(for url: URL) -> String {
        let ext = url.pathExtension.lowercased()
        return HLSStaticServer.mime[ext] ?? "application/octet-stream"
    }

    // MARK: - Derived playlists (native AVPlayer views of a downloaded bundle)

    // A downloaded bundle is a byte copy of the host's `.offline_cache/<sha>/`,
    // and the host NEVER writes these two names to disk — it generates them per
    // request (`_native_master` / `_sub_wrapper_playlist` in main.py, which this
    // mirrors) so `master.m3u8` stays byte-identical for the web player and the
    // cache version doesn't move. That is fine while the host is serving, but a
    // device copy is served by US, so the same two views have to be generated
    // here or a downloaded episode has no native master at all — which is
    // exactly what left it unable to hand off to the background player.

    private static let nativeMasterName = "master-native.m3u8"

    private func derivedPlaylist(for url: URL) -> String? {
        let name = url.lastPathComponent
        let dir = url.deletingLastPathComponent()
        if name == HLSStaticServer.nativeMasterName {
            let master = dir.appendingPathComponent("master.m3u8")
            guard let text = try? String(contentsOf: master, encoding: .utf8) else { return nil }
            return nativeMaster(text, meta: readMeta(dir))
        }
        if name.hasPrefix("sub_"), name.hasSuffix(".m3u8") {
            let n = String(name.dropFirst(4).dropLast(5))
            guard !n.isEmpty, n.allSatisfy({ $0.isNumber }) else { return nil }
            let vtt = dir.appendingPathComponent("sub_\(n).vtt")
            guard FileManager.default.fileExists(atPath: vtt.path) else { return nil }
            let dur = (readMeta(dir)["duration_sec"] as? NSNumber)?.doubleValue ?? 0
            return subWrapperPlaylist(vtt.lastPathComponent, duration: dur)
        }
        return nil
    }

    private func readMeta(_ dir: URL) -> [String: Any] {
        guard let data = try? Data(contentsOf: dir.appendingPathComponent("meta.json")),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return [:] }
        return obj
    }

    /// Escape a value for an HLS quoted-string attribute. A double quote can't be
    /// represented inside one, so it degrades to a single quote; a newline would
    /// break the line-oriented format outright.
    private func m3u8Attr(_ value: String) -> String {
        value.replacingOccurrences(of: "\"", with: "'")
             .replacingOccurrences(of: "\r", with: " ")
             .replacingOccurrences(of: "\n", with: " ")
    }

    /// `master.m3u8` with the bundle's sidecar subtitles declared as renditions.
    /// The subtitle NUMBER comes from each entry's `file` (`sub_<n>.vtt`), never
    /// from its position — a mismatch would serve the WRONG subtitle, since the
    /// route above maps `sub_<n>.m3u8` straight onto `sub_<n>.vtt`.
    private func nativeMaster(_ masterText: String, meta: [String: Any]) -> String {
        let subs = (meta["subtitles"] as? [[String: Any]]) ?? []
        if subs.isEmpty { return masterText }
        var media: [String] = []
        for (i, s) in subs.enumerated() {
            let file = (s["file"] as? String) ?? ""
            var n = String(i)
            if file.hasPrefix("sub_"), file.hasSuffix(".vtt") {
                let digits = String(file.dropFirst(4).dropLast(4))
                if !digits.isEmpty, digits.allSatisfy({ $0.isNumber }) { n = digits }
            }
            let rawLabel = (s["label"] as? String) ?? ""
            let rawLang  = (s["language"] as? String) ?? ""
            let name = m3u8Attr(rawLabel.isEmpty ? "Subtitles \(i + 1)" : rawLabel)
            let lang = m3u8Attr(rawLang.isEmpty ? "und" : rawLang)
            media.append("#EXT-X-MEDIA:TYPE=SUBTITLES,GROUP-ID=\"subs\","
                + "NAME=\"\(name)\",LANGUAGE=\"\(lang)\","
                + "DEFAULT=NO,AUTOSELECT=NO,FORCED=NO,URI=\"sub_\(n).m3u8\"")
        }
        var out: [String] = []
        for raw in masterText.components(separatedBy: "\n") {
            var line = raw.hasSuffix("\r") ? String(raw.dropLast()) : raw   // normalise CRLF
            if line.hasPrefix("#EXT-X-STREAM-INF:"), !line.contains("SUBTITLES=") {
                line += ",SUBTITLES=\"subs\""
            }
            out.append(line)
            if line.hasPrefix("#EXTM3U") { out.append(contentsOf: media) }
        }
        return out.joined(separator: "\n") + "\n"
    }

    /// A WebVTT rendition playlist wrapping one sidecar `.vtt` as a single
    /// whole-asset segment — the minimum legal playlist that turns a plain
    /// sidecar into something AVPlayer can select from a subtitle group.
    private func subWrapperPlaylist(_ vttName: String, duration: Double) -> String {
        let d = max(duration, 1.0)
        return "#EXTM3U\n"
            + "#EXT-X-VERSION:3\n"
            + "#EXT-X-TARGETDURATION:\(Int(d) + 1)\n"
            + "#EXT-X-MEDIA-SEQUENCE:0\n"
            + "#EXT-X-PLAYLIST-TYPE:VOD\n"
            + String(format: "#EXTINF:%.3f,\n", d)
            + "\(vttName)\n"
            + "#EXT-X-ENDLIST\n"
    }

    /// Send a small generated body (HEAD gets the headers only).
    private func sendText(_ conn: NWConnection, _ body: String, mime: String, method: String) {
        let data = Data(body.utf8)
        var headers = "HTTP/1.1 200 OK\r\n"
        headers += "Content-Type: \(mime)\r\n"
        headers += "Content-Length: \(data.count)\r\n"
        headers += "Accept-Ranges: bytes\r\n"
        headers += "Access-Control-Allow-Origin: *\r\n"
        headers += "Connection: close\r\n\r\n"
        var out = Data(headers.utf8)
        if method != "HEAD" { out.append(data) }
        send(conn, out, close: true)
    }

    // MARK: file serving (with Range)

    private func serveFile(_ conn: NWConnection, url: URL, method: String, rangeHeader: String?) {
        let attrs = try? FileManager.default.attributesOfItem(atPath: url.path)
        let total = (attrs?[.size] as? NSNumber)?.int64Value ?? 0
        let mime = contentType(for: url)

        // Resolve the byte range to send.
        var start: Int64 = 0
        var end: Int64 = max(total - 1, 0)
        var isPartial = false

        if let rh = rangeHeader, let parsed = parseRange(rh, total: total) {
            start = parsed.0
            end = parsed.1
            isPartial = true
        } else if let rh = rangeHeader, !rh.isEmpty, parseRange(rh, total: total) == nil {
            // Unsatisfiable range.
            var headers = "HTTP/1.1 416 Range Not Satisfiable\r\n"
            headers += "Content-Range: bytes */\(total)\r\n"
            headers += "Access-Control-Allow-Origin: *\r\n"
            headers += "Connection: close\r\n\r\n"
            send(conn, Data(headers.utf8), close: true)
            return
        }

        let length = (total == 0) ? 0 : (end - start + 1)
        let statusLine = isPartial ? "HTTP/1.1 206 Partial Content\r\n" : "HTTP/1.1 200 OK\r\n"

        var headers = statusLine
        headers += "Content-Type: \(mime)\r\n"
        headers += "Content-Length: \(length)\r\n"
        headers += "Accept-Ranges: bytes\r\n"
        if isPartial {
            headers += "Content-Range: bytes \(start)-\(end)/\(total)\r\n"
        }
        headers += "Access-Control-Allow-Origin: *\r\n"
        headers += "Cache-Control: no-store\r\n"
        headers += "Connection: close\r\n\r\n"

        let headerData = Data(headers.utf8)
        if method == "HEAD" || length == 0 {
            send(conn, headerData, close: true)
            return
        }

        // Stream the body in chunks so large segments never load fully into memory.
        guard let fh = try? FileHandle(forReadingFrom: url) else {
            sendStatus(conn, 500, "Internal Server Error"); return
        }
        try? fh.seek(toOffset: UInt64(start))
        send(conn, headerData, close: false) { [weak self] in
            self?.streamBody(conn, fileHandle: fh, remaining: length, chunk: 256 * 1024)
        }
    }

    private func streamBody(_ conn: NWConnection, fileHandle: FileHandle, remaining: Int64, chunk: Int) {
        if remaining <= 0 {
            try? fileHandle.close()
            conn.send(content: nil, contentContext: .finalMessage, isComplete: true,
                      completion: .contentProcessed { _ in conn.cancel() })
            return
        }
        let toRead = Int(min(Int64(chunk), remaining))
        let data = (try? fileHandle.read(upToCount: toRead)) ?? Data()
        if data.isEmpty {
            try? fileHandle.close()
            conn.cancel()
            return
        }
        conn.send(content: data, completion: .contentProcessed { [weak self] error in
            if error != nil {
                try? fileHandle.close()
                conn.cancel()
                return
            }
            self?.streamBody(conn, fileHandle: fileHandle, remaining: remaining - Int64(data.count), chunk: chunk)
        })
    }

    /// Parse a single-range header `bytes=start-end | start- | -suffix`.
    /// Returns nil for multi-range or unsatisfiable requests.
    private func parseRange(_ header: String, total: Int64) -> (Int64, Int64)? {
        guard total > 0 else { return nil }
        let h = header.trimmingCharacters(in: .whitespaces)
        guard h.lowercased().hasPrefix("bytes=") else { return nil }
        let spec = String(h.dropFirst("bytes=".count))
        if spec.contains(",") { return nil }   // multi-range unsupported
        let comps = spec.split(separator: "-", omittingEmptySubsequences: false)
        guard comps.count == 2 else { return nil }

        let startStr = comps[0].trimmingCharacters(in: .whitespaces)
        let endStr = comps[1].trimmingCharacters(in: .whitespaces)

        var start: Int64
        var end: Int64
        if startStr.isEmpty {
            // suffix range: last N bytes
            guard let suffix = Int64(endStr), suffix > 0 else { return nil }
            start = max(total - suffix, 0)
            end = total - 1
        } else {
            guard let s = Int64(startStr) else { return nil }
            start = s
            if endStr.isEmpty {
                end = total - 1
            } else {
                guard let e = Int64(endStr) else { return nil }
                end = min(e, total - 1)
            }
        }
        if start > end || start >= total || start < 0 { return nil }
        return (start, end)
    }

    // MARK: low-level send helpers

    private func sendStatus(_ conn: NWConnection, _ code: Int, _ text: String) {
        var headers = "HTTP/1.1 \(code) \(text)\r\n"
        headers += "Content-Length: 0\r\n"
        headers += "Access-Control-Allow-Origin: *\r\n"
        headers += "Connection: close\r\n\r\n"
        send(conn, Data(headers.utf8), close: true)
    }

    private func send(_ conn: NWConnection, _ data: Data, close: Bool, then: (() -> Void)? = nil) {
        conn.send(content: data, completion: .contentProcessed { _ in
            if close {
                conn.send(content: nil, contentContext: .finalMessage, isComplete: true,
                          completion: .contentProcessed { _ in conn.cancel() })
            } else {
                then?()
            }
        })
    }
}

// MARK: - AirPlay door (18.26.0 spike)

/// The one way an AirPlay receiver can reach what the phone is playing.
///
/// WHY IT EXISTS. An AirPlay receiver does not take frames from the phone: it is
/// handed the HLS URL and FETCHES IT ITSELF. Every URL the native player holds is
/// unreachable from an Apple TV — `http://127.0.0.1:<port>/…` is the Apple TV's
/// own loopback, and the box behind Tailscale is on no network the TV is on. So
/// the phone opens a second listener on its Wi-Fi interface and reverse-proxies
/// ONE upstream origin (the loopback server, or the box) under a secret prefix:
///
///     http://<wifi-ip>:<port>/ap/<token>/<upstream path + query>
///
/// Relative URIs inside the playlists resolve against the playlist's own URL, so
/// every rendition, segment and subtitle a master names stays under the prefix
/// with no playlist rewriting. Offline bundles (loopback upstream) and box
/// streams over Tailscale (box upstream) are the same code path.
///
/// Safety: GET/HEAD only, and nothing is answered without the 128-bit token,
/// which is minted per open and never leaves the AirPlay session. The loopback
/// server stays loopback-only; this is the only thing on the LAN, and only while
/// an AirPlay session is up.
final class AirPlayDoor {
    static let shared = AirPlayDoor()

    private let queue = DispatchQueue(label: "com.streamlink.airplaydoor", attributes: .concurrent)
    private var listener: NWListener?
    private(set) var port: UInt16?
    private(set) var host: String?
    private var token = ""
    private var upstream: URL?     // scheme://host:port, no path
    private var bearer: String?

    var isOpen: Bool { listener != nil && port != nil }

    enum DoorError: Error, LocalizedError {
        case noWifi, badUpstream, listener(Error)
        var errorDescription: String? {
            switch self {
            case .noWifi: return "No Wi-Fi address — AirPlay needs the phone on the same Wi-Fi as the TV."
            case .badUpstream: return "Nothing playable to share."
            case .listener(let e): return e.localizedDescription
            }
        }
    }

    /// Open (or re-point) the door at the origin of `media`. Completion on main.
    func open(for media: URL, bearer: String?, completion: @escaping (Result<Void, DoorError>) -> Void) {
        guard let origin = Self.origin(of: media) else {
            DispatchQueue.main.async { completion(.failure(.badUpstream)) }
            return
        }
        guard let ip = Self.wifiIPv4() else {
            DispatchQueue.main.async { completion(.failure(.noWifi)) }
            return
        }
        upstream = origin
        self.bearer = bearer
        host = ip
        if isOpen {
            DispatchQueue.main.async { completion(.success(())) }
            return
        }
        token = Self.mintToken()
        let params = NWParameters.tcp
        // Wi-Fi only: the receiver is on the Wi-Fi, and cellular must never
        // expose anything.
        params.requiredInterfaceType = .wifi
        params.allowLocalEndpointReuse = true
        let l: NWListener
        do { l = try NWListener(using: params) } catch {
            DispatchQueue.main.async { completion(.failure(.listener(error))) }
            return
        }
        listener = l
        var finished = false
        l.stateUpdateHandler = { [weak self] state in
            switch state {
            case .ready:
                self?.port = l.port?.rawValue
                if !finished { finished = true; DispatchQueue.main.async { completion(.success(())) } }
            case .failed(let err):
                l.cancel()
                if self?.listener === l { self?.listener = nil; self?.port = nil }
                DiagLog.shared.write("airplay-door-failed", ["err": err.localizedDescription], cat: "ext")
                if !finished { finished = true; DispatchQueue.main.async { completion(.failure(.listener(err))) } }
            default: break
            }
        }
        l.newConnectionHandler = { [weak self] conn in self?.handle(conn) }
        l.start(queue: queue)
    }

    func close() {
        guard listener != nil else { return }
        listener?.cancel()
        listener = nil
        port = nil
        upstream = nil
        bearer = nil
        token = ""
        DiagLog.shared.write("airplay-door-closed", [:], cat: "ext")
    }

    /// The receiver-reachable form of `url`, or nil when it is not under the
    /// origin the door is pointed at (the caller then keeps the original).
    func lanURL(for url: URL) -> URL? {
        guard isOpen, let port = port, let host = host, let up = upstream,
              Self.origin(of: url) == up else { return nil }
        var tail = url.path
        if let q = url.query { tail += "?" + q }
        return URL(string: "http://\(host):\(port)/ap/\(token)\(tail)")
    }

    // MARK: request handling

    private func handle(_ conn: NWConnection) {
        conn.start(queue: queue)
        receive(conn, buffer: Data())
    }

    private func receive(_ conn: NWConnection, buffer: Data) {
        conn.receive(minimumIncompleteLength: 1, maximumLength: 16 * 1024) { [weak self] data, _, done, err in
            guard let self = self else { conn.cancel(); return }
            var buf = buffer
            if let data = data { buf.append(data) }
            if let end = buf.range(of: Data("\r\n\r\n".utf8)) {
                self.respond(conn, header: buf.subdata(in: 0..<end.lowerBound))
                return
            }
            if err != nil || done || buf.count > 16 * 1024 { conn.cancel(); return }
            self.receive(conn, buffer: buf)
        }
    }

    private func respond(_ conn: NWConnection, header: Data) {
        let lines = (String(data: header, encoding: .utf8) ?? "").components(separatedBy: "\r\n")
        let parts = (lines.first ?? "").split(separator: " ")
        let prefix = "/ap/\(token)/"
        guard parts.count >= 2, !token.isEmpty, let up = upstream else { refuse(conn, 404); return }
        let method = String(parts[0]).uppercased()
        let target = String(parts[1])
        // A Cast receiver is a web page on another origin, so a request that
        // carries a non-simple header (Range, on some segments) is preflighted.
        // Answer it without the token: it grants nothing, it only says which
        // methods the real, token-checked request may use.
        if method == "OPTIONS" {
            let head = "HTTP/1.1 204 No Content\r\n"
                + "Access-Control-Allow-Origin: *\r\n"
                + "Access-Control-Allow-Methods: GET, HEAD, OPTIONS\r\n"
                + "Access-Control-Allow-Headers: *\r\n"
                + "Access-Control-Max-Age: 600\r\n"
                + "Content-Length: 0\r\nConnection: close\r\n\r\n"
            conn.send(content: Data(head.utf8), contentContext: .finalMessage, isComplete: true,
                      completion: .contentProcessed { _ in conn.cancel() })
            return
        }
        guard method == "GET" || method == "HEAD" else { refuse(conn, 405); return }
        guard target.hasPrefix(prefix),
              let url = URL(string: up.absoluteString + "/" + String(target.dropFirst(prefix.count))) else {
            refuse(conn, 404); return
        }
        var req = URLRequest(url: url)
        req.httpMethod = method
        // Range is the one header that matters (fmp4 byte ranges); User-Agent
        // and Accept ride along so the host's access log can tell who asked.
        for line in lines.dropFirst() {
            guard let i = line.firstIndex(of: ":") else { continue }
            let name = line[..<i].trimmingCharacters(in: .whitespaces)
            let value = line[line.index(after: i)...].trimmingCharacters(in: .whitespaces)
            if ["range", "user-agent", "accept", "if-range"].contains(name.lowercased()) {
                req.setValue(value, forHTTPHeaderField: name)
            }
        }
        req.setValue("identity", forHTTPHeaderField: "Accept-Encoding")
        if let b = bearer, !b.isEmpty { req.setValue("Bearer \(b)", forHTTPHeaderField: "Authorization") }
        let fwd = ProxyForwarder(conn: conn)
        conn.stateUpdateHandler = { state in
            switch state { case .failed, .cancelled: fwd.clientGone(); default: break }
        }
        fwd.start(req)
    }

    private func refuse(_ conn: NWConnection, _ code: Int) {
        let head = "HTTP/1.1 \(code) \(HTTPURLResponse.localizedString(forStatusCode: code))\r\n"
            + "Access-Control-Allow-Origin: *\r\n"
            + "Content-Length: 0\r\nConnection: close\r\n\r\n"
        conn.send(content: Data(head.utf8), contentContext: .finalMessage, isComplete: true,
                  completion: .contentProcessed { _ in conn.cancel() })
    }

    // MARK: helpers

    private static func origin(of url: URL) -> URL? {
        guard let scheme = url.scheme, let host = url.host else { return nil }
        let port = url.port.map { ":\($0)" } ?? ""
        return URL(string: "\(scheme)://\(host)\(port)")
    }

    private static func mintToken() -> String {
        var bytes = [UInt8](repeating: 0, count: 16)
        if SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) != errSecSuccess {
            return UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
        }
        return bytes.map { String(format: "%02x", $0) }.joined()
    }

    /// The phone's IPv4 on Wi-Fi (`en0`). IPv4 because every AirPlay receiver
    /// speaks it and a literal needs no brackets in a URL.
    static func wifiIPv4() -> String? {
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return nil }
        defer { freeifaddrs(head) }
        var found: String?
        for p in sequence(first: first, next: { $0.pointee.ifa_next }) {
            let ifa = p.pointee
            guard let sa = ifa.ifa_addr, sa.pointee.sa_family == UInt8(AF_INET),
                  String(cString: ifa.ifa_name) == "en0",
                  (ifa.ifa_flags & UInt32(IFF_UP)) != 0 else { continue }
            var buf = [CChar](repeating: 0, count: Int(NI_MAXHOST))
            if getnameinfo(sa, socklen_t(sa.pointee.sa_len), &buf, socklen_t(buf.count),
                           nil, 0, NI_NUMERICHOST) == 0 {
                found = String(cString: buf)
                break
            }
        }
        return found
    }
}

// MARK: - Reverse-proxy forwarder (proxied playback session, v8.7)

/// Streams one proxied request/response between a client `NWConnection` and the
/// host over `URLSession`. One instance (and one ephemeral session) per request;
/// self-retained by the session delegate until the transfer finishes. Handles
/// plain JSON, ranged media (206), and long-lived SSE identically — the only
/// difference is how long the body streams.
///
/// Back-pressure: `didReceive data` blocks the (serial) delegate queue on a
/// semaphore until the socket accepts the bytes, so a slow client throttles the
/// pull from the host instead of buffering unbounded — the same discipline
/// `HLSStaticServer.streamBody` uses for local files.
final class ProxyForwarder: NSObject, URLSessionDataDelegate {
    private let conn: NWConnection
    private var session: URLSession!
    private var task: URLSessionDataTask?
    private var wroteHead = false
    private var finished = false

    init(conn: NWConnection) {
        self.conn = conn
        super.init()
        let cfg = URLSessionConfiguration.ephemeral
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.timeoutIntervalForRequest = 120           // idle gap (SSE keepalives are well under this)
        cfg.timeoutIntervalForResource = 24 * 3600    // don't kill a long-lived SSE stream
        cfg.httpShouldSetCookies = false
        cfg.httpCookieAcceptPolicy = .never
        let q = OperationQueue()
        q.maxConcurrentOperationCount = 1             // serial → the semaphore back-pressure is safe
        session = URLSession(configuration: cfg, delegate: self, delegateQueue: q)
    }

    func start(_ req: URLRequest) {
        let t = session.dataTask(with: req)
        task = t
        t.resume()
    }

    /// The client disconnected (SSE close / page navigation) — stop the upstream pull.
    func clientGone() { task?.cancel() }

    private func finish(sendFinal: Bool) {
        if finished { return }
        finished = true
        conn.stateUpdateHandler = nil
        if sendFinal {
            conn.send(content: nil, contentContext: .finalMessage, isComplete: true,
                      completion: .contentProcessed { [conn] _ in conn.cancel() })
        } else {
            conn.cancel()
        }
        session.finishTasksAndInvalidate()
    }

    /// Synchronously push `data` to the client, blocking until the socket accepts it.
    /// Returns false if the send failed (client gone). Runs on the serial delegate queue.
    @discardableResult
    private func sendSync(_ data: Data) -> Bool {
        let sem = DispatchSemaphore(value: 0)
        var ok = true
        conn.send(content: data, completion: .contentProcessed { err in ok = (err == nil); sem.signal() })
        sem.wait()
        return ok
    }

    // Accept the host's (typically self-signed, LAN) TLS — matches the app's
    // NSAllowsArbitraryLoads posture for WKWebView; URLSession needs it explicitly.
    func urlSession(_ session: URLSession, didReceive challenge: URLAuthenticationChallenge,
                    completionHandler: @escaping (URLSession.AuthChallengeDisposition, URLCredential?) -> Void) {
        if challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
           let trust = challenge.protectionSpace.serverTrust {
            completionHandler(.useCredential, URLCredential(trust: trust))
        } else {
            completionHandler(.performDefaultHandling, nil)
        }
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive response: URLResponse,
                    completionHandler: @escaping (URLSession.ResponseDisposition) -> Void) {
        writeHead(response)
        completionHandler(.allow)
    }

    func urlSession(_ session: URLSession, dataTask: URLSessionDataTask, didReceive data: Data) {
        if !sendSync(data) { dataTask.cancel() }   // client gone → stop pulling from the host
    }

    func urlSession(_ session: URLSession, task: URLSessionTask, didCompleteWithError error: Error?) {
        if !wroteHead {
            _ = sendSync(Data(("HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\n"
                + "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n").utf8))
        }
        finish(sendFinal: wroteHead)
    }

    private func writeHead(_ response: URLResponse) {
        wroteHead = true
        guard let http = response as? HTTPURLResponse else {
            _ = sendSync(Data("HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n".utf8))
            return
        }
        var out = "HTTP/1.1 \(http.statusCode) \(HTTPURLResponse.localizedString(forStatusCode: http.statusCode))\r\n"
        var hasCORS = false
        for (k, v) in http.allHeaderFields {
            guard let key = k as? String, let val = v as? String else { continue }
            let lk = key.lowercased()
            // Drop hop-by-hop + anything that would contradict how we frame the body
            // (we always `Connection: close`; Accept-Encoding:identity means no C-E).
            if lk == "connection" || lk == "keep-alive" || lk == "transfer-encoding"
                || lk == "content-encoding" || lk == "proxy-connection" { continue }
            if lk == "access-control-allow-origin" { hasCORS = true }
            out += "\(key): \(val)\r\n"
        }
        if !hasCORS { out += "Access-Control-Allow-Origin: *\r\n" }
        out += "Connection: close\r\n\r\n"
        _ = sendSync(Data(out.utf8))
    }
}
