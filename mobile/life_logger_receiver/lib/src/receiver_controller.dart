import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter/foundation.dart';
import 'package:flutter/widgets.dart';

import 'mac_sync_client.dart';
import 'models.dart';
import 'passport_protocol.dart';
import 'passport_store.dart';
import 'platform_adapter.dart';
import 'receiver_server.dart';
import 'sync_outbox.dart';

class ReceiverController extends ChangeNotifier with WidgetsBindingObserver {
  ReceiverController() {
    WidgetsBinding.instance.addObserver(this);
  }

  final ReceiverPlatformAdapter _platform = ReceiverPlatformAdapter();
  PassportStore? _store;
  PassportReceiverServer? _server;
  MacSyncClient? _syncClient;
  StreamSubscription<ReceiverStatus>? _statusSubscription;
  Timer? _syncTimer;
  bool _disposed = false;
  bool _wantedRunning = false;
  bool _resumeAfterBackground = false;
  bool _syncing = false;
  DateTime? _retryAfter;
  int _retrySeconds = 15;

  bool initialized = false;
  bool busy = false;
  bool syncing = false;
  String receiverMessage = '正在初始化本地存储…';
  String sessionState = '尚未接收';
  String syncMessage = '待配置 Mac 地址';
  String? lastPeer;
  String? device;
  String? session;
  String? lastSyncRoute;
  DateTime? lastActivity;
  DateTime? lastSyncAt;
  int receivedChunks = 0;
  int eventCount = 0;
  int expectedChunks = 0;
  int missingChunks = 0;
  int pendingSyncItems = 0;
  String lanMacUrl = '';
  String tailscaleMacUrl = '';

  bool get isListening => _server?.status.phase == ReceiverPhase.listening;
  bool get isStarting => busy || _server?.status.phase == ReceiverPhase.starting;
  bool get isFailed => _server?.status.phase == ReceiverPhase.failed;

  Future<void> initialize() async {
    try {
      final support = await _platform.appSupportDirectory();
      await support.create(recursive: true);
      final root = Directory('${support.path}/PassportReceiver');
      await root.create(recursive: true);
      await _platform.excludeFromCloudBackup(root);
      final store = PassportStore(root: root);
      final protocol = PassportProtocol(store: store);
      _store = store;
      _server = PassportReceiverServer(
        protocol: protocol,
        onRequest: _recordRequest,
      );
      _statusSubscription = _server!.statuses.listen((status) {
        receiverMessage = status.message;
        _notify();
      });
      _syncClient = MacSyncClient(outbox: PassportSyncOutbox(root: root));
      await _loadSettings(support);
      initialized = true;
      receiverMessage = '准备就绪';
      await _refreshPending();
      _syncTimer = Timer.periodic(const Duration(seconds: 20), (_) {
        unawaited(_syncPending());
      });
      _notify();
    } on Object catch (error) {
      receiverMessage = '本地存储初始化失败：$error';
      _notify();
    }
  }

  Future<void> startReceiver() async {
    if (!initialized || busy || isListening) return;
    _wantedRunning = true;
    busy = true;
    receiverMessage = '正在启动接收器…';
    _notify();
    try {
      final permissionGranted = await _platform.requestLocalNetworkPermission();
      if (!permissionGranted) {
        receiverMessage = '需要允许本地网络访问才能接收 Passport 广播。';
        return;
      }
      await _platform.startAndroidForegroundService();
      await _server?.start();
      receiverMessage =
          '等待 Passport；iOS 广播发现还需要 Apple 批准的多播 entitlement。';
      unawaited(_syncPending());
    } on Object catch (error) {
      receiverMessage = '启动失败：$error';
      await _platform.stopAndroidForegroundService();
    } finally {
      busy = false;
      _notify();
    }
  }

  Future<void> stopReceiver() async {
    _wantedRunning = false;
    _resumeAfterBackground = false;
    await _server?.stop();
    await _platform.stopAndroidForegroundService();
    receiverMessage = '接收器已停止；本地未同步项目仍保留。';
    _notify();
  }

  Future<void> saveMacEndpoints({
    required String directLanUrl,
    required String tailscaleUrl,
  }) async {
    lanMacUrl = directLanUrl.trim();
    tailscaleMacUrl = tailscaleUrl.trim();
    final support = await _platform.appSupportDirectory();
    final settings = File('${support.path}/receiver-settings.json');
    await settings.parent.create(recursive: true);
    final temporary = File('${settings.path}.tmp');
    await temporary.writeAsString(
      '${jsonEncode(<String, String>{
        'direct_lan_url': lanMacUrl,
        'tailscale_url': tailscaleMacUrl,
      })}\n',
      flush: true,
    );
    if (await settings.exists()) await settings.delete();
    await temporary.rename(settings.path);
    syncMessage = 'Mac 地址已保存';
    await _refreshPending();
    _notify();
  }

  Future<void> syncNow() => _syncPending(force: true);

  @override
  void didChangeAppLifecycleState(AppLifecycleState state) {
    if (!Platform.isIOS) return;
    if (state == AppLifecycleState.paused && isListening) {
      _resumeAfterBackground = _wantedRunning;
      unawaited(_pauseIOSReceiver());
    } else if (state == AppLifecycleState.resumed && _resumeAfterBackground) {
      _resumeAfterBackground = false;
      unawaited(startReceiver());
    }
  }

  Future<void> _pauseIOSReceiver() async {
    await _server?.stop();
    receiverMessage = '应用进入后台，iOS 接收已暂停；回到前台后会重新监听。';
    _notify();
  }

  void _recordRequest(
    PassportRequest request,
    PassportResponse response,
    String? peer,
  ) {
    if (request.path == '/health' || response.statusCode < 200 || response.statusCode >= 300) {
      return;
    }
    final payload = response.json;
    lastPeer = peer;
    lastActivity = DateTime.now();
    device = payload['device'] as String? ?? device;
    session = payload['session'] as String? ?? session;

    if (payload['type'] == 'event') {
      eventCount++;
    } else if (payload['end'] != true && payload['duplicate'] != true) {
      receivedChunks++;
      sessionState = '接收中';
    }
    final summary = payload['summary'];
    if (summary is Map<String, dynamic>) {
      expectedChunks = summary['expected_chunks'] as int? ?? expectedChunks;
      final missing = summary['missing_chunks'];
      missingChunks = missing is List ? missing.length : missingChunks;
      sessionState = summary['complete'] == true ? '已完成' : '不完整';
      if (summary['complete'] == true) receivedChunks = summary['received_unique_chunks'] as int? ?? receivedChunks;
    }
    receiverMessage = peer == null ? 'Passport 请求已处理。' : '最近 Passport 地址：$peer';
    _notify();
    unawaited(_refreshPending());
    unawaited(_syncPending());
  }

  Future<void> _syncPending({bool force = false}) async {
    final client = _syncClient;
    if (client == null || _syncing) return;
    if (!force && _retryAfter != null && DateTime.now().isBefore(_retryAfter!)) return;
    if (lanMacUrl.trim().isEmpty && tailscaleMacUrl.trim().isEmpty) {
      await _refreshPending();
      return;
    }

    _syncing = true;
    syncing = true;
    _notify();
    try {
      final report = await client.syncOnce(
        directLanUrl: lanMacUrl,
        tailscaleUrl: tailscaleMacUrl,
      );
      lastSyncAt = DateTime.now();
      lastSyncRoute = report.route;
      pendingSyncItems = report.queued;
      if (report.error == null) {
        syncMessage = report.queued == 0
            ? 'Mac 已确认全部待同步项目。'
            : '已确认 ${report.acknowledged} 项，剩余 ${report.queued} 项。';
        _retrySeconds = 15;
        _retryAfter = null;
      } else {
        syncMessage = '同步未完成：${report.error}；${report.queued} 项保留在本机。';
        _retryAfter = DateTime.now().add(Duration(seconds: _retrySeconds));
        _retrySeconds = (_retrySeconds * 2).clamp(15, 300).toInt();
      }
    } on Object catch (error) {
      syncMessage = '同步未完成：$error；待同步数据仍保留在本机。';
      _retryAfter = DateTime.now().add(Duration(seconds: _retrySeconds));
      _retrySeconds = (_retrySeconds * 2).clamp(15, 300).toInt();
      await _refreshPending();
    } finally {
      _syncing = false;
      syncing = false;
      _notify();
    }
  }

  Future<void> _refreshPending() async {
    final client = _syncClient;
    if (client == null) return;
    String destination;
    try {
      destination = client.destinationKey(
        directLanUrl: lanMacUrl,
        tailscaleUrl: tailscaleMacUrl,
      );
    } on FormatException {
      pendingSyncItems = 0;
      return;
    }
    if (destination == '|') {
      pendingSyncItems = 0;
    } else {
      pendingSyncItems = await client.outbox.pendingCount(destination: destination);
    }
  }

  Future<void> _loadSettings(Directory support) async {
    final settings = File('${support.path}/receiver-settings.json');
    if (!await settings.exists()) return;
    try {
      final decoded = jsonDecode(await settings.readAsString());
      if (decoded is Map<String, dynamic>) {
        lanMacUrl = decoded['direct_lan_url'] as String? ?? '';
        tailscaleMacUrl = decoded['tailscale_url'] as String? ?? '';
      }
    } on FormatException {
      // A corrupt settings file doesn't affect saved recordings.
    }
  }

  void _notify() {
    if (!_disposed) notifyListeners();
  }

  @override
  void dispose() {
    _disposed = true;
    WidgetsBinding.instance.removeObserver(this);
    _syncTimer?.cancel();
    unawaited(_statusSubscription?.cancel());
    unawaited(_server?.dispose());
    unawaited(_platform.stopAndroidForegroundService());
    super.dispose();
  }
}
