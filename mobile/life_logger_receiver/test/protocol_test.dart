import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';
import 'package:life_logger_receiver/life_logger_receiver.dart';

void main() {
  late Directory sandbox;
  late Directory root;
  late PassportProtocol protocol;

  setUp(() async {
    sandbox = await Directory.systemTemp.createTemp('passport-receiver-test-');
    root = Directory('${sandbox.path}/receiver');
    protocol = PassportProtocol(store: PassportStore(root: root));
  });

  tearDown(() async {
    if (await sandbox.exists()) {
      await sandbox.delete(recursive: true);
    }
  });

  test('CRC32 uses the firmware header representation', () {
    final bytes = utf8.encode('123456789');
    expect(Crc32.checksum(bytes), 0xcbf43926);
    expect(Crc32.header(bytes), 'crc32:cbf43926');
    expect(Crc32.header(const <int>[]), 'crc32:00000000');
  });

  test('discovery echoes the eight-character nonce and receiver port', () {
    expect(
      PassportDiscovery.response(
        utf8.encode('LL_DISCOVER_V1 a1B2c3D4'),
        receiverPort: 8766,
      ),
      'LL_RECEIVER_V1 a1B2c3D4 8766',
    );
    expect(
      PassportDiscovery.response(
        utf8.encode('LL_DISCOVER_V1 invalid'),
        receiverPort: 8766,
      ),
      isNull,
    );
  });

  test('health response exposes the receiver contract', () async {
    final response = await protocol.handle(
      const PassportRequest(method: 'GET', path: '/health'),
    );

    expect(response.statusCode, 200);
    expect(response.json, {
      'ok': true,
      'service': 'LifeLoggerReceiver',
      'version': 1,
      'port': 8766,
    });
  });

  test('a chunk is durably written before the receiver acknowledges it', () async {
    final pcm = <int>[1, 0, 2, 0];
    final response = await protocol.handle(audioRequest(index: 0, body: pcm));

    expect(response.statusCode, 200);
    expect(response.json['checksum_ok'], true);
    expect(
      await File('${root.path}/passport-01/session-1/chunk-00000000.s16le')
          .readAsBytes(),
      pcm,
    );
  });

  test('checksum mismatch is rejected', () async {
    final request = audioRequest(index: 0, body: <int>[1, 0]).copyWith(
      headers: <String, String>{'X-LifeLogger-Checksum': 'crc32:00000000'},
    );

    final response = await protocol.handle(request);

    expect(response.statusCode, 422);
    expect(response.json['error'], 'checksum mismatch');
  });

  test('end reports missing, duplicate, and out-of-order chunks', () async {
    final pcm = <int>[1, 0];
    expect((await protocol.handle(audioRequest(index: 1, body: pcm))).statusCode, 200);
    expect((await protocol.handle(audioRequest(index: 0, body: pcm))).statusCode, 200);
    expect((await protocol.handle(audioRequest(index: 0, body: pcm))).statusCode, 200);

    final response = await protocol.handle(endRequest(expectedChunks: 3));
    final summary = response.json['summary'] as Map<String, dynamic>;

    expect(summary['missing_chunks'], <int>[2]);
    expect(summary['duplicate_chunks'], 1);
    expect(summary['out_of_order_chunks'], 1);
    expect(summary['complete'], false);
  });

  test('conflicting duplicate chunk is rejected', () async {
    expect(
      (await protocol.handle(audioRequest(index: 0, body: <int>[1, 0])))
          .statusCode,
      200,
    );

    final response =
        await protocol.handle(audioRequest(index: 0, body: <int>[2, 0]));

    expect(response.statusCode, 409);
  });

  test('complete end assembles 16 kHz mono 16-bit WAV', () async {
    final first = <int>[1, 0, 2, 0];
    final second = <int>[3, 0, 4, 0];
    await protocol.handle(audioRequest(index: 0, body: first));
    await protocol.handle(audioRequest(index: 1, body: second));

    final response = await protocol.handle(endRequest(expectedChunks: 2));
    final wav = await File('${root.path}/passport-01/session-1/recording.wav')
        .readAsBytes();

    expect(response.statusCode, 200);
    expect(ascii.decode(wav.sublist(0, 4)), 'RIFF');
    expect(ascii.decode(wav.sublist(8, 12)), 'WAVE');
    expect(wav[22], 1);
    expect(wav[23], 0);
    expect(wav.sublist(24, 28), <int>[0x80, 0x3e, 0, 0]);
    expect(wav.sublist(44), <int>[...first, ...second]);
    expect((response.json['summary'] as Map<String, dynamic>)['complete'], true);
  });

  test('event marker timestamp is persisted without an audio body', () async {
    final headers = <String, String>{
      ...baseHeaders(index: 0, body: const <int>[]),
      'X-LifeLogger-Type': 'event',
      'X-LifeLogger-Event': 'tap',
      'X-LifeLogger-Event-Time-Ms': '12345',
    };

    final response = await protocol.handle(
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        headers: headers,
      ),
    );
    final contents = await File('${root.path}/passport-01/events.jsonl')
        .readAsString();

    expect(response.statusCode, 200);
    expect(contents, contains('"event_time_ms":"12345"'));
  });

  PassportRequest audioRequest({required int index, required List<int> body}) =>
      PassportRequest(
        method: 'POST',
        path: '/v1/audio/chunk',
        headers: baseHeaders(index: index, body: body),
        body: body,
      );

  PassportRequest endRequest({required int expectedChunks}) => PassportRequest(
        method: 'POST',
        path: '/v1/audio/end',
        headers: baseHeaders(index: expectedChunks, body: const <int>[]),
      );

  Map<String, String> baseHeaders({
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
