//
//  CastSession.swift
//  StreamLink iOS — Chromecast / Google TV casting (18.27.0 spike).
//
//  WHY NOT THE GOOGLE CAST SDK
//  The SDK is a large CocoaPods framework built around its own UI, session
//  manager and a registered receiver app. What we need from it is small: find
//  the devices, launch Google's stock Default Media Receiver, hand it a URL, and
//  drive play/pause/seek while reading the position back. That protocol (Cast
//  v2) is TLS on port 8009 carrying a six-field protobuf envelope whose payload
//  is JSON — the same thing pychromecast / node-castv2 speak. So it is spoken
//  here directly: no dependency, no Google developer registration, no pod.
//
//  Discovery is Bonjour (`_googlecast._tcp`), which needs only the
//  NSBonjourServices entry and the local-network prompt — unlike DLNA's SSDP,
//  which needs a multicast entitlement a sideloaded app cannot get.
//
//  The receiver FETCHES the stream itself, exactly like AirPlay, so the URLs
//  handed to it go through AirPlayDoor (LocalMediaServer.swift). The session is
//  a TRANSPORT only: NativePlaybackManager owns what is playing, progress,
//  auto-skip and advance, and drives this class the way it drives an AVPlayer.
//

import Foundation
import Network
import AVFoundation

// MARK: - Discovery

final class CastDiscovery {
    static let shared = CastDiscovery()

    struct Device {
        let id: String
        let name: String
        let model: String
        let endpoint: NWEndpoint
    }

    private let queue = DispatchQueue(label: "com.streamlink.castdiscovery")
    private var browser: NWBrowser?
    private(set) var devices: [String: Device] = [:]

    /// Browse for `seconds`, then report everything seen. Devices persist across
    /// scans so a pick made from a list that has since gone stale still resolves.
    func scan(seconds: Double, done: @escaping ([[String: Any]]) -> Void) {
        browser?.cancel()
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: "_googlecast._tcp", domain: nil),
                          using: .tcp)
        browser = b
        var failed = ""
        b.stateUpdateHandler = { state in
            if case .failed(let err) = state { failed = err.localizedDescription }
            if case .waiting(let err) = state { failed = err.localizedDescription }
        }
        b.browseResultsChangedHandler = { [weak self] results, _ in
            guard let self = self else { return }
            for r in results {
                guard case .service(let svcName, _, _, _) = r.endpoint else { continue }
                var txt: [String: String] = [:]
                if case .bonjour(let rec) = r.metadata { txt = rec.dictionary }
                let id = txt["id"] ?? svcName
                self.devices[id] = Device(id: id, name: txt["fn"] ?? svcName,
                                          model: txt["md"] ?? "", endpoint: r.endpoint)
            }
        }
        b.start(queue: queue)
        queue.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self = self else { return }
            b.cancel()
            if self.browser === b { self.browser = nil }
            let list = self.devices.values
                .sorted { $0.name.localizedCaseInsensitiveCompare($1.name) == .orderedAscending }
                .map { ["id": $0.id, "name": $0.name, "model": $0.model] as [String: Any] }
            DiagLog.shared.write("cast-scan", ["found": list.count, "err": failed], cat: "cast")
            DispatchQueue.main.async { done(list) }
        }
    }

    func device(_ id: String) -> Device? { queue.sync { devices[id] } }
}

// MARK: - Media status

struct CastMediaStatus {
    var mediaSessionId: Int?
    var playerState = ""        // IDLE | PLAYING | PAUSED | BUFFERING
    var idleReason = ""         // FINISHED | ERROR | CANCELLED | INTERRUPTED
    var currentTime: Double = 0
    var duration: Double = 0
    var tracks: [[String: Any]] = []
    var activeTrackIds: [Int] = []
}

// MARK: - Session

/// One connection to one Cast device, running the Default Media Receiver.
/// Every callback is delivered on the MAIN queue.
final class CastSession {
    static let defaultReceiver = "CC1AD845"

    private enum NS {
        static let connection = "urn:x-cast:com.google.cast.tp.connection"
        static let heartbeat  = "urn:x-cast:com.google.cast.tp.heartbeat"
        static let receiver   = "urn:x-cast:com.google.cast.receiver"
        static let media      = "urn:x-cast:com.google.cast.media"
    }

    struct Sub {
        var url: URL
        var name: String
        var lang: String
    }

    struct Load {
        var url: URL
        var position: Double
        var autoplay: Bool
        var title: String
        var subtitle: String
        /// Sidecar WebVTT tracks. The receiver does NOT expose the SUBTITLES
        /// renditions of an HLS master as text tracks (measured: `text:0` on the
        /// first device test), so subtitles ride in the LOAD as their own tracks,
        /// trackId = index + 1.
        var subs: [Sub] = []
        var activeSub = -1
        /// Prepped bundles are CMAF fMP4; on-demand (JIT) sessions are MPEG-TS,
        /// which is the receiver's default.
        var fmp4 = true
    }

    let device: CastDiscovery.Device
    /// Media status, at ~1 Hz while live.
    var onStatus: ((CastMediaStatus) -> Void)?
    /// The TLS link died. The receiver may well still be playing — see `rejoin`.
    var onDropped: ((String) -> Void)?
    /// The receiver refused or ended the session for good.
    var onFailed: ((String) -> Void)?
    /// The TV's volume (0...1) and mute, whenever the receiver reports them.
    var onVolume: ((Double, Bool) -> Void)?

    private let queue = DispatchQueue(label: "com.streamlink.castsession")
    private var conn: NWConnection?
    private var rx = Data()
    private var requestId = 1
    private var transportId: String?
    private var appSessionId: String?
    private(set) var mediaSessionId: Int?
    private var pendingLoad: Load?
    private var launched = false
    private var joining = false
    private var closed = false
    private var timer: DispatchSourceTimer?
    private var lastRx = Date()
    private var tick = 0

    init(device: CastDiscovery.Device) { self.device = device }

    // MARK: lifecycle

    /// Connect, launch the Default Media Receiver, load `load`.
    func start(_ load: Load) {
        queue.async {
            self.pendingLoad = load
            self.joining = false
            self.open()
        }
    }

    /// Reconnect to a receiver that kept playing while we were gone (the phone
    /// was suspended, Wi-Fi blinked). Loads nothing: it finds our app, joins its
    /// transport and resumes reading status.
    func rejoin() {
        queue.async {
            self.pendingLoad = nil
            self.joining = true
            self.open()
        }
    }

    /// Load a different URL into the running receiver (episode advance).
    func load(_ load: Load) {
        queue.async {
            guard self.transportId != nil else { self.pendingLoad = load; return }
            self.sendLoad(load)
        }
    }

    func play()  { mediaCommand("PLAY", [:]) }
    func pause() { mediaCommand("PAUSE", [:]) }
    func seek(_ t: Double) { mediaCommand("SEEK", ["currentTime": t, "resumeState": "PLAYBACK_UNCHANGED"]) }
    func setActiveTracks(_ ids: [Int]) { mediaCommand("EDIT_TRACKS_INFO", ["activeTrackIds": ids]) }

    /// Device volume, not stream volume: this is the TV's own level.
    func setVolume(_ level: Double) {
        queue.async { self.send(NS.receiver, to: "receiver-0",
                                ["type": "SET_VOLUME", "volume": ["level": min(max(level, 0), 1)]]) }
    }
    func setMuted(_ muted: Bool) {
        queue.async { self.send(NS.receiver, to: "receiver-0",
                                ["type": "SET_VOLUME", "volume": ["muted": muted]]) }
    }

    /// End the session. `stopApp` also closes the receiver on the TV, which is
    /// what "stop casting" means; a plain disconnect would leave it playing.
    func stop(stopApp: Bool) {
        queue.async {
            guard !self.closed else { return }
            self.closed = true
            if stopApp, let sid = self.appSessionId {
                self.send(NS.receiver, to: "receiver-0", ["type": "STOP", "sessionId": sid])
            }
            if let t = self.transportId { self.send(NS.connection, to: t, ["type": "CLOSE"]) }
            self.send(NS.connection, to: "receiver-0", ["type": "CLOSE"])
            // Give the CLOSE a moment on the wire before the socket goes.
            self.queue.asyncAfter(deadline: .now() + 0.3) { self.teardown() }
        }
    }

    private func open() {
        teardown()
        closed = false
        transportId = nil
        let tls = NWProtocolTLS.Options()
        // Cast devices present a self-signed certificate. The TLS is not our
        // security boundary here (the door token is); accept it.
        sec_protocol_options_set_verify_block(tls.securityProtocolOptions, { _, _, complete in
            complete(true)
        }, queue)
        let tcp = NWProtocolTCP.Options()
        tcp.enableKeepalive = true
        tcp.keepaliveIdle = 10
        let c = NWConnection(to: device.endpoint, using: NWParameters(tls: tls, tcp: tcp))
        conn = c
        c.stateUpdateHandler = { [weak self] state in
            guard let self = self, self.conn === c else { return }
            switch state {
            case .ready:
                DiagLog.shared.write("cast-connected", ["device": self.device.name,
                                                        "join": self.joining], cat: "cast")
                self.lastRx = Date()
                self.send(NS.connection, to: "receiver-0", ["type": "CONNECT"])
                self.send(NS.receiver, to: "receiver-0", ["type": "GET_STATUS"])
                self.startTimer()
                self.receive(c)
            case .failed(let err):
                self.dropped("tls-failed: \(err.localizedDescription)")
            case .waiting(let err):
                self.dropped("waiting: \(err.localizedDescription)")
            default: break
            }
        }
        c.start(queue: queue)
    }

    private func teardown() {
        timer?.cancel(); timer = nil
        conn?.stateUpdateHandler = nil
        conn?.cancel()
        conn = nil
        rx.removeAll()
    }

    private func dropped(_ why: String) {
        guard !closed else { return }
        teardown()
        DiagLog.shared.write("cast-dropped", ["why": why, "device": device.name], cat: "cast")
        DispatchQueue.main.async { self.onDropped?(why) }
    }

    private func failed(_ why: String) {
        guard !closed else { return }
        closed = true
        teardown()
        DiagLog.shared.write("cast-failed", ["why": why, "device": device.name], cat: "cast")
        DispatchQueue.main.async { self.onFailed?(why) }
    }

    /// 1 Hz: heartbeat every 5 s, media status every second while loaded, and a
    /// silence watchdog — a receiver that answers nothing for 20 s is gone even
    /// if the socket has not noticed.
    private func startTimer() {
        timer?.cancel()
        let t = DispatchSource.makeTimerSource(queue: queue)
        t.schedule(deadline: .now() + 1, repeating: 1)
        t.setEventHandler { [weak self] in
            guard let self = self else { return }
            self.tick += 1
            if self.tick % 5 == 0 { self.send(NS.heartbeat, to: "receiver-0", ["type": "PING"]) }
            if let tr = self.transportId, let ms = self.mediaSessionId {
                self.send(NS.media, to: tr, ["type": "GET_STATUS", "mediaSessionId": ms])
            }
            if Date().timeIntervalSince(self.lastRx) > 20 { self.dropped("silent") }
        }
        t.resume()
        timer = t
    }

    // MARK: receive

    private func receive(_ c: NWConnection) {
        c.receive(minimumIncompleteLength: 1, maximumLength: 64 * 1024) { [weak self] data, _, done, err in
            guard let self = self, self.conn === c else { return }
            if let data = data { self.rx.append(data); self.drain() }
            if let err = err { self.dropped("rx: \(err.localizedDescription)"); return }
            if done { self.dropped("eof"); return }
            self.receive(c)
        }
    }

    /// Frames are a 4-byte big-endian length, then a CastMessage protobuf.
    private func drain() {
        while rx.count >= 4 {
            let n = rx.prefix(4).reduce(0) { ($0 << 8) | Int($1) }
            guard rx.count >= 4 + n else { return }
            let frame = rx.subdata(in: rx.startIndex + 4 ..< rx.startIndex + 4 + n)
            rx.removeFirst(4 + n)
            lastRx = Date()
            if let m = CastProto.decode(frame) { handle(m) }
        }
    }

    private func handle(_ m: CastProto.Message) {
        guard let data = m.payload.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let type = obj["type"] as? String else { return }
        switch (m.namespace, type) {
        case (NS.heartbeat, "PING"):
            send(NS.heartbeat, to: m.source, ["type": "PONG"])
        case (NS.connection, "CLOSE"):
            // The receiver app closed our virtual connection: someone stopped it
            // from the TV, another sender took over, or the input was switched.
            if m.source == transportId { failed("receiver-closed") }
        case (NS.receiver, "RECEIVER_STATUS"):
            receiverStatus(obj)
        case (NS.receiver, "LAUNCH_ERROR"):
            failed("launch-error: \(obj["reason"] as? String ?? "?")")
        case (NS.media, "MEDIA_STATUS"):
            mediaStatus(obj)
        case (NS.media, "LOAD_FAILED"), (NS.media, "LOAD_CANCELLED"),
             (NS.media, "INVALID_REQUEST"), (NS.media, "ERROR"):
            let detail = (obj["detailedErrorCode"] as? Int).map { " \($0)" } ?? ""
            failed("\(type.lowercased())\(detail) \(obj["reason"] as? String ?? "")")
        default:
            break
        }
    }

    private func receiverStatus(_ obj: [String: Any]) {
        let status = obj["status"] as? [String: Any] ?? [:]
        if let vol = status["volume"] as? [String: Any],
           let level = (vol["level"] as? NSNumber)?.doubleValue {
            let muted = vol["muted"] as? Bool ?? false
            DispatchQueue.main.async { self.onVolume?(level, muted) }
        }
        let apps = status["applications"] as? [[String: Any]] ?? []
        let ours = apps.first { ($0["appId"] as? String) == Self.defaultReceiver }
        guard let app = ours, let tr = app["transportId"] as? String else {
            if transportId != nil {
                // We were joined to it, and it is gone: the TV stopped it.
                failed("app-stopped")
            } else if joining {
                failed("app-gone")
            } else if !launched {
                launched = true
                send(NS.receiver, to: "receiver-0", ["type": "LAUNCH", "appId": Self.defaultReceiver])
            }
            return
        }
        guard transportId != tr else { return }
        transportId = tr
        appSessionId = app["sessionId"] as? String
        send(NS.connection, to: tr, ["type": "CONNECT"])
        if let load = pendingLoad {
            pendingLoad = nil
            sendLoad(load)
        } else {
            send(NS.media, to: tr, ["type": "GET_STATUS"])
        }
    }

    private func mediaStatus(_ obj: [String: Any]) {
        guard let s = (obj["status"] as? [[String: Any]])?.first else { return }
        var st = CastMediaStatus()
        st.mediaSessionId = s["mediaSessionId"] as? Int
        st.playerState = s["playerState"] as? String ?? ""
        st.idleReason = s["idleReason"] as? String ?? ""
        st.currentTime = (s["currentTime"] as? NSNumber)?.doubleValue ?? 0
        if let media = s["media"] as? [String: Any] {
            st.duration = (media["duration"] as? NSNumber)?.doubleValue ?? 0
            st.tracks = media["tracks"] as? [[String: Any]] ?? []
        }
        st.activeTrackIds = s["activeTrackIds"] as? [Int] ?? []
        if let id = st.mediaSessionId { mediaSessionId = id }
        DispatchQueue.main.async { self.onStatus?(st) }
    }

    // MARK: send

    private func sendLoad(_ l: Load) {
        guard let tr = transportId else { pendingLoad = l; return }
        mediaSessionId = nil
        var media: [String: Any] = [
            "contentId": l.url.absoluteString,
            "contentUrl": l.url.absoluteString,
            "contentType": "application/x-mpegurl",
            "streamType": "BUFFERED",
            "metadata": ["metadataType": 0, "title": l.title, "subtitle": l.subtitle],
        ]
        if l.fmp4 {
            // Our bundles are fmp4 CMAF, and the receiver assumes MPEG-TS for
            // HLS unless told otherwise.
            media["hlsSegmentFormat"] = "fmp4"
            media["hlsVideoSegmentFormat"] = "fmp4"
        }
        if !l.subs.isEmpty {
            media["tracks"] = l.subs.enumerated().map { i, sub -> [String: Any] in
                ["trackId": i + 1, "type": "TEXT", "subtype": "SUBTITLES",
                 "trackContentId": sub.url.absoluteString, "trackContentType": "text/vtt",
                 "name": sub.name, "language": sub.lang]
            }
            // White on a translucent box: the receiver's default is small and
            // unboxed, which vanishes over bright scenes on a big TV.
            media["textTrackStyle"] = ["backgroundColor": "#00000099", "foregroundColor": "#FFFFFFFF",
                                       "fontScale": 1.1, "edgeType": "NONE"]
        }
        var body: [String: Any] = ["type": "LOAD", "media": media,
                                   "autoplay": l.autoplay, "currentTime": l.position]
        if l.activeSub >= 0, l.activeSub < l.subs.count { body["activeTrackIds"] = [l.activeSub + 1] }
        send(NS.media, to: tr, body)
        DiagLog.shared.write("cast-load", ["at": l.position, "autoplay": l.autoplay,
                                           "title": l.title, "subs": l.subs.count,
                                           "activeSub": l.activeSub, "fmp4": l.fmp4,
                                           "host": l.url.host ?? ""], cat: "cast")
    }

    private func mediaCommand(_ type: String, _ extra: [String: Any]) {
        queue.async {
            guard let tr = self.transportId, let ms = self.mediaSessionId else { return }
            var body = extra
            body["type"] = type
            body["mediaSessionId"] = ms
            self.send(NS.media, to: tr, body)
        }
    }

    /// Queue-confined. Adds `requestId` to everything but heartbeats/connection.
    private func send(_ ns: String, to dest: String, _ body: [String: Any]) {
        guard let c = conn else { return }
        var b = body
        if ns != NS.heartbeat && ns != NS.connection {
            b["requestId"] = requestId
            requestId += 1
        }
        guard let json = try? JSONSerialization.data(withJSONObject: b),
              let payload = String(data: json, encoding: .utf8) else { return }
        let frame = CastProto.encode(source: "sender-0", destination: dest,
                                     namespace: ns, payload: payload)
        var out = Data()
        var n = UInt32(frame.count).bigEndian
        withUnsafeBytes(of: &n) { out.append(contentsOf: $0) }
        out.append(frame)
        c.send(content: out, completion: .contentProcessed { _ in })
    }
}

// MARK: - CastMessage protobuf (hand-rolled; six fields)

enum CastProto {
    struct Message {
        var source = ""
        var destination = ""
        var namespace = ""
        var payload = ""
    }

    //  message CastMessage {
    //    required ProtocolVersion protocol_version = 1;  // CASTV2_1_0 = 0
    //    required string source_id = 2;
    //    required string destination_id = 3;
    //    required string namespace = 4;
    //    required PayloadType payload_type = 5;          // STRING = 0
    //    optional string payload_utf8 = 6;
    //    optional bytes payload_binary = 7;
    //  }
    static func encode(source: String, destination: String, namespace: String, payload: String) -> Data {
        var d = Data()
        func varint(_ v: UInt64) {
            var v = v
            repeat {
                var byte = UInt8(v & 0x7f)
                v >>= 7
                if v != 0 { byte |= 0x80 }
                d.append(byte)
            } while v != 0
        }
        func str(_ field: UInt64, _ s: String) {
            let bytes = Array(s.utf8)
            varint(field << 3 | 2)
            varint(UInt64(bytes.count))
            d.append(contentsOf: bytes)
        }
        varint(1 << 3 | 0); varint(0)
        str(2, source)
        str(3, destination)
        str(4, namespace)
        varint(5 << 3 | 0); varint(0)
        str(6, payload)
        return d
    }

    static func decode(_ data: Data) -> Message? {
        let b = [UInt8](data)
        var i = 0
        func varint() -> UInt64? {
            var v: UInt64 = 0, shift: UInt64 = 0
            while i < b.count {
                let byte = b[i]; i += 1
                v |= UInt64(byte & 0x7f) << shift
                if byte & 0x80 == 0 { return v }
                shift += 7
                if shift > 63 { return nil }
            }
            return nil
        }
        var m = Message()
        while i < b.count {
            guard let key = varint() else { return nil }
            let field = key >> 3, wire = key & 7
            switch wire {
            case 0:
                guard varint() != nil else { return nil }
            case 2:
                guard let n = varint(), i + Int(n) <= b.count else { return nil }
                let s = String(decoding: b[i ..< i + Int(n)], as: UTF8.self)
                i += Int(n)
                switch field {
                case 2: m.source = s
                case 3: m.destination = s
                case 4: m.namespace = s
                case 6: m.payload = s
                default: break
                }
            default:
                return nil
            }
        }
        return m
    }
}

// MARK: - Staying alive while the TV plays

/// The receiver fetches every segment THROUGH the phone (AirPlayDoor), so a
/// suspended phone is a stalled TV a buffer's length later. With AirPlay the
/// AVPlayer's own playback keeps the process up under the `audio` background
/// mode; a Cast session plays nothing locally, so it plays silence instead.
/// Mixed with others so it never interrupts the viewer's own audio.
final class SilentKeepAlive {
    static let shared = SilentKeepAlive()
    private var engine: AVAudioEngine?

    var isRunning: Bool { engine?.isRunning ?? false }

    func start() {
        guard engine == nil else { return }
        let s = AVAudioSession.sharedInstance()
        try? s.setCategory(.playback, mode: .default, options: [.mixWithOthers])
        try? s.setActive(true)
        let e = AVAudioEngine()
        let node = AVAudioPlayerNode()
        let fmt = AVAudioFormat(standardFormatWithSampleRate: 44100, channels: 1)!
        guard let buf = AVAudioPCMBuffer(pcmFormat: fmt, frameCapacity: 44100) else { return }
        buf.frameLength = buf.frameCapacity      // zero-filled: one second of silence
        e.attach(node)
        e.connect(node, to: e.mainMixerNode, format: fmt)
        do { try e.start() } catch {
            DiagLog.shared.write("cast-keepalive-failed", ["err": error.localizedDescription], cat: "cast")
            return
        }
        node.scheduleBuffer(buf, at: nil, options: .loops)
        node.play()
        engine = e
        DiagLog.shared.write("cast-keepalive", ["on": true], cat: "cast")
    }

    func stop() {
        guard let e = engine else { return }
        e.stop()
        engine = nil
        DiagLog.shared.write("cast-keepalive", ["on": false], cat: "cast")
    }
}
