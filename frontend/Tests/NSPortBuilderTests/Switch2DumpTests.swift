import XCTest
@testable import NSPortBuilder

/// A Switch 2 dump (exefs/ and romfs/) is recognized without listing the romfs, and a build of one
/// passes --switch2 and does not pass prod.keys.
final class Switch2DumpTests: XCTestCase {
    private var temporary: URL!
    private let app: UInt64 = 0x040074A01BF12000

    override func setUpWithError() throws {
        temporary = FileManager.default.temporaryDirectory.appendingPathComponent("nx-switch2-" + UUID().uuidString)
        try FileManager.default.createDirectory(at: temporary, withIntermediateDirectories: true)
    }

    override func tearDownWithError() throws { try? FileManager.default.removeItem(at: temporary) }

    private func std(_ path: String) -> String { URL(fileURLWithPath: path).standardizedFileURL.path }

    /// META … ACI0, program id at ACI0 + 0x10. The same layout main.npdm uses.
    private func npdm(_ program: UInt64) -> Data {
        var data = Data(count: 0x80)
        data.replaceSubrange(0..<4, with: Data("META".utf8))
        var aci = Data("ACI0".utf8)
        aci.append(Data(count: 12))
        var value = program.littleEndian
        aci.append(Data(bytes: &value, count: 8))
        data.append(aci)
        return data
    }

    /// `Game (v3.2.0)/updates/<version>/{exefs,romfs}` and one DLC at index 1. Returns (game, version).
    private func dump(version: String = "458752", program: UInt64? = nil, main name: String = "main") throws -> (URL, URL) {
        let game = temporary.appendingPathComponent("Donkey Kong Bananza (v3.2.0)")
        let ver = game.appendingPathComponent("updates").appendingPathComponent(version)
        let exefs = ver.appendingPathComponent("exefs")
        try FileManager.default.createDirectory(at: exefs, withIntermediateDirectories: true)
        try Data("NSO0".utf8).write(to: exefs.appendingPathComponent(name))
        try npdm(program ?? app).write(to: exefs.appendingPathComponent("main.npdm"))
        try FileManager.default.createDirectory(at: ver.appendingPathComponent("romfs"), withIntermediateDirectories: true)
        try Data("rom".utf8).write(to: ver.appendingPathComponent("romfs").appendingPathComponent("a.txt"))
        let dlc = game.appendingPathComponent("dlc").appendingPathComponent(String(format: "%016llx", app + 0x1000 + 1)).appendingPathComponent("0").appendingPathComponent("romfs")
        try FileManager.default.createDirectory(at: dlc, withIntermediateDirectories: true)
        try Data("dlc".utf8).write(to: dlc.appendingPathComponent("b.txt"))
        // index 0 is not reported by the host
        let zero = game.appendingPathComponent("dlc").appendingPathComponent(String(format: "%016llx", app + 0x1000)).appendingPathComponent("0").appendingPathComponent("romfs")
        try FileManager.default.createDirectory(at: zero, withIntermediateDirectories: true)
        return (game, ver)
    }

    func testVersionFolderAndGameRoot() throws {
        let (game, ver) = try dump()
        for folder in [ver, game] {
            let found = try XCTUnwrap(Switch2Dump.find(in: folder.path))
            XCTAssertEqual(found.titleID, app)
            XCTAssertEqual(found.version, "v458752")
            XCTAssertEqual(found.name, "Donkey Kong Bananza")
            XCTAssertEqual(found.display, "3.2.0")
            XCTAssertEqual(found.dlcCount, 1)
            XCTAssertEqual(found.root, std(game.path))
        }
        let fromVersion = try XCTUnwrap(Switch2Dump.find(in: ver.path))
        XCTAssertEqual(fromVersion.source, std(ver.path), "a version folder is passed through, and the script finds DLC beside updates/")
        let fromGame = try XCTUnwrap(Switch2Dump.find(in: game.path))
        XCTAssertEqual(fromGame.source, std(game.path))
    }

    func testExefsOrRomfsResolvesToTheVersion() throws {
        let (_, ver) = try dump()
        let fromExefs = try XCTUnwrap(Switch2Dump.find(in: ver.appendingPathComponent("exefs").path))
        let fromRomfs = try XCTUnwrap(Switch2Dump.find(in: ver.appendingPathComponent("romfs").path))
        XCTAssertEqual(fromExefs.source, std(ver.path))
        XCTAssertEqual(fromRomfs.source, std(ver.path))
        XCTAssertEqual(fromRomfs.dlcCount, 1)
    }

    func testUpdateProgramIDIsTheApplication() throws {
        let (game, _) = try dump(program: app | 0x800)
        let found = try XCTUnwrap(Switch2Dump.find(in: game.path))
        XCTAssertEqual(found.titleID, app)
    }

    func testAddOnProgramIDIsNotATitle() throws {
        let (game, _) = try dump(program: app + 0x1000 + 1)
        let found = try XCTUnwrap(Switch2Dump.find(in: game.path))
        XCTAssertNil(found.titleID)
    }

    func testMainNsoAndUppercaseNames() throws {
        let game = temporary.appendingPathComponent("Upper (v1.0)")
        let ver = game.appendingPathComponent("updates").appendingPathComponent("3")
        let exefs = ver.appendingPathComponent("ExeFS")
        try FileManager.default.createDirectory(at: exefs, withIntermediateDirectories: true)
        try Data("NSO0".utf8).write(to: exefs.appendingPathComponent("main.nso"))
        try npdm(app).write(to: exefs.appendingPathComponent("main.npdm"))
        try FileManager.default.createDirectory(at: ver.appendingPathComponent("RomFS"), withIntermediateDirectories: true)
        let found = try XCTUnwrap(Switch2Dump.find(in: ver.path))
        XCTAssertEqual(found.version, "v3")
        XCTAssertEqual(found.titleID, app)
        XCTAssertEqual(found.display, "1.0")
    }

    func testAnNSPFolderIsNotADump() throws {
        let folder = temporary.appendingPathComponent("NSPs")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try Data([0]).write(to: folder.appendingPathComponent("Game [0100000000010000].nsp"))
        XCTAssertNil(Switch2Dump.find(in: folder.path))
        let model = BuildModel()
        model.scan(folder: folder.path)
        XCTAssertNil(model.switch2)
        XCTAssertEqual(model.basePath, folder.appendingPathComponent("Game [0100000000010000].nsp").path)
    }

    func testChoosingADumpClearsNSPSlotsAndADroppedNSPClearsTheDump() throws {
        let (_, ver) = try dump()
        let model = BuildModel()
        model.slots[0].paths = ["/tmp/old.nsp"]
        model.slots[3].paths = ["/tmp/prod.keys"]
        model.take(dropped: [ver.path])
        XCTAssertEqual(model.switch2?.version, "v458752")
        XCTAssertTrue(model.basePath.isEmpty)
        XCTAssertTrue(model.keysPath.isEmpty)
        let nsp = temporary.appendingPathComponent("Game [0100000000010000].nsp")
        try Data([1]).write(to: nsp)
        model.take(dropped: [nsp.path])
        XCTAssertNil(model.switch2)
        XCTAssertEqual(model.basePath, nsp.path)
    }

    func testTheNewestUpdateIsTheOneShown() throws {
        let (game, _) = try dump(version: "100")
        let later = game.appendingPathComponent("updates").appendingPathComponent("458752")
        let exefs = later.appendingPathComponent("exefs")
        try FileManager.default.createDirectory(at: exefs, withIntermediateDirectories: true)
        try Data("NSO0".utf8).write(to: exefs.appendingPathComponent("main"))
        try npdm(app).write(to: exefs.appendingPathComponent("main.npdm"))
        try FileManager.default.createDirectory(at: later.appendingPathComponent("romfs"), withIntermediateDirectories: true)
        let found = try XCTUnwrap(Switch2Dump.find(in: game.path))
        XCTAssertEqual(found.version, "v458752")
    }

    /// The Bananza dump this was built against, when it is on this machine. Skipped otherwise.
    func testBananzaDumpIfPresent() throws {
        let ver = "/Users/lwarn/Downloads/Donkey Kong Bananza (v3.2.0)/updates/458752"
        guard FileManager.default.fileExists(atPath: ver + "/exefs/main.npdm") else {
            throw XCTSkip("Bananza dump is not on this machine")
        }
        let found = try XCTUnwrap(Switch2Dump.find(in: ver))
        XCTAssertEqual(found.titleID, app)
        XCTAssertEqual(found.version, "v458752")
        XCTAssertEqual(found.name, "Donkey Kong Bananza")
        XCTAssertEqual(found.display, "3.2.0")
        XCTAssertGreaterThanOrEqual(found.dlcCount, 1)
        XCTAssertTrue(found.source.hasSuffix("updates/458752"))
    }

    /// The command is --switch2 and no prod.keys. The script is a stand-in that sleeps; nothing is built.
    @MainActor func testBuildPassesSwitch2AndNoKeys() throws {
        guard FileManager.default.isExecutableFile(atPath: "/usr/bin/python3") else { throw XCTSkip("System Python is required") }
        let saved = ["repoPath", "buildTarget", "buildOutputDirectory", "installDevice"].map { ($0, UserDefaults.standard.object(forKey: $0)) }
        defer { for (key, value) in saved { UserDefaults.standard.set(value, forKey: key) } }
        let (_, ver) = try dump()
        let model = BuildModel()
        model.repoPath = temporary.path
        model.target = "ios"
        model.installDevice = ""
        model.outDir = temporary.path
        model.pythonPath = "/usr/bin/python3"
        model.dotnetPath = "/usr/bin/true"
        model.depsChecked = true
        model.deps = [BuildModel.Dep(id: "test", name: "test", hint: "", present: true, installCmd: [])]
        let marker = temporary.appendingPathComponent("dm/tools/nx_extract/Program.cs")
        try FileManager.default.createDirectory(at: marker.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: marker)
        try BuildModel.requiredTools.filter { $0 != BuildModel.buildScript }.forEach { tool in
            let url = temporary.appendingPathComponent(tool)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try Data().write(to: url)
        }
        let script = temporary.appendingPathComponent(BuildModel.buildScript)
        try FileManager.default.createDirectory(at: script.deletingLastPathComponent(), withIntermediateDirectories: true)
        try """
        import time
        # '--work-dir' '--switch2'
        time.sleep(30)
        """.write(to: script, atomically: true, encoding: .utf8)
        model.scan(folder: ver.path)
        XCTAssertTrue(model.canBuild)
        model.startBuild()
        XCTAssertTrue(model.lastBuildCommand.contains("--switch2"))
        XCTAssertTrue(model.lastBuildCommand.contains(model.switch2?.source ?? "missing"))
        XCTAssertFalse(model.lastBuildCommand.contains("-k"))
        XCTAssertTrue(model.lastBuildCommand.contains("--work-dir"))
        model.stop()
    }
}
