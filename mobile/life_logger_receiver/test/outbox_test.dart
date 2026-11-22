import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:life_logger_receiver/life_logger_receiver.dart';

void main() {
  late Directory sandbox;
  late Directory root;
  late PassportProtocol protocol;

  setUp(() async {
    sandbox = await Directory.systemTemp.createTemp('passport-outbox-test-');
    root = Directory('${sandbox.path}/receiver');
    protocol = PassportProtocol(store: PassportStore(root: root));
  });

  tearDown(() async {
    if (await sandbox.exists()) {
      await sandbox.delete(recursive: true);
    }
  });

  test('outbox orders chunks before end and persists Mac acknowledgements',
      () async {
    final audio = <int>[1, 0, 2, 0];
    await protocol.handle(_chunk(index: 0, body: audio));
    await protocol.handle(_end(expectedChunks: 1));

    const destination = 'http://100.64.0.10:8766';
    var outbox = PassportSyncOutbox(root: root);
    final firstBatch = await outbox.pendingBatch(
      destination: destination,
      itemLimit: 1,
      byteLimit: 1024,
    );

    expect(firstBatch, hasLength(1));
    expect(firstBatch.single.kind, SyncItemKind.audio);
    expect(firstBatch.single.path, '/v1/audio/chunk');
    expect(firstBatch.single.body, audio);
    expect(
      firstBatch.single.headers['X-LifeLogger-Checksum'],
      Crc32.header(audio),
    );

    await outbox.acknowledge(firstBatch.single, destination: destination);
    outbox = PassportSyncOutbox(root: root);
    final secondBatch = await outbox.pendingBatch(
      destination: destination,
      itemLimit: 1,
      byteLimit: 1024,
    );

    expect(secondBatch, hasLength(1));
    expect(secondBatch.single.kind, SyncItemKind.end);
    expect(secondBatch.single.path, '/v1/audio/end');
    await outbox.acknowledge(secondBatch.single, destination: destination);
    expect(await outbox.pendingBatch(destination: destination), isEmpty);
  });

  test('outbox carries event names and timestamps to Mac receiver', () async {
    await protocol.handle(_chunk(index: 0, body: <int>[1, 0]));
    await protocol.handle(
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        headers: <String, String>{
          ..._headers(index: 0, body: const <int>[]),
          'X-LifeLogger-Type': 'event',
          'X-LifeLogger-Event': 'tap',
          'X-LifeLogger-Event-Time-Ms': '7654',
        },
      ),
    );

    final items = await PassportSyncOutbox(root: root).pendingBatch(
      destination: 'http://mac.local:8766',
      itemLimit: 10,
      byteLimit: 1024,
    );
    final event = items.singleWhere((item) => item.kind == SyncItemKind.event);

    expect(event.path, '/v1/audio/chunk');
    expect(event.headers['X-LifeLogger-Type'], 'event');
    expect(event.headers['X-LifeLogger-Event'], 'tap');
    expect(event.headers['X-LifeLogger-Event-Time-Ms'], '7654');
    expect(event.body, isEmpty);
  });

  test('outbox honors item and byte bounds and leaves remaining work queued',
      () async {
    final audio = List<int>.filled(16, 1);
    await protocol.handle(_chunk(index: 0, body: audio));
    await protocol.handle(_chunk(index: 1, body: audio));
    final outbox = PassportSyncOutbox(root: root);

    final batch = await outbox.pendingBatch(
      destination: 'http://mac.local:8766',
      itemLimit: 2,
      byteLimit: 16,
    );

    expect(batch, hasLength(1));
    expect(batch.single.body, hasLength(16));
    expect(
      await outbox.pendingBatch(destination: 'http://mac.local:8766'),
      hasLength(2),
    );
  });

  PassportRequest _chunk({required int index, required List<int> body}) =>
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        headers: _headers(index: index, body: body),
        body: body,
      );

  PassportRequest _end({required int expectedChunks}) => PassportRequest(
        method: 'POST',
        path: '/v1/audio/end',
        headers: _headers(index: expectedChunks, body: const <int>[]),
      );

  Map<String, String> _headers({
    required int index,
    required List<int> body,
  }) =>
      <String, String>{
        'X-LifeLogger-Session': 'session-1',
        'X-LifeLogger-Device': 'passport-01',
        'X-LifeLogger-Chunk': '$index',
        'X-LifeLogger-Checksum': Crc32.header(body),
        'X-LifeLogger-Sample-Rate': '16000',
        'X-LifeLogger-Format': 's16le',
      };
}
