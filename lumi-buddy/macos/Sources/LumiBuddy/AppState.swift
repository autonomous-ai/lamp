import Foundation

enum PairingStatus: Equatable {
    case notPaired
    case paired(buddyID: String, lampHost: String)
}

enum ConnectionStatus: Equatable {
    case disconnected
    case connecting
    case connected
    case error(String)
}

struct CommandRecord {
    let id: String
    let action: String
    let ok: Bool
    let error: String?
    let timestamp: Date
}

final class AppState {
    static let shared = AppState()

    // Cap on the in-memory ring buffer. The full audit trail lives on disk
    // (see AuditLog.swift) — this list is just what the menu bar can show.
    static let recentCommandsCap = 20

    private(set) var pairing: PairingStatus = .notPaired { didSet { notify() } }
    private(set) var connection: ConnectionStatus = .disconnected { didSet { notify() } }
    private(set) var discoveredLamps: [LampInfo] = [] { didSet { notify() } }
    private(set) var paused: Bool = false { didSet { notify() } }
    private(set) var recentCommands: [CommandRecord] = [] { didSet { notify() } }

    var lastCommand: CommandRecord? { recentCommands.first }

    var onChange: (() -> Void)?

    private init() {}

    func setPairing(_ status: PairingStatus) { onMain { self.pairing = status } }
    func setConnection(_ status: ConnectionStatus) { onMain { self.connection = status } }
    func setDiscoveredLamps(_ lamps: [LampInfo]) { onMain { self.discoveredLamps = lamps } }
    func setPaused(_ paused: Bool) { onMain { self.paused = paused } }
    func recordCommand(_ record: CommandRecord) {
        onMain {
            var list = self.recentCommands
            list.insert(record, at: 0)
            if list.count > Self.recentCommandsCap {
                list.removeLast(list.count - Self.recentCommandsCap)
            }
            self.recentCommands = list
        }
    }

    private func notify() {
        // didSet runs on whichever thread the setter ran. setPairing etc. always hop to main first,
        // so onChange always fires on main.
        onChange?()
    }

    private func onMain(_ block: @escaping () -> Void) {
        if Thread.isMainThread { block() }
        else { DispatchQueue.main.async(execute: block) }
    }
}
