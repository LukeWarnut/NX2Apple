import Foundation

/// A Switch 2 dump: already-decrypted `exefs/` and `romfs/`, not an NSP. The same shape
/// `scripts/switch2_build.py` reads — a version folder (`updates/458752`) or a game folder
/// (`updates/<version>/` and `dlc/<title id>/`). Nothing here lists a `romfs` directory:
/// a game's romfs is tens of thousands of files, and the build only needs the folder itself.
struct Switch2Dump: Equatable {
    /// What `--switch2` is given. A version folder, or the game folder when that is what was chosen.
    var source: String
    /// The game folder: the parent of `updates/`, where the build folder goes and where `dlc/` sits.
    var root: String
    var version: String
    /// The application title ID from `main.npdm` (an update's `…800` id is masked down). Nil when the file has none.
    var titleID: UInt64?
    var name: String
    var display: String
    var dlcCount: Int

    /// The dump at `folder`, or at a parent of it (a chosen `romfs/` or `exefs/` is the version folder's).
    /// Nil when the folder is not this layout — an NSP folder stays an NSP folder.
    static func find(in folder: String) -> Switch2Dump? {
        var here = URL(fileURLWithPath: folder).standardizedFileURL.path
        for _ in 0..<6 {
            if let dump = inspect(here) { return dump }
            let up = (here as NSString).deletingLastPathComponent
            if up == here || up == "/" { return nil }
            here = up
        }
        return nil
    }

    /// `here` itself, not its parents. A version folder is passed through as the source; a game folder too.
    private static func inspect(_ here: String) -> Switch2Dump? {
        if let exefs = childDirectory(here, "exefs"), childDirectory(here, "romfs") != nil {
            guard hasMain(exefs) else { return nil }
            let parent = (here as NSString).deletingLastPathComponent
            let parentName = (parent as NSString).lastPathComponent.lowercased()
            let root: String
            let source: String
            let version: String
            if parentName == "updates" {
                root = (parent as NSString).deletingLastPathComponent
                source = here
                version = versionName((here as NSString).lastPathComponent)
            } else if parentName == "base" {
                root = (parent as NSString).deletingLastPathComponent
                source = root   // the game folder, so the script also sees updates/ and dlc/ beside base/
                version = "v0"
            } else {
                root = here
                source = here
                version = versionName((here as NSString).lastPathComponent)
            }
            return make(source: source, root: root, version: version, exefs: exefs)
        }
        guard let updates = childDirectory(here, "updates") else { return nil }
        var versions: [(String, String, String)] = []   // version, directory, exefs
        for name in entries(updates) {
            let dir = updates + "/" + name
            guard let exefs = childDirectory(dir, "exefs"), childDirectory(dir, "romfs") != nil, hasMain(exefs) else { continue }
            versions.append((versionName(name), dir, exefs))
        }
        if let base = childDirectory(here, "base"), let exefs = childDirectory(base, "exefs"),
           childDirectory(base, "romfs") != nil, hasMain(exefs) {
            versions.append(("v0", base, exefs))
        }
        guard let newest = versions.max(by: { versionNumber($0.0) < versionNumber($1.0) }) else { return nil }
        return make(source: here, root: here, version: newest.0, exefs: newest.2)
    }

    private static func make(source: String, root: String, version: String, exefs: String) -> Switch2Dump {
        let title = file(exefs, "main.npdm").flatMap { programID(of: $0) }.flatMap { applicationID($0) }
        let (name, display) = titles(from: root)
        return Switch2Dump(source: source, root: root, version: version, titleID: title,
                           name: name, display: display, dlcCount: dlcCount(root: root, app: title))
    }

    /// `main` or `main.nso`, plus `main.npdm`: a directory that merely happens to be named exefs is not a dump.
    private static func hasMain(_ exefs: String) -> Bool {
        file(exefs, "main.npdm") != nil && (file(exefs, "main") != nil || file(exefs, "main.nso") != nil)
    }

    /// The names in `dir`. A directory named `romfs` is never listed: that is the game's files.
    private static func entries(_ dir: String) -> [String] {
        if (dir as NSString).lastPathComponent.lowercased() == "romfs" { return [] }
        return ((try? FileManager.default.contentsOfDirectory(atPath: dir)) ?? []).filter { !$0.hasPrefix(".") }
    }

    private static func childDirectory(_ dir: String, _ name: String) -> String? {
        let want = name.lowercased()
        for entry in entries(dir) where entry.lowercased() == want {
            let path = dir + "/" + entry
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue { return path }
        }
        return nil
    }

    private static func file(_ dir: String, _ name: String) -> String? {
        let want = name.lowercased()
        for entry in entries(dir) where entry.lowercased() == want {
            let path = dir + "/" + entry
            var isDir: ObjCBool = false
            if FileManager.default.fileExists(atPath: path, isDirectory: &isDir), !isDir.boolValue { return path }
        }
        return nil
    }

    private static func versionName(_ name: String) -> String {
        let digits = name.first == "v" ? String(name.dropFirst()) : name
        if !digits.isEmpty, digits.allSatisfy(\.isNumber) { return "v" + digits }
        return "v0"
    }

    private static func versionNumber(_ version: String) -> Int {
        Int(version.dropFirst()) ?? 0
    }

    /// `(name, display version)` from a folder named "Game (v3.2.0)", walking up past `updates/` and the version number.
    private static func titles(from root: String) -> (String, String) {
        var chain: [String] = []
        var here = root
        for _ in 0..<4 {
            chain.append(here)
            let up = (here as NSString).deletingLastPathComponent
            if up == here { break }
            here = up
        }
        let named = try? NSRegularExpression(pattern: #"^(.*?)\s*\(v(\d+(?:\.\d+){0,2})\)\s*$"#)
        let skip: Set<String> = ["updates", "dlc", "exefs", "romfs", "base"]
        if let named {
            for path in chain {
                let name = (path as NSString).lastPathComponent
                let ns = name as NSString
                guard let m = named.firstMatch(in: name, range: NSRange(location: 0, length: ns.length)),
                      m.range(at: 1).length > 0 else { continue }
                let title = ns.substring(with: m.range(at: 1)).trimmingCharacters(in: .whitespaces)
                if !title.isEmpty { return (title, ns.substring(with: m.range(at: 2))) }
            }
        }
        for path in chain {
            let name = (path as NSString).lastPathComponent
            if skip.contains(name.lowercased()) || versionName(name) != "v0" || isHex16(name) { continue }
            if name.isEmpty || name == "/" { continue }
            return (name, "")
        }
        return ("", "")
    }

    /// Add-on content the host will report: `dlc/<title id>/…/romfs`, index = title − application − 0x1000, from 1.
    /// Index 0 is not counted; the host ignores a name that does not start with 1–9.
    private static func dlcCount(root: String, app: UInt64?) -> Int {
        guard let dlc = childDirectory(root, "dlc") else { return 0 }
        var seen = Set<UInt64>()
        for name in entries(dlc) {
            let path = dlc + "/" + name
            var index: UInt64?
            if isHex16(name), let app, let tid = UInt64(name, radix: 16) {
                let n = tid &- app &- 0x1000
                if n >= 1 && n <= 2000, hasRomfs(path) { index = n }
            } else if let n = UInt64(name), n >= 1 && n <= 2000, childDirectory(path, "romfs") != nil {
                index = n
            }
            if let index { seen.insert(index) }
        }
        return seen.count
    }

    /// `romfs` directly inside, or one level down (`dlc/<title id>/0/romfs`). The romfs directory is not listed.
    private static func hasRomfs(_ dir: String) -> Bool {
        if childDirectory(dir, "romfs") != nil { return true }
        for name in entries(dir) where name.lowercased() != "romfs" {
            if childDirectory(dir + "/" + name, "romfs") != nil { return true }
        }
        return false
    }

    private static func isHex16(_ name: String) -> Bool {
        name.range(of: #"^[0-9A-Fa-f]{16}$"#, options: .regularExpression) != nil
    }

    /// The program id at ACI0 + 0x10, little-endian. The same read as `switch2_build.program_id`.
    static func programID(of npdm: String) -> UInt64? {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: npdm)) else { return nil }
        guard let at = data.range(of: Data("ACI0".utf8)) else { return nil }
        let off = at.lowerBound + 0x10
        guard off + 8 <= data.count else { return nil }
        var value: UInt64 = 0
        for i in 0..<8 { value |= UInt64(data[off + i]) << (8 * i) }
        return value
    }

    /// An update's id ends in 800. Add-on content is not a game's `main.npdm`.
    static func applicationID(_ program: UInt64) -> UInt64? {
        let low = program & 0xFFF
        if low == 0 { return program }
        if low == 0x800 { return program & ~0xFFF }
        return nil
    }
}
