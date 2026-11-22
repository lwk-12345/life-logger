import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'models.dart';
import 'passport_protocol.dart';

enum ReceiverPhase { stopped, starting, listening, failed }

class ReceiverStatus {
  const ReceiverStatus({
    required this.phase,
    this.message = '',
    this.lastPeer,
    this.lastRequestAt,
    this.lastResponse,
  });

  final ReceiverPhase phase;
  final String message;
  final String? lastPeer;
  final DateTime? lastRequestAt;
  final PassportResponse? lastResponse;
}

class PassportReceiverServer {
  PassportReceiverServer({
    required this.protocol,
    this.onRequest,
  });

  final PassportProtocol protocol;
  final void Function(PassportRequest, PassportResponse, String? peer)? onRequest;
  final StreamController<ReceiverStatus> _statusController =
      StreamController<ReceiverStatus>.broadcast();

  HttpServer? _httpServer;
  RawDatagramSocket? _discoverySocket;
  ReceiverStatus _status = const ReceiverStatus(phase: ReceiverPhase.stopped);

  Stream<ReceiverStatus> get statuses => _statusController.stream;
  ReceiverStatus get status => _status;

  Future<void> start() async {
    if (_status.phase == ReceiverPhase.starting ||
        _status.phase == ReceiverPhase.listening) {
      return;
    }
    _setStatus(const ReceiverStatus(
      phase: ReceiverPhase.starting,
      message: '正在打开局域网接收器…',
    ));
    try {
      final http = await HttpServer.bind(InternetAddress.anyIPv4, 8766);
      _httpServer = http;
      final discovery = await RawDatagramSocket.bind(
        InternetAddress.anyIPv4,
        8767,
        reuseAddress: true,
      );
      _discoverySocket = discovery;
      _listenForDiscovery(discovery);
      http.listen(_handleHttpRequest, onError: _onServerError);
      _setStatus(const ReceiverStatus(
        phase: ReceiverPhase.listening,
        message: 'HTTP 8766 与 UDP 8767 已监听；等待 AI Passport。',
      ));
    } on Object catch (error) {
      await stop();
      _setStatus(ReceiverStatus(
        phase: ReceiverPhase.failed,
        message: '接收器启动失败：$error',
      ));
      rethrow;
    }
  }

  Future<void> stop() async {
    final http = _httpServer;
    final discovery = _discoverySocket;
    _httpServer = null;
    _discoverySocket = null;
    discovery?.close();
    await http?.close(force: true);
    _setStatus(const ReceiverStatus(
      phase: ReceiverPhase.stopped,
      message: '接收器已停止。',
    ));
  }

  Future<void> dispose() async {
    await stop();
    await _statusController.close();
  }

  void _listenForDiscovery(RawDatagramSocket socket) {
    socket.listen(
      (event) {
        if (event != RawSocketEvent.read) return;
        Datagram? packet;
        while ((packet = socket.receive()) != null) {
          final datagram = packet!;
          final reply = PassportDiscovery.response(
            datagram.data,
            receiverPort: 8766,
          );
          if (reply != null) {
            socket.send(utf8.encode(reply), datagram.address, datagram.port);
          }
        }
      },
      onError: _onServerError,
    );
  }

  Future<void> _handleHttpRequest(HttpRequest incoming) async {
    final peer = incoming.connectionInfo?.remoteAddress.address;
    try {
      if (incoming.contentLength > 32 * 1024 * 1024) {
        await _writeResponse(incoming.response, 413, <String, dynamic>{
          'error': 'request body exceeds receiver limit',
        });
        return;
      }
      final builder = BytesBuilder(copy: false);
      await for (final block in incoming) {
        if (builder.length + block.length > 32 * 1024 * 1024) {
          await _writeResponse(incoming.response, 413, <String, dynamic>{
            'error': 'request body exceeds receiver limit',
          });
          return;
        }
        builder.add(block);
      }
      final headers = <String, String>{};
      incoming.headers.forEach((name, values) {
        headers[name] = values.join(',');
      });
      final request = PassportRequest(
        method: incoming.method,
        path: incoming.uri.path,
        headers: headers,
        body: builder.takeBytes(),
      );
      final response = await protocol.handle(request);
      onRequest?.call(request, response, peer);
      await _writeResponse(incoming.response, response.statusCode, response.json);
      _setStatus(ReceiverStatus(
        phase: ReceiverPhase.listening,
        message: _statusMessage(request, response),
        lastPeer: peer,
        lastRequestAt: DateTime.now(),
        lastResponse: response,
      ));
    } on Object catch (error) {
      try {
        await _writeResponse(incoming.response, 500, <String, dynamic>{
          'error': 'receiver failure',
        });
      } on Object {
        // The peer may already have closed the connection.
      }
      _setStatus(ReceiverStatus(
        phase: ReceiverPhase.listening,
        message: '请求处理失败：$error',
        lastPeer: peer,
      ));
    }
  }

  Future<void> _writeResponse(
    HttpResponse response,
    int statusCode,
    Map<String, dynamic> payload,
  ) async {
    response.statusCode = statusCode;
    response.headers.contentType = ContentType.json;
    response.headers.set(HttpHeaders.cacheControlHeader, 'no-store');
    response.headers.set(HttpHeaders.connectionHeader, 'close');
    response.write(jsonEncode(payload));
    await response.close();
  }

  void _onServerError(Object error, [StackTrace? stack]) {
    _setStatus(ReceiverStatus(
      phase: ReceiverPhase.failed,
      message: '局域网接收异常：$error',
    ));
  }

  String _statusMessage(PassportRequest request, PassportResponse response) {
    if (response.statusCode < 200 || response.statusCode >= 300) {
      return 'Passport 请求被拒绝（HTTP ${response.statusCode}）。';
    }
    if (request.header('X-LifeLogger-Type')?.toLowerCase() == 'event') {
      return '已保存 Passport 事件标记。';
    }
    if (request.path == '/v1/audio/end' ||
        request.header('X-LifeLogger-End') == '1') {
      return '已收到音频结束标记并更新会话清单。';
    }
    if (request.path == '/v1/audio/chunk') return '正在接收并保存音频块。';
    return 'Passport 连接正常。';
  }

  void _setStatus(ReceiverStatus value) {
    _status = value;
    if (!_statusController.isClosed) _statusController.add(value);
  }
}
