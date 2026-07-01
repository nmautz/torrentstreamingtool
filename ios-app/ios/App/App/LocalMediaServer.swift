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
//    start({ path? , bundledPath? }) -> { url, port, root }
//    stop()                          -> {}
//    info()                          -> { running, url?, port?, root? }
//
//  `path`        absolute filesystem dir (a downloaded bundle, M2+).
//  `bundledPath` dir relative to the app's bundled web assets ("public/<bundledPath>"),
//                used by the Gate 1b self-test to serve the shipped sample bundle.
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
        if let p = call.getString("path"), !p.isEmpty {
            root = URL(fileURLWithPath: p, isDirectory: true)
        } else if let bp = call.getString("bundledPath"), !bp.isEmpty {
            guard let base = Bundle.main.url(forResource: "public", withExtension: nil) else {
                call.reject("Bundled web assets not found.")
                return
            }
            root = base.appendingPathComponent(bp, isDirectory: true)
        } else {
            call.reject("start() requires `path` or `bundledPath`.")
            return
        }

        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: root.path, isDirectory: &isDir), isDir.boolValue else {
            call.reject("Directory does not exist: \(root.path)")
            return
        }

        // Single active server — tear down any previous one first.
        server?.stop()
        let srv = HLSStaticServer(root: root)
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
    ]

    init(root: URL) {
        self.root = root.standardizedFileURL
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
                self.respond(conn, headerData: headerData)
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

    private func respond(_ conn: NWConnection, headerData: Data) {
        guard let header = String(data: headerData, encoding: .utf8) else {
            sendStatus(conn, 400, "Bad Request"); return
        }
        let lines = header.components(separatedBy: "\r\n")
        guard let requestLine = lines.first else { sendStatus(conn, 400, "Bad Request"); return }
        let parts = requestLine.split(separator: " ")
        guard parts.count >= 2 else { sendStatus(conn, 400, "Bad Request"); return }

        let method = String(parts[0]).uppercased()
        guard method == "GET" || method == "HEAD" else {
            sendStatus(conn, 405, "Method Not Allowed"); return
        }

        // Strip query/fragment and percent-decode the path.
        var rawPath = String(parts[1])
        if let q = rawPath.firstIndex(where: { $0 == "?" || $0 == "#" }) {
            rawPath = String(rawPath[..<q])
        }
        let decoded = rawPath.removingPercentEncoding ?? rawPath

        guard let fileURL = resolve(path: decoded) else {
            sendStatus(conn, 403, "Forbidden"); return
        }

        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: fileURL.path, isDirectory: &isDir), !isDir.boolValue else {
            sendStatus(conn, 404, "Not Found"); return
        }

        // Parse a single Range header if present.
        let rangeHeader = headerValue(in: lines, name: "range")
        serveFile(conn, url: fileURL, method: method, rangeHeader: rangeHeader)
    }

    /// Map a request path to a file inside `root`, rejecting traversal.
    private func resolve(path: String) -> URL? {
        var rel = path
        while rel.hasPrefix("/") { rel.removeFirst() }
        if rel.isEmpty { rel = "" }    // a request for "/" maps to the root dir (will 404 as a dir)
        let candidate = root.appendingPathComponent(rel).standardizedFileURL
        // Containment check: candidate must be root itself or a descendant.
        let rootPath = root.path.hasSuffix("/") ? root.path : root.path + "/"
        if candidate.path == root.path || candidate.path.hasPrefix(rootPath) {
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
