//
//  OfflineStore.swift
//  StreamLink iOS — M3 (offline progress + auto-sync)
//
//  A small, durable, native key/value log of watch progress captured while the
//  device is offline. The offline player (www/downloads.html) writes progress
//  here during playback (the host dashboard can't load with no network, so this
//  is the only place offline history can live); the dashboard's B5 sync glue
//  (static/index.html), which DOES run on the host once reconnected, drains it to
//  POST /api/library/sync/progress and records each file's new `base_synced_at`
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
//                   profileId?, subtitleSel?, localAudioIdx?,
//                   localSubtitleIdx? })                      -> {}
//    getProgress({ itemId, filePath, profileId? })           -> { found, positionSec,
//                                                                  durationSec, completed,
//                                                                  clientUpdatedAt, baseSyncedAt }
//    seedProgress({ itemId, filePath, positionSec, durationSec,
//                   completed, serverUpdatedAt, profileId? })  -> {}   // server→device baseline
//    pending()                                               -> { events:[ <event> ] }
//    markSynced({ applied:[ { itemId, filePath, serverUpdatedAt, profileId? } ] }) -> {}
//    all()                                                   -> { events:[ <event> ] }
//    clear()                                                 -> {}
//
//  <event> = { profileId, itemId, filePath, positionSec, durationSec, completed,
//              clientUpdatedAt, baseSyncedAt, subtitleSel?, localAudioIdx?,
//              localSubtitleIdx? }
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
        store.saveProgress(
            profileId: pid, itemId: itemId, filePath: filePath,
            positionSec: pos, durationSec: dur,
            subtitleSel: sel,
            localAudioIdx: call.getInt("localAudioIdx"),
            localSubtitleIdx: call.getInt("localSubtitleIdx"))
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
            serverUpdatedAt: call.getString("serverUpdatedAt") ?? "")
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
                      subtitleSel: [String: Any]?, localAudioIdx: Int?, localSubtitleIdx: Int?) {
        queue.sync {
            var obj = read()
            let pid = (profileId?.isEmpty == false ? profileId! : activeProfileLocked(obj))
            var records = (obj["records"] as? [String: Any]) ?? [:]
            let k = key(pid, itemId, filePath)
            var rec = (records[k] as? [String: Any]) ?? [:]

            let pct = durationSec > 0 ? positionSec / durationSec : 0
            let nowCompleted = pct > 0.92
            // `completed` is monotonic on-device too (mirrors the server merge).
            let prevCompleted = (rec["completed"] as? Bool) ?? false

            rec["profileId"] = pid
            rec["itemId"] = itemId
            rec["filePath"] = filePath
            rec["positionSec"] = (positionSec * 10).rounded() / 10
            rec["durationSec"] = (durationSec * 10).rounded() / 10
            rec["completed"] = nowCompleted || prevCompleted
            rec["clientUpdatedAt"] = Self.isoNow()
            if rec["baseSyncedAt"] == nil { rec["baseSyncedAt"] = NSNull() }
            if let s = subtitleSel { rec["subtitleSel"] = s }
            if let a = localAudioIdx { rec["localAudioIdx"] = a }
            if let s = localSubtitleIdx { rec["localSubtitleIdx"] = s }

            records[k] = rec
            obj["records"] = records
            write(obj)
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
            return [
                "found": true,
                "positionSec": rec["positionSec"] ?? 0,
                "durationSec": rec["durationSec"] ?? 0,
                "completed": rec["completed"] ?? false,
                "clientUpdatedAt": rec["clientUpdatedAt"] ?? "",
                "baseSyncedAt": rec["baseSyncedAt"] ?? NSNull(),
            ]
        }
    }

    /// Adopt the server's progress as the local baseline for a downloaded file so
    /// **offline resume reflects history accrued online**. Skips the write when the
    /// device holds *newer, unsynced* offline progress for this file (a pending
    /// edit whose clientUpdatedAt is past both its own baseSyncedAt and the server
    /// timestamp) — that would clobber something not yet pushed. Otherwise it writes
    /// a settled record (clientUpdatedAt == baseSyncedAt == serverUpdatedAt), so it
    /// is never re-pushed by pending().
    func seedProgress(profileId: String?, itemId: String, filePath: String,
                      positionSec: Double, durationSec: Double, completed: Bool,
                      serverUpdatedAt: String) {
        queue.sync {
            var obj = read()
            let pid = (profileId?.isEmpty == false ? profileId! : activeProfileLocked(obj))
            var records = (obj["records"] as? [String: Any]) ?? [:]
            let k = key(pid, itemId, filePath)
            let serverDate = Self.parse(serverUpdatedAt)
            if let existing = records[k] as? [String: Any] {
                let cu = (existing["clientUpdatedAt"] as? String).flatMap(Self.parse)
                let bs = (existing["baseSyncedAt"] as? String).flatMap(Self.parse)
                let hasUnsynced = (cu != nil) && (bs == nil || (cu! > bs!))
                if hasUnsynced {
                    // Local edits pending. Only let the server win if it's strictly newer.
                    if let sd = serverDate, let c = cu, sd > c {
                        // server newer — fall through to overwrite
                    } else {
                        return
                    }
                }
            }
            let stamp = serverUpdatedAt.isEmpty ? Self.isoNow() : serverUpdatedAt
            records[k] = [
                "profileId": pid, "itemId": itemId, "filePath": filePath,
                "positionSec": (positionSec * 10).rounded() / 10,
                "durationSec": (durationSec * 10).rounded() / 10,
                "completed": completed,
                "clientUpdatedAt": stamp,
                "baseSyncedAt": stamp,   // settled — pending() will not re-push it
            ]
            obj["records"] = records
            write(obj)
        }
    }

    /// Events not yet confirmed by the server: never synced, OR watched again
    /// since the last sync (clientUpdatedAt > baseSyncedAt). Filters out trivial
    /// (< 5 s, not completed) positions — the server ignores those anyway.
    func pending() -> [[String: Any]] {
        queue.sync {
            let records = (read()["records"] as? [String: Any]) ?? [:]
            var out: [[String: Any]] = []
            for (_, v) in records {
                guard let rec = v as? [String: Any] else { continue }
                let pos = (rec["positionSec"] as? Double) ?? Double((rec["positionSec"] as? Int) ?? 0)
                let completed = (rec["completed"] as? Bool) ?? false
                if pos < 5 && !completed { continue }
                let client = rec["clientUpdatedAt"] as? String
                let base = rec["baseSyncedAt"] as? String   // nil/NSNull ⇒ never synced
                if let b = base, let c = client,
                   let bd = Self.parse(b), let cd = Self.parse(c), cd <= bd {
                    continue   // already synced and not re-watched
                }
                out.append(eventDict(rec))
            }
            return out
        }
    }

    func markSynced(_ applied: [[String: Any]]) {
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
        ]
        if let s = rec["subtitleSel"] { e["subtitleSel"] = s }
        if let a = rec["localAudioIdx"] { e["localAudioIdx"] = a }
        if let s = rec["localSubtitleIdx"] { e["localSubtitleIdx"] = s }
        return e
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
