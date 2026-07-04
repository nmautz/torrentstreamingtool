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

@objc(LocalMediaServer)
public class LocalMediaServer: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "LocalMediaServer"
    public let jsName = "LocalMediaServer"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "start", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "info",  returnType: CAPPluginReturnPromise),
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
                call.resolve(["url": url, "port": Int(port), "root": root.path])
            case .failure(let err):
                self?.server = nil
                call.reject("Failed to start localhost server: \(err.localizedDescription)")
            }
        }
    }

    @objc func stop(_ call: CAPPluginCall) {
        server?.stop()
        server = nil
        call.resolve()
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
        let params = NWParameters.tcp
        // Loopback-only: nothing on the LAN can reach the media server.
        params.requiredInterfaceType = .loopback
        params.allowLocalEndpointReuse = true

        let listener: NWListener
        do {
            listener = try NWListener(using: params)   // ephemeral port (OS-assigned)
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
                    finishOnce(.success(port))
                } else {
                    finishOnce(.failure(.noPort))
                }
            case .failed(let error):
                finishOnce(.failure(.listenerFailed(error)))
                self?.stop()
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
        listener?.cancel()
        listener = nil
        boundPort = nil
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
