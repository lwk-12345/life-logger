import 'dart:io';

import 'package:flutter/services.dart';

class ReceiverPlatformAdapter {
  static const MethodChannel _channel =
      MethodChannel('life_logger_receiver/platform');

  Future<Directory> appSupportDirectory() async {
    final path = await _channel.invokeMethod<String>('appSupportDirectory');
    if (path == null || path.isEmpty) {
      throw StateError('Native app-support directory is unavailable.');
    }
    return Directory(path);
  }

  Future<void> excludeFromCloudBackup(Directory directory) async {
    if (Platform.isIOS) {
      await _channel.invokeMethod<void>(
        'excludeFromBackup',
        <String, Object>{'path': directory.path},
      );
    }
  }

  Future<bool> requestLocalNetworkPermission() async {
    if (Platform.isAndroid) {
      return await _channel.invokeMethod<bool>('requestLocalNetworkPermission') ??
          false;
    }
    // iOS presents its Local Network prompt when the app first accesses LAN.
    return true;
  }

  Future<void> startAndroidForegroundService() async {
    if (Platform.isAndroid) {
      await _channel.invokeMethod<void>('startForegroundReceiver');
    }
  }

  Future<void> stopAndroidForegroundService() async {
    if (Platform.isAndroid) {
      await _channel.invokeMethod<void>('stopForegroundReceiver');
    }
  }
}
