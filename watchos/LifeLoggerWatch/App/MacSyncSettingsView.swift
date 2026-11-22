import SwiftUI

struct MacSyncSettingsView: View {
    @State private var message = "正在查找 Life Logger…"
    @State private var isChecking = false
    @State private var currentURLString = WatchSyncConfigurationStore.load()?.baseURL.absoluteString ?? WatchSyncConfigurationStore.defaultBaseURLString

    var body: some View {
        Form {
            Section("这台 Mac") {
                Text(currentURLString)
                    .font(.caption2.monospaced())
                Text(message)
                    .font(.caption2)
                    .foregroundStyle(isChecking ? .secondary : .primary)
                Button(isChecking ? "检查中…" : "重新检查") {
                    Task { await checkConnection() }
                }
                .disabled(isChecking)
            }

            Section("自动连接") {
                Text("不用填地址或密码，Life Logger 会自动找到你的 Mac。")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
            }
        }
        .navigationTitle("Mac 连接")
        .task {
            await checkConnection()
        }
    }

    @MainActor
    private func checkConnection() async {
        isChecking = true
        message = "正在检查 Mac…"
        defer { isChecking = false }

        if let resolved = await WatchSyncConfigurationStore.resolveWorkingConfiguration() {
            currentURLString = resolved.baseURL.absoluteString
            do {
                try await MacWatchSessionTransport(configuration: resolved).checkConnection()
                message = "已连上这台 Mac。"
                return
            } catch {
                message = "连不上 Mac。录音都安全地存在手表上，连上后自动上传。"
                return
            }
        }
        message = "没有找到 Mac。录音都安全地存在手表上。"
    }
}

#if DEBUG
private struct MacSyncSettingsView_Previews: PreviewProvider {
    static var previews: some View {
        NavigationStack {
            MacSyncSettingsView()
        }
    }
}
#endif
