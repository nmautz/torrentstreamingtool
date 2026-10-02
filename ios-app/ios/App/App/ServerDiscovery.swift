//
//  ServerDiscovery.swift
//  StreamLink iOS — find the servers this phone can reach (19.14.0).
//
//  The Connect screen used to be a text box for an IP address. It is now a list,
//  and this file fills it. There is no single mechanism that finds a server in
//  every place the app is used, so there are three, and they overlap on purpose:
//
//  1. BONJOUR (`_streamlink._tcp`). Instant on home Wi-Fi. The host puts its
//     address in the TXT record so a browse result needs no second resolve.
//
//  2. A SWEEP of the Wi-Fi subnet: one short HTTP GET per address. Bonjour is
//     multicast, and routers, mesh systems and an un-elevated Windows firewall
//     all drop it. The sweep also finds a host too old to advertise.
//
//  3. A SWEEP OF WHAT THE VPN ROUTES. Multicast does not cross Tailscale at all,
//     and nothing on iOS lets an app list a tailnet's machines. What the app CAN
//     read is the kernel routing table: a subnet router shows up there as an
//     ordinary route (`192.168.0/24` via `utunN`), and that is 254 addresses to
//     knock on. Other machines on the tailnet sit inside one `100.64/10` route,
//     four million addresses wide, which cannot be swept. Those are reached by
//     being TOLD: the host reports its own 100.x address in `/api/discovery`,
//     the shell remembers it, and passes it back in as a `known` URL.
//
//  A candidate is a server only if it SAYS so: `/api/discovery` answering
//  `app: "streamlink"`, or for a host older than 19.14.0, `/api/version`
//  answering with a version. Port 80 being open proves nothing, every router
//  has that.
//
//  `iOS has no <net/route.h>`. The routing dump is parsed by offset, against the
//  XNU layout, which is the same on macOS. That is also what makes `NetMap`
//  testable on the Mac with no device.
//

import Foundation
import Network
#if canImport(Capacitor)
import Capacitor
#endif

// MARK: - What networks is this phone on

struct NetIface {
    let name: String
    let addr: UInt32        // host byte order
    let mask: UInt32
}

struct NetRoute {
    let dest: UInt32
    let prefix: Int
    let iface: String
}

enum NetMap {
    static func parse(_ s: String) -> UInt32? {
        let parts = s.split(separator: ".", omittingEmptySubsequences: false)
        guard parts.count == 4 else { return nil }
        var v: UInt32 = 0
        for p in parts {
            guard let b = UInt8(p) else { return nil }
            v = v << 8 | UInt32(b)
        }
        return v
    }

    static func string(_ v: UInt32) -> String {
        "\(v >> 24).\((v >> 16) & 255).\((v >> 8) & 255).\(v & 255)"
    }

    /// Tailscale hands out addresses from carrier-grade NAT space, 100.64/10.
    static func isTailnet(_ a: UInt32) -> Bool { a & 0xFFC0_0000 == 0x6440_0000 }

    static func isPrivate(_ a: UInt32) -> Bool {
        a & 0xFF00_0000 == 0x0A00_0000          // 10/8
            || a & 0xFFF0_0000 == 0xAC10_0000   // 172.16/12
            || a & 0xFFFF_0000 == 0xC0A8_0000   // 192.168/16
    }

    static func isTunnel(_ name: String) -> Bool {
        name.hasPrefix("utun") || name.hasPrefix("ipsec") || name.hasPrefix("ppp")
    }

    /// Wi-Fi (and a wired adapter). Not cellular (`pdp_ip`), not AirDrop
    /// (`awdl`/`llw`), not the hotspot the phone itself is serving (`bridge`).
    static func isLAN(_ name: String) -> Bool { name.hasPrefix("en") }

    static func mask(prefix: Int) -> UInt32 {
        prefix <= 0 ? 0 : (prefix >= 32 ? 0xFFFF_FFFF : ~UInt32(0) << UInt32(32 - prefix))
    }

    static func interfaces() -> [NetIface] {
        var out: [NetIface] = []
        var head: UnsafeMutablePointer<ifaddrs>?
        guard getifaddrs(&head) == 0, let first = head else { return out }
        defer { freeifaddrs(head) }
        var cur: UnsafeMutablePointer<ifaddrs>? = first
        while let p = cur {
            defer { cur = p.pointee.ifa_next }
            let flags = Int32(p.pointee.ifa_flags)
            guard flags & IFF_UP != 0, flags & IFF_RUNNING != 0, flags & IFF_LOOPBACK == 0,
                  let sa = p.pointee.ifa_addr, sa.pointee.sa_family == sa_family_t(AF_INET)
            else { continue }
            let addr = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
            }
            var mask: UInt32 = 0xFFFF_FFFF
            if let nm = p.pointee.ifa_netmask {
                mask = nm.withMemoryRebound(to: sockaddr_in.self, capacity: 1) {
                    UInt32(bigEndian: $0.pointee.sin_addr.s_addr)
                }
            }
            out.append(NetIface(name: String(cString: p.pointee.ifa_name), addr: addr, mask: mask))
        }
        return out
    }

    /// The IPv4 routing table. Empty when the kernel won't hand it over, which
    /// callers must read as "unknown", never as "no routes".
    static func routes() -> [NetRoute] {
        var mib: [Int32] = [CTL_NET, PF_ROUTE, 0, AF_INET, 1 /* NET_RT_DUMP */, 0]
        var len = 0
        guard sysctl(&mib, UInt32(mib.count), nil, &len, nil, 0) == 0, len > 0 else { return [] }
        var buf = [UInt8](repeating: 0, count: len + 4096)   // the table can grow between calls
        len = buf.count
        guard sysctl(&mib, UInt32(mib.count), &buf, &len, nil, 0) == 0 else { return [] }
        return parseRoutes(buf, len)
    }

    // struct rt_msghdr (XNU, 92 bytes):
    //   0 u16 msglen · 2 u8 version · 3 u8 type · 4 u16 index · 8 i32 flags
    //   12 i32 addrs · … · 36 rt_metrics (56 bytes)
    // followed by one sockaddr per bit set in `addrs`, lowest bit first, each
    // padded to 4 bytes. A netmask sockaddr is TRUNCATED: its sa_len covers only
    // the bytes that are non-zero, and its family byte is not meaningful.
    static func parseRoutes(_ b: [UInt8], _ len: Int) -> [NetRoute] {
        let header = 92
        let RTF_UP: UInt32 = 0x1, RTF_HOST: UInt32 = 0x4
        let RTF_BROADCAST: UInt32 = 0x40_0000, RTF_MULTICAST: UInt32 = 0x80_0000
        func u16(_ o: Int) -> Int { Int(b[o]) | Int(b[o + 1]) << 8 }
        func u32(_ o: Int) -> UInt32 {
            UInt32(b[o]) | UInt32(b[o + 1]) << 8 | UInt32(b[o + 2]) << 16 | UInt32(b[o + 3]) << 24
        }
        var out: [NetRoute] = []
        var off = 0
        while off + header <= len {
            let msglen = u16(off)
            guard msglen >= header, off + msglen <= len else { break }
            defer { off += msglen }
            let flags = u32(off + 8), addrs = u32(off + 12)
            guard flags & RTF_UP != 0, flags & (RTF_BROADCAST | RTF_MULTICAST) == 0 else { continue }
            var p = off + header
            let end = off + msglen
            var dest: UInt32?
            var mask: UInt32?
            for bit in 0..<8 where addrs & (1 << UInt32(bit)) != 0 {
                guard p < end else { break }
                let saLen = Int(b[p])
                if bit == 0, saLen >= 8, p + 8 <= end, b[p + 1] == UInt8(AF_INET) {
                    dest = UInt32(b[p + 4]) << 24 | UInt32(b[p + 5]) << 16
                         | UInt32(b[p + 6]) << 8 | UInt32(b[p + 7])
                }
                if bit == 2 {
                    var m: UInt32 = 0
                    for i in 0..<4 {
                        let idx = p + 4 + i
                        m = m << 8 | ((4 + i < saLen && idx < end) ? UInt32(b[idx]) : 0)
                    }
                    mask = m
                }
                p += saLen == 0 ? 4 : (1 + ((saLen - 1) | 3))
            }
            guard let d = dest else { continue }
            let prefix: Int
            if flags & RTF_HOST != 0 { prefix = 32 }
            else if let m = mask { prefix = m.nonzeroBitCount }
            else { continue }
            var name = [CChar](repeating: 0, count: Int(IF_NAMESIZE) + 1)
            guard if_indextoname(UInt32(u16(off + 4)), &name) != nil else { continue }
            out.append(NetRoute(dest: d, prefix: prefix, iface: String(cString: name)))
        }
        return out
    }
}

// MARK: - Which addresses are worth knocking on

struct SweepPlan {
    /// Addresses in the order they should be tried.
    var hosts: [UInt32] = []
    /// One row per network swept, for the screen and the log.
    var nets: [[String: Any]] = []
    /// False when the phone holds no address at all (Airplane Mode).
    var online = false
    var routesRead = 0
    private(set) var ifaces: [NetIface] = []
    private var tunnelNets: [(net: UInt32, mask: UInt32, kind: String)] = []

    /// The largest network swept whole. A /22 is 1022 addresses; anything wider
    /// (a corporate 10/8, the tailnet's own /10) is not a place to go knocking.
    static let minPrefix = 22
    static let maxHosts = 1024

    /// - Parameter knownHosts: addresses of servers the app has used before.
    ///   Only consulted when a VPN is up and its routes could not be read.
    static func make(sweepLAN: Bool, knownHosts: [UInt32],
                     ifaces: [NetIface] = NetMap.interfaces(),
                     routes: [NetRoute] = NetMap.routes()) -> SweepPlan {
        var plan = SweepPlan()
        plan.ifaces = ifaces
        plan.online = !ifaces.isEmpty
        plan.routesRead = routes.count
        let own = Set(ifaces.map { $0.addr })
        var seen = Set<UInt32>()

        func add(_ net: UInt32, _ prefix: Int, _ kind: String, _ iface: String) {
            let mask = NetMap.mask(prefix: prefix)
            let base = net & mask
            let before = plan.hosts.count
            if prefix >= 31 {
                if !own.contains(base), seen.insert(base).inserted { plan.hosts.append(base) }
            } else {
                let size = ~mask
                for i in 1..<size where plan.hosts.count < maxHosts {
                    let h = base | i
                    if !own.contains(h), seen.insert(h).inserted { plan.hosts.append(h) }
                }
            }
            guard plan.hosts.count > before, prefix < 31 else { return }
            plan.nets.append(["net": "\(NetMap.string(base))/\(prefix)", "kind": kind,
                              "iface": iface, "hosts": plan.hosts.count - before])
        }

        // What a VPN routes. Before the Wi-Fi sweep: Bonjour already covers Wi-Fi.
        // `ipsec0 192.0.0.x` is the carrier's IPv4-over-IPv6 shim on cellular,
        // not a VPN anyone chose.
        let tunnels = ifaces.filter { NetMap.isTunnel($0.name) && $0.addr >> 8 != 0xC0_0000 }
        func kind(_ iface: String) -> String {
            tunnels.contains { $0.name == iface && NetMap.isTailnet($0.addr) } ? "tailscale" : "vpn"
        }
        var tunnelRoutes = 0
        // Host routes first: on a tunnel those are single machines the VPN
        // names outright, or ones this phone was just talking to.
        for r in routes.sorted(by: { $0.prefix > $1.prefix })
        where tunnels.contains(where: { $0.name == r.iface }) {
            guard r.prefix >= minPrefix,
                  r.dest != 0x6464_6464,                       // Tailscale's resolver
                  r.dest != 0xFFFF_FFFF,                       // broadcast
                  r.dest >> 28 != 0xE, r.dest >> 24 != 127,     // multicast, loopback
                  r.dest & 0xFFFF_0000 != 0xA9FE_0000          // link-local
            else { continue }
            tunnelRoutes += 1
            let k = kind(r.iface)
            plan.tunnelNets.append((r.dest & NetMap.mask(prefix: r.prefix), NetMap.mask(prefix: r.prefix), k))
            add(r.dest, r.prefix, k, r.iface)
        }
        // Routes unreadable (or the VPN publishes none we can sweep) but a tunnel
        // is up: the neighbourhood of a server we already know is the best guess
        // for where it went.
        if tunnelRoutes == 0, let t = tunnels.first {
            for h in knownHosts where NetMap.isPrivate(h) {
                let onLAN = ifaces.contains { NetMap.isLAN($0.name) && $0.addr & $0.mask == h & $0.mask }
                if !onLAN {
                    plan.tunnelNets.append((h & 0xFFFF_FF00, 0xFFFF_FF00, kind(t.name)))
                    add(h, 24, kind(t.name), t.name)
                }
            }
        }

        if sweepLAN {
            for i in ifaces where NetMap.isLAN(i.name) && NetMap.isPrivate(i.addr) {
                // A wide network is swept only around the phone's own address.
                add(i.addr, max(i.mask.nonzeroBitCount, 24), "wifi", i.name)
            }
        }
        return plan
    }

    /// How an address is reached, for the label under a server's name.
    func via(_ a: UInt32) -> String {
        if NetMap.isTailnet(a) { return "tailscale" }
        if ifaces.contains(where: { NetMap.isLAN($0.name) && $0.addr & $0.mask == a & $0.mask }) {
            return "wifi"
        }
        if let t = tunnelNets.first(where: { a & $0.mask == $0.net }) { return t.kind }
        return ""
    }
}

// MARK: - One scan

/// Probes candidates and browses Bonjour for one pass, reporting each server as
/// it answers. Single-use. Callbacks arrive on the main queue.
final class DiscoveryScan {
    struct Options {
        var known: [String] = []
        var sweep = true
        /// How long Bonjour is given even if every probe has already returned.
        var browseSeconds = 2.5
        /// Hard stop. Results found so far are returned.
        var deadline = 14.0
        /// Stop as soon as this server id answers.
        var wantId = ""
    }

    private struct Target {
        let url: String
        let timeout: TimeInterval
    }

    private static let maxActive = 96
    private let queue = DispatchQueue(label: "com.streamlink.discovery")
    private let session: URLSession
    private let opts: Options
    private var plan = SweepPlan()
    private var pending: [Target] = []
    private var queued = Set<String>()
    private var active = 0
    private var probed = 0
    private var browsing = false
    private var bonjourSeen = 0
    private var browser: NWBrowser?
    private var browserError = ""
    private var servers: [[String: Any]] = []
    private var done = false
    private let started = Date()
    private let onFound: ([String: Any]) -> Void
    private let onDone: ([String: Any]) -> Void

    init(_ opts: Options, onFound: @escaping ([String: Any]) -> Void,
         onDone: @escaping ([String: Any]) -> Void) {
        self.opts = opts
        self.onFound = onFound
        self.onDone = onDone
        let cfg = URLSessionConfiguration.ephemeral
        cfg.waitsForConnectivity = false
        cfg.requestCachePolicy = .reloadIgnoringLocalCacheData
        cfg.timeoutIntervalForResource = 6
        cfg.httpCookieStorage = nil
        cfg.urlCache = nil
        session = URLSession(configuration: cfg)
    }

    func start() {
        queue.async { [self] in
            let knownHosts = opts.known.compactMap { URL(string: $0)?.host }.compactMap(NetMap.parse)
            plan = SweepPlan.make(sweepLAN: opts.sweep, knownHosts: knownHosts)
            // Servers the app already knows go first and get longer to answer:
            // one of them is nearly always the one being looked for.
            for u in opts.known { enqueue(u, timeout: 3.0) }
            guard plan.online else { finish(); return }
            for h in plan.hosts { enqueue("http://" + NetMap.string(h), timeout: 2.0) }
            browse()
            queue.asyncAfter(deadline: .now() + opts.browseSeconds) { [self] in
                browsing = false
                pump()
            }
            queue.asyncAfter(deadline: .now() + opts.deadline) { [self] in finish() }
            pump()
        }
    }

    func cancel() { queue.async { [self] in finish() } }

    private func enqueue(_ url: String, timeout: TimeInterval, first: Bool = false) {
        guard !done, queued.insert(url).inserted else { return }
        let t = Target(url: url, timeout: timeout)
        // A Bonjour answer is almost certainly a server. It does not wait behind
        // a sweep whose slots are all held by addresses with nothing on them.
        if first { launch(t) } else { pending.append(t) }
    }

    private func launch(_ t: Target) {
        active += 1
        probed += 1
        Self.probe(t.url, timeout: t.timeout, session: session) { [self] _, server in
            queue.async { [self] in
                active -= 1
                if let s = server { found(s) }
                pump()
            }
        }
    }

    private func pump() {
        guard !done else { return }
        while active < Self.maxActive, !pending.isEmpty { launch(pending.removeFirst()) }
        if active == 0, pending.isEmpty, !browsing { finish() }
    }

    private func found(_ server: [String: Any]) {
        guard !done else { return }
        var s = server
        let url = s["url"] as? String ?? ""
        // A redirect (the host bounces a non-preferred adapter to its preferred
        // one) lands two candidates on one URL.
        guard !servers.contains(where: { $0["url"] as? String == url }) else { return }
        if let host = URL(string: url)?.host, let a = NetMap.parse(host) { s["via"] = plan.via(a) }
        else { s["via"] = "" }
        servers.append(s)
        DispatchQueue.main.async { [onFound] in onFound(s) }
        if !opts.wantId.isEmpty, s["id"] as? String == opts.wantId { finish() }
    }

    private func browse() {
        browsing = true
        let b = NWBrowser(for: .bonjourWithTXTRecord(type: "_streamlink._tcp", domain: nil), using: .tcp)
        browser = b
        b.stateUpdateHandler = { [weak self] state in
            // `.waiting` with a policy-denied error is what a refused Local
            // Network permission looks like; there is no API that says so outright.
            if case .failed(let e) = state { self?.browserError = "\(e)" }
            if case .waiting(let e) = state { self?.browserError = "\(e)" }
        }
        b.browseResultsChangedHandler = { [weak self] results, _ in
            guard let self = self else { return }
            for r in results {
                guard case .bonjour(let rec) = r.metadata else { continue }
                let txt = rec.dictionary
                guard let ip = txt["ip"], NetMap.parse(ip) != nil else { continue }
                let port = Int(txt["port"] ?? "") ?? 80
                self.bonjourSeen += 1
                self.enqueue("http://" + ip + (port == 80 ? "" : ":\(port)"), timeout: 3.0, first: true)
            }
            self.pump()
        }
        b.start(queue: queue)
    }

    private func finish() {
        guard !done else { return }
        done = true
        browser?.cancel()
        browser = nil
        session.invalidateAndCancel()
        let result: [String: Any] = [
            "servers": servers, "nets": plan.nets, "online": plan.online,
            "probed": probed, "routes": plan.routesRead, "bonjour": bonjourSeen,
            "bonjourError": browserError,
            "ms": Int(Date().timeIntervalSince(started) * 1000),
        ]
        DispatchQueue.main.async { [onDone] in onDone(result) }
    }

    // MARK: Probe

    /// Ask one address whether it is a StreamLink server.
    /// `reachable` is "something answered HTTP there", server or not.
    static func probe(_ base: String, timeout: TimeInterval, session: URLSession,
                      done: @escaping (_ reachable: Bool, _ server: [String: Any]?) -> Void) {
        get(base + "/api/discovery", timeout, session) { status, origin, json in
            guard status > 0 else { done(false, nil); return }
            if status == 200, let j = json, j["app"] as? String == "streamlink" {
                done(true, [
                    "url": origin, "id": j["id"] as? String ?? "",
                    "name": j["name"] as? String ?? "",
                    "version": j["version"] as? String ?? "",
                    "addrs": j["addrs"] as? [String] ?? [],
                ])
                return
            }
            guard status == 404 else { done(true, nil); return }
            // A host from before 19.14.0. `{"version": "19.13.1"}` and nothing
            // else is distinctive enough; it has no id, so it can be listed
            // but never followed to a new address.
            get(base + "/api/version", timeout, session) { status, origin, json in
                if status == 200, let j = json, j.count == 1, let v = j["version"] as? String,
                   v.range(of: #"^\d+\.\d+\.\d+"#, options: .regularExpression) != nil {
                    done(true, ["url": origin, "id": "", "name": "", "version": v, "addrs": [String]()])
                } else {
                    done(true, nil)
                }
            }
        }
    }

    private static func get(_ url: String, _ timeout: TimeInterval, _ session: URLSession,
                            _ done: @escaping (_ status: Int, _ origin: String, _ json: [String: Any]?) -> Void) {
        guard let u = URL(string: url) else { done(0, "", nil); return }
        var req = URLRequest(url: u, cachePolicy: .reloadIgnoringLocalCacheData, timeoutInterval: timeout)
        req.setValue("application/json", forHTTPHeaderField: "Accept")
        session.dataTask(with: req) { data, resp, _ in
            guard let http = resp as? HTTPURLResponse else { done(0, "", nil); return }
            // The origin that actually answered, after any redirect.
            var origin = url
            if let f = http.url, let scheme = f.scheme, let host = f.host {
                origin = "\(scheme)://\(host)" + (f.port.map { ":\($0)" } ?? "")
            }
            let json = data.flatMap { $0.count <= 65_536 ? $0 : nil }
                .flatMap { try? JSONSerialization.jsonObject(with: $0) } as? [String: Any]
            done(http.statusCode, origin, json)
        }.resume()
    }
}

// MARK: - Plugin

#if canImport(Capacitor)
@objc(ServerDiscovery)
public class ServerDiscovery: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "ServerDiscovery"
    public let jsName = "ServerDiscovery"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "scan",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "stop",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "probe", returnType: CAPPluginReturnPromise),
    ]

    private var current: DiscoveryScan?
    private var generation = 0

    /// One row per launch saying what the phone could see of its own networks.
    /// "The server didn't show up" is undiagnosable without it: an empty route
    /// table (iOS refused the read) and a VPN that is simply off both end in
    /// the same empty list.
    public override func load() {
        DispatchQueue.global(qos: .utility).async {
            let plan = SweepPlan.make(sweepLAN: true, knownHosts: [])
            DiagLog.shared.write("netmap", [
                "ifaces": plan.ifaces.map { "\($0.name) \(NetMap.string($0.addr))/\($0.mask.nonzeroBitCount)" }
                    .joined(separator: ", "),
                "routes": plan.routesRead,
                "nets": plan.nets.map { "\($0["kind"] ?? "") \($0["net"] ?? "")" }.joined(separator: ", "),
                "hosts": plan.hosts.count,
            ], cat: "app")
        }
    }

    /// scan({known: [url], sweep: bool, browseSeconds, deadline, wantId})
    ///   → {servers: [{url, id, name, version, addrs, via}], nets, online, …}
    /// Each server is also sent as a `found` event the moment it answers.
    @objc func scan(_ call: CAPPluginCall) {
        var o = DiscoveryScan.Options()
        o.known = (call.getArray("known") ?? []).compactMap { $0 as? String }
        o.sweep = call.getBool("sweep") ?? true
        o.browseSeconds = call.getDouble("browseSeconds") ?? o.browseSeconds
        o.deadline = call.getDouble("deadline") ?? o.deadline
        o.wantId = call.getString("wantId") ?? ""
        DispatchQueue.main.async {
            self.current?.cancel()
            self.generation += 1
            let gen = self.generation
            let scan = DiscoveryScan(o, onFound: { [weak self] s in
                guard let self = self, self.generation == gen else { return }
                self.notifyListeners("found", data: s)
            }, onDone: { [weak self] r in
                DiagLog.shared.write("discover", [
                    "found": (r["servers"] as? [Any])?.count ?? 0,
                    "probed": r["probed"] ?? 0, "routes": r["routes"] ?? 0,
                    "bonjour": r["bonjour"] ?? 0, "err": r["bonjourError"] ?? "",
                    "nets": ((r["nets"] as? [[String: Any]]) ?? [])
                        .map { "\($0["kind"] ?? "") \($0["net"] ?? "")" }.joined(separator: ", "),
                    "ms": r["ms"] ?? 0, "want": !o.wantId.isEmpty,
                ], cat: "app")
                if let self = self, self.generation == gen { self.current = nil }
                call.resolve(r)
            })
            self.current = scan
            scan.start()
        }
    }

    @objc func stop(_ call: CAPPluginCall) {
        DispatchQueue.main.async {
            self.current?.cancel()
            call.resolve()
        }
    }

    /// probe({url, timeout}) → {reachable, server?}. The launch check: is the
    /// saved server there, and which server is it.
    @objc func probe(_ call: CAPPluginCall) {
        guard let url = call.getString("url"), !url.isEmpty else { call.reject("url required"); return }
        let timeout = call.getDouble("timeout") ?? 2.2
        let cfg = URLSessionConfiguration.ephemeral
        cfg.waitsForConnectivity = false
        cfg.timeoutIntervalForResource = timeout * 2 + 1
        let session = URLSession(configuration: cfg)
        DiscoveryScan.probe(url, timeout: timeout, session: session) { reachable, server in
            session.finishTasksAndInvalidate()
            var out: [String: Any] = ["reachable": reachable]
            if let s = server { out["server"] = s }
            call.resolve(out)
        }
    }
}
#endif
