import 'dart:async';
import 'dart:convert';
import 'dart:io';
import 'dart:typed_data';

import 'crc32.dart';
import 'models.dart';

class PassportStoreError implements Exception {
  const PassportStoreError(this.message, {this.code});

  final String message;
  final String? code;

  @override
  String toString() => message;
}

class PassportStore {
  PassportStore({required this.root});

  final Directory root;
  final Map<String, _SessionState> _states = <String, _SessionState>{};
  Future<void> _tail = Future<void>.value();

  Future<StoredResult> receiveAudioChunk({
    required String device,
    required String session,
    required int chunk,
    required List<int> body,
    required String checksum,
    required int sampleRate,
    required String format,
    required String startMilliseconds,
  }) =>
      _serialized(() async {
        final directory = await _sessionDirectory(device, session);
        final state = await _stateFor(device, session, directory);
        final previous = state.seen[chunk];
        if (previous != null && previous != checksum) {
          throw const PassportStoreError(
            'duplicate chunk has different checksum',
            code: 'conflicting_duplicate',
          );
        }

        final duplicate = previous != null;
        final outOfOrder = !duplicate && chunk != state.nextExpected;
        final receivedAt = DateTime.now().toUtc().toIso8601String();
        final target = File(
          '${directory.path}/chunk-${chunk.toString().padLeft(8, '0')}.s16le',
        );

        // Write PCM before its manifest row and before returning the HTTP ACK.
        if (!duplicate || !await target.exists()) {
          await _writeAtomic(target, body);
        }

        final record = <String, dynamic>{
          'capture_source': 'ai-passport-wifi',
          'device': device,
          'session': session,
          'type': 'audio',
          'chunk': chunk,
          'bytes': body.length,
          'checksum': checksum,
          'checksum_ok': true,
          'sample_rate': sampleRate,
          'format': format,
          'start_ms': startMilliseconds,
          'end': false,
          'duplicate': duplicate,
          'out_of_order': outOfOrder,
          'received_at': receivedAt,
        };
        await _appendJsonLine(File('${directory.path}/manifest.jsonl'), record);

        if (duplicate) {
          state.duplicates++;
        } else {
          state.seen[chunk] = checksum;
          if (outOfOrder) state.outOfOrder++;
          while (state.seen.containsKey(state.nextExpected)) {
            state.nextExpected++;
          }
        }
        _states[_sessionKey(device, session)] = state;
        return StoredResult(record: record);
      });

  Future<StoredResult> receiveEvent({
    required String device,
    required String session,
    required String event,
    required String eventTimeMilliseconds,
    required String checksum,
  }) =>
      _serialized(() async {
        final deviceDirectory = Directory('${root.path}/$device');
        await deviceDirectory.create(recursive: true);
        final sessionDirectory = await _sessionDirectory(device, session);
        final receivedAt = DateTime.now().toUtc().toIso8601String();
        final record = <String, dynamic>{
          'capture_source': 'ai-passport-wifi',
          'device': device,
          'session': session,
          'type': 'event',
          'event': event,
          'event_time_ms': eventTimeMilliseconds,
          'checksum': checksum,
          'checksum_ok': true,
          'received_at': receivedAt,
        };
        await _appendJsonLine(File('${deviceDirectory.path}/events.jsonl'), record);

        final records = await _manifestRecords(sessionDirectory);
        final hasAudio = records.any(
          (item) => item['type'] == 'audio' && item['end'] != true,
        );
        if (!hasAudio) {
          await _writeJsonAtomic(
            File('${sessionDirectory.path}/summary.json'),
            <String, dynamic>{
              'schema_version': 'ai-passport-stream-session-v1',
              'capture_source': 'ai-passport-wifi',
              'device': device,
              'device_label': 'AI Passport',
              'session': session,
              'event_only': true,
              'complete': false,
              'updated_at': receivedAt,
            },
          );
        }
        return StoredResult(record: record);
      });

  Future<StoredResult> finishAudio({
    required String device,
    required String session,
    required int expectedChunks,
    required int sampleRate,
    required String format,
    required String checksum,
  }) =>
      _serialized(() async {
        if (expectedChunks < 0 || expectedChunks > 1_000_000) {
          throw const PassportStoreError(
            'expected chunk count out of range',
            code: 'invalid_chunk_count',
          );
        }
        final directory = await _sessionDirectory(device, session);
        final state = await _stateFor(device, session, directory);
        final summary = state.summary(expectedChunks);
        final receivedAt = DateTime.now().toUtc().toIso8601String();
        final record = <String, dynamic>{
          'capture_source': 'ai-passport-wifi',
          'device': device,
          'session': session,
          'type': 'audio',
          'chunk': expectedChunks,
          'bytes': 0,
          'checksum': checksum,
          'checksum_ok': true,
          'sample_rate': sampleRate,
          'format': format,
          'start_ms': '',
          'end': true,
          'duplicate': false,
          'out_of_order': false,
          'received_at': receivedAt,
        };
        await _appendJsonLine(File('${directory.path}/manifest.jsonl'), record);

        final records = await _manifestRecords(directory);
        Object? firstReceived;
        for (final item in records) {
          if (item['type'] == 'audio' && item['end'] != true) {
            firstReceived = item['received_at'];
            break;
          }
        }
        final summaryPayload = <String, dynamic>{
          'schema_version': 'ai-passport-stream-session-v1',
          'capture_source': 'ai-passport-wifi',
          'device': device,
          'device_label': 'AI Passport',
          'session': session,
          'sample_rate': sampleRate,
          'format': format,
          'received_started_at': firstReceived,
          'updated_at': receivedAt,
          ...summary,
        };

        if (summary['complete'] == true) {
          try {
            final wav = await _assembleWav(directory, expectedChunks, sampleRate);
            if (wav != null) summaryPayload['recording_path'] = wav.path;
          } on Object catch (error) {
            summaryPayload['complete'] = false;
            summaryPayload['assembly_error'] = error.toString();
          }
        }
        await _writeJsonAtomic(
          File('${directory.path}/summary.json'),
          summaryPayload,
        );
        return StoredResult(record: record, summary: summaryPayload);
      });

  Future<List<Map<String, dynamic>>> readManifest(String device, String session) async {
    final directory = Directory('${root.path}/$device/$session');
    return _manifestRecords(directory);
  }

  Future<Map<String, dynamic>?> readSummary(String device, String session) async {
    final file = File('${root.path}/$device/$session/summary.json');
    if (!await file.exists()) return null;
    final decoded = jsonDecode(await file.readAsString());
    return decoded is Map<String, dynamic> ? decoded : null;
  }

  Future<List<Map<String, dynamic>>> readEvents(String device) async {
    final file = File('${root.path}/$device/events.jsonl');
    if (!await file.exists()) return <Map<String, dynamic>>[];
    final lines = await file.readAsLines();
    return lines.map(_decodeRecord).whereType<Map<String, dynamic>>().toList();
  }

  Future<T> _serialized<T>(Future<T> Function() action) async {
    final previous = _tail;
    final release = Completer<void>();
    _tail = release.future;
    await previous;
    try {
      return await action();
    } finally {
      release.complete();
    }
  }

  Future<Directory> _sessionDirectory(String device, String session) async {
    final directory = Directory('${root.path}/$device/$session');
    await directory.create(recursive: true);
    return directory;
  }

  Future<_SessionState> _stateFor(
    String device,
    String session,
    Directory directory,
  ) async {
    final key = _sessionKey(device, session);
    final cached = _states[key];
    if (cached != null) return cached;

    final state = _SessionState();
    for (final record in await _manifestRecords(directory)) {
      if (record['type'] != 'audio' || record['end'] == true) continue;
      final index = record['chunk'];
      final checksum = record['checksum'];
      if (index is! int || checksum is! String) continue;
      final old = state.seen[index];
      if (old != null) {
        if (old == checksum) state.duplicates++;
      } else {
        state.seen[index] = checksum;
      }
    }
    while (state.seen.containsKey(state.nextExpected)) {
      state.nextExpected++;
    }
    _states[key] = state;
    return state;
  }

  Future<List<Map<String, dynamic>>> _manifestRecords(Directory directory) async {
    final file = File('${directory.path}/manifest.jsonl');
    if (!await file.exists()) return <Map<String, dynamic>>[];
    return (await file.readAsLines())
        .map(_decodeRecord)
        .whereType<Map<String, dynamic>>()
        .toList();
  }

  Map<String, dynamic>? _decodeRecord(String line) {
    try {
      final decoded = jsonDecode(line);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      return null;
    }
  }

  Future<void> _appendJsonLine(File file, Map<String, dynamic> payload) async {
    await file.parent.create(recursive: true);
    final handle = await file.open(mode: FileMode.append);
    try {
      await handle.writeString('${jsonEncode(payload)}\n');
      await handle.flush();
    } finally {
      await handle.close();
    }
  }

  Future<void> _writeJsonAtomic(File target, Map<String, dynamic> payload) async {
    final encoded = const JsonEncoder.withIndent('  ').convert(payload);
    await _writeAtomic(target, utf8.encode('$encoded\n'));
  }

  Future<void> _writeAtomic(File target, List<int> bytes) async {
    await target.parent.create(recursive: true);
    final temporary = File('${target.path}.tmp');
    await temporary.writeAsBytes(bytes, flush: true);
    if (await target.exists()) await target.delete();
    await temporary.rename(target.path);
  }

  Future<File?> _assembleWav(
    Directory directory,
    int chunkCount,
    int sampleRate,
  ) async {
    if (chunkCount == 0) return null;
    if (sampleRate <= 0 || sampleRate > 0x7fffffff) {
      throw const PassportStoreError('sample rate out of WAV range');
    }

    final chunks = <File>[];
    var dataLength = 0;
    for (var index = 0; index < chunkCount; index++) {
      final file = File(
        '${directory.path}/chunk-${index.toString().padLeft(8, '0')}.s16le',
      );
      if (!await file.exists()) {
        throw PassportStoreError('missing audio chunk $index');
      }
      final size = await file.length();
      if (size == 0 || size.isOdd) {
        throw PassportStoreError('invalid audio chunk $index');
      }
      dataLength += size;
      if (dataLength > 0xffffffff - 36) {
        throw const PassportStoreError('WAV exceeds RIFF size limit');
      }
      chunks.add(file);
    }

    final target = File('${directory.path}/recording.wav');
    final temporary = File('${target.path}.tmp');
    final output = await temporary.open(mode: FileMode.write);
    try {
      await output.writeFrom(_wavHeader(sampleRate, dataLength));
      for (final chunk in chunks) {
        await for (final block in chunk.openRead()) {
          await output.writeFrom(block);
        }
      }
      await output.flush();
    } finally {
      await output.close();
    }
    if (await target.exists()) await target.delete();
    return temporary.rename(target.path);
  }

  Uint8List _wavHeader(int sampleRate, int dataLength) {
    final bytes = ByteData(44);
    void tag(int offset, String value) {
      for (var index = 0; index < value.length; index++) {
        bytes.setUint8(offset + index, value.codeUnitAt(index));
      }
    }

    tag(0, 'RIFF');
    bytes.setUint32(4, 36 + dataLength, Endian.little);
    tag(8, 'WAVE');
    tag(12, 'fmt ');
    bytes.setUint32(16, 16, Endian.little);
    bytes.setUint16(20, 1, Endian.little);
    bytes.setUint16(22, 1, Endian.little);
    bytes.setUint32(24, sampleRate, Endian.little);
    bytes.setUint32(28, sampleRate * 2, Endian.little);
    bytes.setUint16(32, 2, Endian.little);
    bytes.setUint16(34, 16, Endian.little);
    tag(36, 'data');
    bytes.setUint32(40, dataLength, Endian.little);
    return bytes.buffer.asUint8List();
  }

  String _sessionKey(String device, String session) => '$device\u0000$session';
}

class _SessionState {
  final Map<int, String> seen = <int, String>{};
  int nextExpected = 0;
  int duplicates = 0;
  int outOfOrder = 0;

  Map<String, dynamic> summary(int expectedCount) {
    final missing = <int>[];
    for (var index = 0; index < expectedCount; index++) {
      if (!seen.containsKey(index)) missing.add(index);
    }
    final unexpected = seen.keys.where((index) => index >= expectedCount).toList()
      ..sort();
    return <String, dynamic>{
      'expected_chunks': expectedCount,
      'received_unique_chunks': seen.length,
      'missing_chunks': missing,
      'unexpected_chunks': unexpected,
      'duplicate_chunks': duplicates,
      'out_of_order_chunks': outOfOrder,
      'complete': missing.isEmpty && unexpected.isEmpty,
    };
  }
}

String safeIdentifier(String value, {required String fallback}) {
  var cleaned = value
      .replaceAll(RegExp(r'[^A-Za-z0-9_.-]+'), '_')
      .replaceAll(RegExp(r'^[._]+|[._]+$'), '');
  if (cleaned.length > 80) cleaned = cleaned.substring(0, 80);
  return cleaned.isEmpty ? fallback : cleaned;
}
