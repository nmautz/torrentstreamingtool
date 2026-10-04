//
//  OfflineStore.swift
//  StreamLink iOS — M3 (offline progress + auto-sync)
//
//  A small, durable, native key/value log of watch progress captured while the
//  device is offline. The offline player (www/downloads.html) writes progress
//  here during playback (the host dashboard can't load with no network, so this
//  is the only place offline history can live), and so does NativePlayback while
//  it holds an offline episode (18.21.7) — with the phone locked the page's timers
//  are frozen, so native is the only writer left; the dashboard's B5 sync glue
//  (static/index.html), which DOES run on the host once reconnected, drains it to
//  POST /api/sync/progress and records each file's new `base_synced_at`
//  watermark via markSynced().
//
//  Why native (not localStorage): downloads.html (file:// bundle) and the host
//  dashboard (http://host) are different origins and do NOT share localStorage.
//  A native plugin is the only store both can read/write, and it survives an app
//  kill. Stored in Application Support (non-evictable), excluded from backup.
//
//  Records are keyed by (profileId, itemId, filePath). The active profile is set
//  by the dashboard when online (setProfile) so the offline player knows whose
//  history it's recording.
//
//  JS surface (Capacitor plugin "OfflineStore"):
//    setProfile({ profileId, profileName? })                 -> {}
//    getProfile()                                            -> { profileId, profileName }
//    saveProgress({ itemId, filePath, positionSec, durationSec,
//                   profileId?, subtitleSel?, audioSel?, localAudioIdx?,
//                   localSubtitleIdx? })                      -> {}
//    getProgress({ itemId, filePath, profileId? })           -> { found, positionSec,
//                                                                  durationSec, completed,
//                                                                  clientUpdatedAt, baseSyncedAt }
//    seedProgress({ itemId, filePath, positionSec, durationSec,
//                   completed, serverUpdatedAt, playedSec?, profileId?, force? }) -> {}  // server→device baseline
//                                                                  // force: M4 "server wins" overwrite of a dirty record
//                                                                  // playedSec: server's play count to build on
//    pending()                                               -> { events:[ <event> ] }
//    markSynced({ applied:[ { itemId, filePath, serverUpdatedAt, profileId? } ] }) -> {}
//    all()                                                   -> { events:[ <event> ] }
//    clear()                                                 -> {}
//    kvGet({ key })                                          -> { value }   // null when unset
//    kvSet({ key, value })                                   -> {}          // value null = delete
//
//  <event> = { profileId, itemId, filePath, positionSec, durationSec, completed,
//              clientUpdatedAt, baseSyncedAt, serverId?, playedSec?, subtitleSel?,
//              audioSel?, localAudioIdx?, localSubtitleIdx? }
//
//  serverId (20.0.0): which StreamLink server the file belongs to. A phone can
//  hold downloads from more than one, and a play made offline must go back to
//  the server it came from and to no other. saveProgress and seedProgress take
//  it optionally and never drop one already on the record, so the native writer
//  (NativePlayback, which does not know it) keeps what the page set. A record
//  with none is attributed by the page at sync time.
//
//  kvGet / kvSet (20.0.0): a small JSON store for what the app must know on
//  every origin it runs on - the connect shell, the host's dashboard, the
//  loopback offline player. It holds the phone's servers and the account each
//  one is pinned to (key "device"); the page owns the shape. Separate file
//  (device.json), so clear() never touches it.
//
//  playedSec (17.5.0): seconds of this file genuinely PLAYED, as opposed to where
//  the playhead is. An offline session reaches the host as ONE coalesced position,
//  so the host can't tell watching up to the end from scrubbing there. The store
//  measures it instead, on every saveProgress, by the host's own rule
//  (watchrule.py): add the position advance since the last save, capped by the
//  wall clock between them. Playback earns real time; a seek earns about a second.
//  Absent on records written by an older build, which then sync position-only.
//

import Foundation
import Capacitor

@objc(OfflineStore)
public class OfflineStore: CAPPlugin, CAPBridgedPlugin {
    public let identifier = "OfflineStore"
    public let jsName = "OfflineStore"
    public let pluginMethods: [CAPPluginMethod] = [
        CAPPluginMethod(name: "setProfile",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getProfile",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "saveProgress", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "getProgress",  returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "seedProgress", returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "pending",      returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "markSynced",   returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "all",          returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "clear",        returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "kvGet",        returnType: CAPPluginReturnPromise),
        CAPPluginMethod(name: "kvSet",        returnType: CAPPluginReturnPromise),
    ]

    private let store = OfflineProgressStore.shared

    // MARK: - JS methods

    @objc func setProfile(_ call: CAPPluginCall) {
        guard let pid = call.getString("profileId"), !pid.isEmpty else {
            call.reject("setProfile() requires profileId."); return
        }
        store.setProfile(id: pid, name: call.getString("profileName") ?? "")
        call.resolve()
    }

    @objc func getProfile(_ call: CAPPluginCall) {
        let (id, name) = store.getProfile()
        call.resolve(["profileId": id, "profileName": name])
    }

    @objc func saveProgress(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"), !itemId.isEmpty,
              let filePath = call.getString("filePath"), !filePath.isEmpty else {
            call.reject("saveProgress() requires itemId, filePath."); return
        }
        let pos = call.getDouble("positionSec") ?? 0
        let dur = call.getDouble("durationSec") ?? 0
        let pid = call.getString("profileId")
        // JSObject ([String: JSValue]) → plain [String: Any] for the Foundation-only store.
        let sel = call.getObject("subtitleSel")?.reduce(into: [String: Any]()) { $0[$1.key] = $1.value }
        let asel = call.getObject("audioSel")?.reduce(into: [String: Any]()) { $0[$1.key] = $1.value }
        store.saveProgress(
            profileId: pid, itemId: itemId, filePath: filePath,
            positionSec: pos, durationSec: dur,
            subtitleSel: sel, audioSel: asel,
            localAudioIdx: call.getInt("localAudioIdx"),
            localSubtitleIdx: call.getInt("localSubtitleIdx"),
            serverId: call.getString("serverId"))
        call.resolve()
    }

    @objc func getProgress(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"),
              let filePath = call.getString("filePath") else {
            call.reject("getProgress() requires itemId, filePath."); return
        }
        call.resolve(store.getProgress(profileId: call.getString("profileId"),
                                       itemId: itemId, filePath: filePath))
    }

    @objc func seedProgress(_ call: CAPPluginCall) {
        guard let itemId = call.getString("itemId"), !itemId.isEmpty,
              let filePath = call.getString("filePath"), !filePath.isEmpty else {
            call.reject("seedProgress() requires itemId, filePath."); return
        }
        store.seedProgress(
            profileId: call.getString("profileId"),
            itemId: itemId, filePath: filePath,
            positionSec: call.getDouble("positionSec") ?? 0,
            durationSec: call.getDouble("durationSec") ?? 0,
            completed: call.getBool("completed") ?? false,
            serverUpdatedAt: call.getString("serverUpdatedAt") ?? "",
            playedSec: call.getDouble("playedSec"),
            // M4: a "server wins" conflict resolution must overwrite the device's
            // own unsynced (dirty) record; the normal seed must not.
            force: call.getBool("force") ?? false,
            serverId: call.getString("serverId"))
        call.resolve()
    }

    @objc func pending(_ call: CAPPluginCall) {
        call.resolve(["events": store.pending()])
    }

    @objc func markSynced(_ call: CAPPluginCall) {
        let raw = call.getArray("applied", JSObject.self) ?? []
        let applied = raw.map { obj in obj.reduce(into: [String: Any]()) { $0[$1.key] = $1.value } }
        store.markSynced(applied)
        call.resolve()
    }

    @objc func all(_ call: CAPPluginCall) {
        call.resolve(["events": store.all()])
    }

    @objc func clear(_ call: CAPPluginCall) {
        store.clear()
        call.resolve()
    }

    @objc func kvGet(_ call: CAPPluginCall) {
        guard let key = call.getString("key"), !key.isEmpty else {
            call.reject("kvGet() requires key."); return
        }
        call.resolve(["value": store.kvGet(key) ?? NSNull()])
    }

    @objc func kvSet(_ call: CAPPluginCall) {
        guard let key = call.getString("key"), !key.isEmpty else {
            call.reject("kvSet() requires key."); return
        }
        // Whatever JSON the page sent: an object, an array, a string, or null.
        let raw = call.options["value"]
        store.kvSet(key, (raw == nil || raw is NSNull) ? nil : raw)
        call.resolve()
    }
}

// MARK: - Store (process-wide, file-backed, serialized)

final class OfflineProgressStore {
    static let shared = OfflineProgressStore()

    private let fm = FileManager.default
    // All mutations serialized; reads use .sync so callers see a consistent file.
    private let queue = DispatchQueue(label: "com.streamlink.offlinestore.state")

    private var root: URL {
        let base = fm.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let dir = base.appendingPathComponent("StreamLinkOffline", isDirectory: true)
        if !fm.fileExists(atPath: dir.path) {
            try? fm.createDirectory(at: dir, withIntermediateDirectories: true)
            var u = dir
            var v = URLResourceValues(); v.isExcludedFromBackup = true
            try? u.setResourceValues(v)
        }
        return dir
    }
    private var fileURL: URL { root.appendingPathComponent("progress.json") }
    private var kvURL: URL { root.appendingPathComponent("device.json") }

    // MARK: key/value (the phone's servers and pinned accounts)

    private func readKV() -> [String: Any] {
        guard let data = try? Data(contentsOf: kvURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }

    func kvGet(_ key: String) -> Any? {
        queue.sync { readKV()[key] }
    }

    func kvSet(_ key: String, _ value: Any?) {
        queue.sync {
            var obj = readKV()
            // A value JSONSerialization cannot write would throw an ObjC
            // exception, which Swift cannot catch. Refuse it instead.
            if let v = value {
                guard JSONSerialization.isValidJSONObject(["v": v]) else { return }
                obj[key] = v
            } else {
                obj[key] = nil
            }
            if let data = try? JSONSerialization.data(withJSONObject: obj, options: []) {
                try? data.write(to: kvURL, options: .atomic)
            }
        }
    }

    // On-disk shape: { "profile": {id,name}, "records": { "<key>": <record> } }
    private func read() -> [String: Any] {
        guard let data = try? Data(contentsOf: fileURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ["profile": ["id": "", "name": ""], "records": [String: Any]()]
        }
        return obj
    }
    private func write(_ obj: [String: Any]) {
        if let data = try? JSONSerialization.data(withJSONObject: obj, options: []) {
            try? data.write(to: fileURL, options: .atomic)
        }
    }

    private func key(_ profileId: String, _ itemId: String, _ filePath: String) -> String {
        return profileId + "\u{0000}" + itemId + "\u{0000}" + filePath
    }

    // MARK: profile

    func setProfile(id: String, name: String) {
        queue.sync {
            var obj = read()
            obj["profile"] = ["id": id, "name": name]
            write(obj)
        }
    }

    func getProfile() -> (String, String) {
        queue.sync {
            let p = (read()["profile"] as? [String: Any]) ?? [:]
            return ((p["id"] as? String) ?? "", (p["name"] as? String) ?? "")
        }
    }

    private func activeProfileLocked(_ obj: [String: Any]) -> String {
        return ((obj["profile"] as? [String: Any])?["id"] as? String) ?? ""
    }

    // MARK: progress

    func saveProgress(profileId: String?, itemId: String, filePath: String,
                      positionSec: Double, durationSec: Double,
                      subtitleSel: [String: Any]?, audioSel: [String: Any]?,
                      localAudioIdx: Int?, localSubtitleIdx: Int?,
                      serverId: String? = nil) {
        queue.sync {
            var obj = read()
            let pid = (profileId?.isEmpty == false ? profileId! : activeProfileLocked(obj))
            var records = (obj["records"] as? [String: Any]) ?? [:]
            let k = key(pid, itemId, filePath)
            var rec = (records[k] as? [String: Any]) ?? [:]

            // Play accrual: the host's rule (watchrule.played_after), on the
            // device's clock. A brand-new record starts at 0; one written before
            // playedSec existed is seeded from its position, as the host does.
            let hadRecord = !rec.isEmpty
            let prevPos = Self.num(rec["positionSec"])
            var played = rec["playedSec"].map { Self.num($0) } ?? (hadRecord ? prevPos : 0)
            let step = positionSec - prevPos
            if hadRecord, step > 0,
               let prevAt = (rec["clientUpdatedAt"] as? String).flatMap({ Self.parse($0) }) {
                let elapsed = Date().timeIntervalSince(prevAt)
                if elapsed > 0 {
                    played += min(step, elapsed * Self.playedRateCap, Self.playedStepCapSec)
                }
            }
            played = (played * 10).rounded() / 10

            // Same verdict the host reaches for the no-credits case: the tail
            // reached AND most of it actually played. (Was `pct > 0.92`, which a
            // scrub to the end satisfied.)
            let nowCompleted = durationSec > 0
                && positionSec >= durationSec * Self.finishTailPct
                && played >= durationSec * Self.minPlayedPct
            // `completed` is monotonic on-device too (mirrors the server merge).
            let prevCompleted = (rec["completed"] as? Bool) ?? false

            rec["profileId"] = pid
            rec["itemId"] = itemId
            rec["filePath"] = filePath
            rec["positionSec"] = (positionSec * 10).rounded() / 10
            rec["durationSec"] = (durationSec * 10).rounded() / 10
            rec["completed"] = nowCompleted || prevCompleted
            rec["playedSec"] = played
            rec["clientUpdatedAt"] = Self.isoNow()
            // `dirty` (NOT a timestamp comparison) is the source of truth for
            // "needs pushing": a local watch always sets it, markSynced clears it.
            // Comparing the device clock (clientUpdatedAt) against the server clock
            // (baseSyncedAt) was unreliable — any skew made a fresh offline watch
            // look already-synced, so it was never pushed.
            rec["dirty"] = true
            if rec["baseSyncedAt"] == nil { rec["baseSyncedAt"] = NSNull() }
            if let sid = serverId, !sid.isEmpty { rec["serverId"] = sid }
            if let s = subtitleSel { rec["subtitleSel"] = s }
            if let a = audioSel { rec["audioSel"] = a }
            if let a = localAudioIdx { rec["localAudioIdx"] = a }
            if let s = localSubtitleIdx { rec["localSubtitleIdx"] = s }

            records[k] = rec
            obj["records"] = records
            write(obj)

            // Offline play is the one case the SERVER cannot measure — it sees a
            // single final position long after the fact, so if the completion
            // verdict here is wrong nothing downstream can catch it. Log the
            // inputs, not just the answer: `played` versus the two thresholds is
            // what separates "watched it" from "scrubbed to the end", and it was
            // previously visible nowhere at all. Only when the verdict FLIPS, so
            // a two-hour offline session costs a handful of rows.
            if (nowCompleted || prevCompleted) != prevCompleted {
                DiagLog.shared.write("offline-completed", [
                    "item": itemId, "pos": positionSec, "dur": durationSec,
                    "played": played,
                    "needTail": durationSec * Self.finishTailPct,
                    "needPlayed": durationSec * Self.minPlayedPct,
                ], cat: "offline")
            }
        }
    }

    func getProgress(profileId: String?, itemId: String, filePath: String) -> [String: Any] {
        queue.sync {
            let obj = read()
            let pid = (profileId?.isEmpty == false ? profileId! : activeProfileLocked(obj))
            let records = (obj["records"] as? [String: Any]) ?? [:]
            guard let rec = records[key(pid, itemId, filePath)] as? [String: Any] else {
                return ["found": false, "positionSec": 0, "durationSec": 0, "completed": false]
            }
            var out: [String: Any] = [
                "found": true,
                "positionSec": rec["positionSec"] ?? 0,
                "durationSec": rec["durationSec"] ?? 0,
                "completed": rec["completed"] ?? false,
                "clientUpdatedAt": rec["clientUpdatedAt"] ?? "",
                "baseSyncedAt": rec["baseSyncedAt"] ?? NSNull(),
            ]
            // Track picks saved alongside progress — the offline cached player
            // restores audio/subtitle selections from these (the host's
            // remembered picks are unreachable offline). Absent when never set.
            if let s = rec["subtitleSel"] { out["subtitleSel"] = s }
            if let a = rec["audioSel"] { out["audioSel"] = a }
            if let a = rec["localAudioIdx"] { out["localAudioIdx"] = a }
            if let i = rec["localSubtitleIdx"] { out["localSubtitleIdx"] = i }
            return out
        }
    }

    /// Adopt the server's progress as the local baseline for a downloaded file so
    /// **offline resume reflects history accrued online**. Skips the write when the
    /// device holds **unsynced** offline progress (`dirty`) for this file — that
    /// would clobber something not yet pushed (the pending push will reconcile it).
    /// Otherwise it writes a settled (`dirty:false`) record so pending() ignores it.
    func seedProgress(profileId: String?, itemId: String, filePath: String,
                      positionSec: Double, durationSec: Double, completed: Bool,
                      serverUpdatedAt: String, playedSec: Double? = nil,
                      force: Bool = false, serverId: String? = nil) {
        queue.sync {
            var obj = read()
            let pid = (profileId?.isEmpty == false ? profileId! : activeProfileLocked(obj))
            var records = (obj["records"] as? [String: Any]) ?? [:]
            let k = key(pid, itemId, filePath)
            // `force` (M4 "server wins") overrides the dirty guard — the user has
            // chosen the server's value, so the unsynced device record is discarded.
            let existing = records[k] as? [String: Any]
            if !force, let e = existing, (e["dirty"] as? Bool) == true {
                // Unsynced local progress — don't overwrite; the push handles it.
                // The server it belongs to is still worth learning.
                if let sid = serverId, !sid.isEmpty, e["serverId"] == nil {
                    var e2 = e; e2["serverId"] = sid
                    records[k] = e2; obj["records"] = records; write(obj)
                }
                return
            }
            let stamp = serverUpdatedAt.isEmpty ? Self.isoNow() : serverUpdatedAt
            var seeded: [String: Any] = [
                "profileId": pid, "itemId": itemId, "filePath": filePath,
                "positionSec": (positionSec * 10).rounded() / 10,
                "durationSec": (durationSec * 10).rounded() / 10,
                "completed": completed,
                // The host's play count, so offline viewing builds on it (the host
                // merges by max). An older host sends none: trust the position,
                // exactly as the host's own rule seeds a legacy record.
                "playedSec": ((playedSec ?? positionSec) * 10).rounded() / 10,
                "clientUpdatedAt": stamp,
                "baseSyncedAt": stamp,   // server watermark for conflict detection
                "dirty": false,          // settled — pending() will not re-push it
            ]
            // A seed replaces the record, so carry the server across: the one
            // given, else the one the record already had.
            if let sid = serverId, !sid.isEmpty { seeded["serverId"] = sid }
            else if let prev = existing?["serverId"] { seeded["serverId"] = prev }
            records[k] = seeded
            obj["records"] = records
            write(obj)
        }
    }

    /// Events the device still needs to push: `dirty` (a local watch since the last
    /// sync). Uses an explicit flag, NOT a device-vs-server clock comparison (which
    /// broke under clock skew). Legacy records without `dirty` fall back to the old
    /// timestamp heuristic. Trivial (< 5 s, not completed) positions are skipped —
    /// the server ignores those anyway.
    func pending() -> [[String: Any]] {
        queue.sync {
            let records = (read()["records"] as? [String: Any]) ?? [:]
            var out: [[String: Any]] = []
            for (_, v) in records {
                guard let rec = v as? [String: Any] else { continue }
                let pos = (rec["positionSec"] as? Double) ?? Double((rec["positionSec"] as? Int) ?? 0)
                let completed = (rec["completed"] as? Bool) ?? false
                if pos < 5 && !completed { continue }
                if let dirty = rec["dirty"] as? Bool {
                    if !dirty { continue }            // explicitly settled
                } else {
                    // Legacy record (pre-`dirty`): fall back to the timestamp heuristic.
                    let base = rec["baseSyncedAt"] as? String
                    let client = rec["clientUpdatedAt"] as? String
                    if let b = base, let c = client,
                       let bd = Self.parse(b), let cd = Self.parse(c), cd <= bd {
                        continue
                    }
                }
                out.append(eventDict(rec))
            }
            // The offline->online handover, from the side that knows what it is
            // holding. A record that is `dirty` and never leaves is an episode
            // the user watched on a plane that the library will never show as
            // watched — and until now the only trace of it was its continued
            // absence.
            if !out.isEmpty {
                DiagLog.shared.write("offline-pending", ["count": out.count], cat: "offline")
            }
            return out
        }
    }

    func markSynced(_ applied: [[String: Any]]) {
        DiagLog.shared.write("offline-synced", ["count": applied.count], cat: "offline")
        queue.sync {
            var obj = read()
            let active = activeProfileLocked(obj)
            var records = (obj["records"] as? [String: Any]) ?? [:]
            for a in applied {
                guard let itemId = a["itemId"] as? String,
                      let filePath = a["filePath"] as? String else { continue }
                let pid = (a["profileId"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? active
                let k = key(pid, itemId, filePath)
                guard var rec = records[k] as? [String: Any] else { continue }
                rec["baseSyncedAt"] = (a["serverUpdatedAt"] as? String) ?? Self.isoNow()
                rec["dirty"] = false   // acknowledged by the server — stop re-pushing
                records[k] = rec
            }
            obj["records"] = records
            write(obj)
        }
    }

    func all() -> [[String: Any]] {
        queue.sync {
            let records = (read()["records"] as? [String: Any]) ?? [:]
            return records.compactMap { ($0.value as? [String: Any]).map(eventDict) }
        }
    }

    func clear() {
        queue.sync {
            var obj = read()
            obj["records"] = [String: Any]()
            write(obj)
        }
    }

    // MARK: helpers

    private func eventDict(_ rec: [String: Any]) -> [String: Any] {
        var e: [String: Any] = [
            "profileId": rec["profileId"] ?? "",
            "itemId": rec["itemId"] ?? "",
            "filePath": rec["filePath"] ?? "",
            "positionSec": rec["positionSec"] ?? 0,
            "durationSec": rec["durationSec"] ?? 0,
            "completed": rec["completed"] ?? false,
            "clientUpdatedAt": rec["clientUpdatedAt"] ?? "",
            "baseSyncedAt": rec["baseSyncedAt"] ?? NSNull(),
            // Surfaced for the on-device sync diagnostic; the flush ignores it.
            "dirty": rec["dirty"] ?? true,
        ]
        // Present once this build has written or seeded the record. A record left
        // dirty by an older build syncs without it: the host goes position-only.
        if let p = rec["playedSec"] { e["playedSec"] = p }
        if let sid = rec["serverId"] { e["serverId"] = sid }
        if let s = rec["subtitleSel"] { e["subtitleSel"] = s }
        if let a = rec["audioSel"] { e["audioSel"] = a }
        if let a = rec["localAudioIdx"] { e["localAudioIdx"] = a }
        if let s = rec["localSubtitleIdx"] { e["localSubtitleIdx"] = s }
        return e
    }

    // Mirrors watchrule.py on the host. Keep the two in step.
    private static let finishTailPct = 0.90      // FINISH_TAIL_PCT
    private static let minPlayedPct = 0.60       // MIN_PLAYED_PCT
    private static let playedRateCap = 2.5       // PLAYED_RATE_CAP
    private static let playedStepCapSec = 60.0   // PLAYED_STEP_CAP_SEC

    /// A finite, non-negative Double out of a JSON value (NSNumber / Double / Int).
    private static func num(_ v: Any?) -> Double {
        let d: Double
        if let x = v as? Double { d = x }
        else if let x = v as? Int { d = Double(x) }
        else if let x = v as? NSNumber { d = x.doubleValue }
        else { return 0 }
        return d.isFinite ? max(0, d) : 0
    }

    // ISO-8601 UTC, seconds precision — matches the server's _now_iso shape
    // closely enough; the server parses both "Z" and "+00:00".
    private static let isoFmt: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    static func isoNow() -> String { isoFmt.string(from: Date()) }
    static func parse(_ s: String) -> Date? {
        if let d = isoFmt.date(from: s) { return d }
        // Fall back for fractional seconds or odd offsets.
        let alt = ISO8601DateFormatter()
        alt.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return alt.date(from: s)
    }
}
