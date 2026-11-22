import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:life_logger_receiver/life_logger_receiver.dart';

void main() {
  late Directory sandbox;
  late Directory root;
  late PassportProtocol protocol;

  setUp(() async {
    sandbox = await Directory.systemTemp.createTemp('passport-sync-client-test-');
    root = Directory('${sandbox.path}/receiver');
    protocol = PassportProtocol(store: PassportStore(root: root));
  });

  tearDown(() async {
    if (await sandbox.exists()) await sandbox.delete(recursive: true);
  });

  test('falls back from LAN to Tailscale and ACKs only the successful route',
      () async {
    await protocol.handle(
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        body: <int>[1, 0],
        headers: <String, String>{
          'X-LifeLogger-Session': 'session-1',
          'X-LifeLogger-Device': 'passport-01',
          'X-LifeLogger-Chunk': '0',
          'X-LifeLogger-Checksum': Crc32.header(<int>[1, 0]),
          'X-LifeLogger-Sample-Rate': '16000',
          'X-LifeLogger-Format': 's16le',
        },
      ),
    );
    final calls = <String>[];
    final client = MacSyncClient(
      outbox: PassportSyncOutbox(root: root),
      post: (endpoint, item) async {
        calls.add(endpoint.host);
        if (endpoint.host == '192.168.1.20') {
          throw const SocketException('LAN Mac unavailable');
        }
        return 200;
      },
    );

    final report = await client.syncOnce(
      directLanUrl: 'http://192.168.1.20:8766',
      tailscaleUrl: 'http://100.64.0.10:8766',
    );

    expect(calls, <String>['192.168.1.20', '100.64.0.10']);
    expect(report.acknowledged, 1);
    expect(report.queued, 0);
    expect(report.route, 'Tailscale');
  });

  test('non-success HTTP response leaves the item queued', () async {
    await protocol.handle(
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        body: <int>[1, 0],
        headers: <String, String>{
          'X-LifeLogger-Session': 'session-1',
          'X-LifeLogger-Device': 'passport-01',
          'X-LifeLogger-Chunk': '0',
          'X-LifeLogger-Checksum': Crc32.header(<int>[1, 0]),
          'X-LifeLogger-Sample-Rate': '16000',
          'X-LifeLogger-Format': 's16le',
        },
      ),
    );
    var calls = 0;
    final client = MacSyncClient(
      outbox: PassportSyncOutbox(root: root),
      post: (endpoint, item) async {
        calls++;
        return 500;
      },
    );

    final report = await client.syncOnce(
      directLanUrl: 'http://192.168.1.20:8766',
      tailscaleUrl: 'http://100.64.0.10:8766',
    );

    expect(calls, 1);
    expect(report.acknowledged, 0);
    expect(report.queued, 1);
    expect(report.error, contains('HTTP 500'));
  });
}
