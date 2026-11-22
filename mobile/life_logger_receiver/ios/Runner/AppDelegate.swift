import Flutter
import UIKit

@main
@objc class AppDelegate: FlutterAppDelegate {
  private let receiverChannelName = "life_logger_receiver/platform"

  override func application(
    _ application: UIApplication,
    didFinishLaunchingWithOptions launchOptions: [UIApplication.LaunchOptionsKey: Any]?
  ) -> Bool {
    GeneratedPluginRegistrant.register(with: self)
    if let controller = window?.rootViewController as? FlutterViewController {
      let channel = FlutterMethodChannel(
        name: receiverChannelName,
        binaryMessenger: controller.binaryMessenger
      )
      channel.setMethodCallHandler { call, result in
        switch call.method {
        case "appSupportDirectory":
          do {
            let support = try FileManager.default.url(
              for: .applicationSupportDirectory,
              in: .userDomainMask,
              appropriateFor: nil,
              create: true
            ).appendingPathComponent("LifeLoggerReceiver", isDirectory: true)
            try FileManager.default.createDirectory(
              at: support,
              withIntermediateDirectories: true
            )
            result(support.path)
          } catch {
            result(FlutterError(code: "storage_path", message: error.localizedDescription, details: nil))
          }
        case "excludeFromBackup":
          guard
            let arguments = call.arguments as? [String: Any],
            let path = arguments["path"] as? String
          else {
            result(FlutterError(code: "invalid_path", message: "Missing storage path", details: nil))
            return
          }
          do {
            var values = URLResourceValues()
            values.isExcludedFromBackup = true
            var url = URL(fileURLWithPath: path, isDirectory: true)
            try url.setResourceValues(values)
            result(nil)
          } catch {
            result(FlutterError(code: "backup_exclusion", message: error.localizedDescription, details: nil))
          }
        default:
          result(FlutterMethodNotImplemented)
        }
      }
    }
    return super.application(application, didFinishLaunchingWithOptions: launchOptions)
  }
}
