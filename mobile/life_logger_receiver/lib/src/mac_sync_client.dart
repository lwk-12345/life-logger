import 'dart:async';
import 'dart:io';

import 'models.dart';
import 'sync_outbox.dart';

typedef MacPost = Future<int> Function(Uri endpoint, SyncItem item);

class MacResponseRejected implements Exception {
  const MacResponseRejected(this.statusCode, this.uri);

  final int statusCode;
  final Uri uri;

  @override
  String toString() => 'Mac receiver returned HTTP $statusCode ($uri)';
}

class MacSyncClient {
  MacSyncClient({
    required this.outbox,
    MacPost? post,
    this.itemLimit = 20,
    this.byteLimit = 8 * 1024 * 1024,
  }) : _post = post ?? _httpPost;

  final PassportSyncOutbox outbox;
  final MacPost _post;
  final int itemLimit;
  final int byteLimit;

  Future<SyncReport> syncOnce({
    required String directLanUrl,
    required String tailscaleUrl,
  }) async {
    final direct = _endpoint(directLanUrl);
    final tailnet = _endpoint(tailscaleUrl);
    final destination = destinationKey(
      directLanUrl: directLanUrl,
      tailscaleUrl: tailscaleUrl,
    );
    final batch = await outbox.pendingBatch(
      destination: destination,
      itemLimit: itemLimit,
      byteLimit: byteLimit,
    );
    var acknowledged = 0;
    String? usedRoute;
    Uri? preferredRoute;

    for (final item in batch) {
      try {
        final (route, endpoint) = await _sendWithFallback(
          item,
          preferredRoute,
          direct,
          tailnet,
        );
        await outbox.acknowledge(item, destination: destination);
        acknowledged++;
        usedRoute = route;
        preferredRoute = endpoint;
      } on Object catch (error) {
        return SyncReport(
          acknowledged: acknowledged,
          queued: await outbox.pendingCount(destination: destination),
          route: usedRoute,
          error: error.toString(),
        );
      }
    }

    return SyncReport(
      acknowledged: acknowledged,
      queued: await outbox.pendingCount(destination: destination),
      route: usedRoute,
      error: direct == null && tailnet == null ? 'Mac 地址尚未配置' : null,
    );
  }

  String destinationKey({
    required String directLanUrl,
    required String tailscaleUrl,
  }) {
    final direct = _endpoint(directLanUrl);
    final tailnet = _endpoint(tailscaleUrl);
    return '${direct?.toString() ?? ''}|${tailnet?.toString() ?? ''}';
  }

  Future<(String, Uri)> _sendWithFallback(
    SyncItem item,
    Uri? preferred,
    Uri? direct,
    Uri? tailnet,
  ) async {
    final first = preferred ?? direct ?? tailnet;
    final second = first == direct ? tailnet : direct;
    if (first == null) {
      throw const FormatException('请配置 Mac 局域网或 Tailscale 地址');
    }
    try {
      await _send(first, item);
      return (first == tailnet ? 'Tailscale' : 'LAN', first);
    } on MacResponseRejected {
      rethrow;
    } on SocketException {
      if (second == null || second == first) rethrow;
    } on HttpException {
      if (second == null || second == first) rethrow;
    } on TimeoutException {
      if (second == null || second == first) rethrow;
    }
    if (second != null && second != first) {
      await _send(second, item);
      return (second == tailnet ? 'Tailscale' : 'LAN', second);
    }
    throw const SocketException('Mac 端点不可达');
  }

  Future<void> _send(Uri base, SyncItem item) async {
    final endpoint = base.resolve(item.path);
    final statusCode = await _post(endpoint, item);
    if (statusCode < 200 || statusCode >= 300) {
      throw MacResponseRejected(statusCode, endpoint);
    }
  }

  Uri? _endpoint(String raw) {
    final value = raw.trim();
    if (value.isEmpty) return null;
    final uri = Uri.tryParse(value);
    if (uri == null ||
        (uri.scheme != 'http' && uri.scheme != 'https') ||
        uri.host.isEmpty ||
        uri.userInfo.isNotEmpty ||
        (uri.path.isNotEmpty && uri.path != '/')) {
      throw FormatException('Mac 地址应为 http(s)://主机:8766');
    }
    return uri.replace(path: '', query: null, fragment: null);
  }

  static Future<int> _httpPost(Uri endpoint, SyncItem item) async {
    final client = HttpClient()..connectionTimeout = const Duration(seconds: 8);
    try {
      final request = await client.postUrl(endpoint).timeout(
            const Duration(seconds: 8),
          );
      request.headers.set(HttpHeaders.connectionHeader, 'close');
      request.contentLength = item.body.length;
      for (final header in item.headers.entries) {
        request.headers.set(header.key, header.value);
      }
      request.add(item.body);
      final response = await request.close().timeout(const Duration(seconds: 30));
      await response.drain<void>();
      return response.statusCode;
    } finally {
      client.close(force: true);
    }
  }
}
