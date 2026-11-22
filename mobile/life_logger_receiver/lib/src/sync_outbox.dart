import 'dart:convert';
import 'dart:io';

import 'crc32.dart';
import 'models.dart';

class PassportSyncOutbox {
  PassportSyncOutbox({required this.root});

  final Directory root;

  Future<List<SyncItem>> pendingBatch({
    required String destination,
    int itemLimit = 20,
    int byteLimit = 8 * 1024 * 1024,
    int maxSingleItemBytes = 32 * 1024 * 1024,
  }) async {
    if (itemLimit <= 0 || byteLimit <= 0) return <SyncItem>[];
    if (!await root.exists()) return <SyncItem>[];

    final devices = await root
        .list(followLinks: false)
        .where((entity) => entity is Directory)
        .cast<Directory>()
        .toList();
    devices.sort((a, b) => a.path.compareTo(b.path));
    final batch = <SyncItem>[];
    var bodyBytes = 0;

    for (final deviceDirectory in devices) {
      final device = _basename(deviceDirectory.path);
      final sessions = await deviceDirectory
          .list(followLinks: false)
          .where((entity) => entity is Directory)
          .cast<Directory>()
          .toList();
      sessions.sort((a, b) => a.path.compareTo(b.path));

      for (final sessionDirectory in sessions) {
        final session = _basename(sessionDirectory.path);
        final acknowledged = await _readAcknowledged(
          sessionDirectory,
          destination,
        );
        final candidates = await _sessionCandidates(
          device,
          session,
          deviceDirectory,
          sessionDirectory,
        );

        for (final candidate in candidates) {
          if (acknowledged.contains(candidate.id)) continue;
          if (batch.length >= itemLimit) return batch;

          final body = candidate.bodyFile == null
              ? const <int>[]
              : await candidate.bodyFile!.readAsBytes();
          if (body.length > maxSingleItemBytes) {
            throw FormatException(
              'Sync item ${candidate.id} exceeds the single-item limit',
            );
          }
          if (batch.isNotEmpty && bodyBytes + body.length > byteLimit) {
            return batch;
          }
          if (candidate.expectedChecksum != null &&
              Crc32.header(body) != candidate.expectedChecksum) {
            throw FormatException('Local checksum mismatch for ${candidate.id}');
          }

          batch.add(
            SyncItem(
              id: candidate.id,
              kind: candidate.kind,
              device: device,
              session: session,
              path: candidate.path,
              headers: candidate.headers,
              body: body,
            ),
          );
          bodyBytes += body.length;
        }
      }
    }
    return batch;
  }

  Future<void> acknowledge(
    SyncItem item, {
    required String destination,
  }) async {
    final sessionDirectory = Directory(
      '${root.path}/${item.device}/${item.session}',
    );
    await sessionDirectory.create(recursive: true);
    final stateFile = File('${sessionDirectory.path}/sync-state.json');
    Map<String, dynamic> state = <String, dynamic>{'destinations': <String, dynamic>{}};
    if (await stateFile.exists()) {
      final decoded = jsonDecode(await stateFile.readAsString());
      if (decoded is Map<String, dynamic>) state = decoded;
    }
    final destinations = state['destinations'] is Map<String, dynamic>
        ? Map<String, dynamic>.from(state['destinations'] as Map<String, dynamic>)
        : <String, dynamic>{};
    final stored = destinations[destination];
    final acknowledged = stored is List
        ? stored.whereType<String>().toSet()
        : <String>{};
    acknowledged.add(item.id);
    destinations[destination] = acknowledged.toList()..sort();
    state['destinations'] = destinations;
    await _writeJsonAtomic(stateFile, state);
  }

  Future<int> pendingCount({required String destination}) async {
    var count = 0;
    if (!await root.exists()) return count;
    final devices = await root
        .list(followLinks: false)
        .where((entity) => entity is Directory)
        .cast<Directory>()
        .toList();
    for (final deviceDirectory in devices) {
      final device = _basename(deviceDirectory.path);
      final sessions = await deviceDirectory
          .list(followLinks: false)
          .where((entity) => entity is Directory)
          .cast<Directory>()
          .toList();
      for (final sessionDirectory in sessions) {
        final session = _basename(sessionDirectory.path);
        final acknowledged = await _readAcknowledged(
          sessionDirectory,
          destination,
        );
        final candidates = await _sessionCandidates(
          device,
          session,
          deviceDirectory,
          sessionDirectory,
        );
        count += candidates.where((item) => !acknowledged.contains(item.id)).length;
      }
    }
    return count;
  }

  Future<List<_SyncCandidate>> _sessionCandidates(
    String device,
    String session,
    Directory deviceDirectory,
    Directory sessionDirectory,
  ) async {
    final result = <_SyncCandidate>[];
    final manifest = File('${sessionDirectory.path}/manifest.jsonl');
    final audioByChunk = <int, Map<String, dynamic>>{};
    var generation = '';
    if (await manifest.exists()) {
      for (final line in await manifest.readAsLines()) {
        final record = _decode(line);
        if (record == null || record['type'] != 'audio') continue;
        generation = '${record['received_at'] ?? generation}';
        if (record['end'] == true) continue;
        final chunk = record['chunk'];
        if (chunk is int) audioByChunk.putIfAbsent(chunk, () => record);
      }
    }

    final chunkIndices = audioByChunk.keys.toList()..sort();
    for (final chunk in chunkIndices) {
      final record = audioByChunk[chunk]!;
      final bodyFile = File(
        '${sessionDirectory.path}/chunk-${chunk.toString().padLeft(8, '0')}.s16le',
      );
      if (!await bodyFile.exists()) {
        throw FormatException('Local audio chunk $chunk is missing');
      }
      final headers = <String, String>{
        'X-LifeLogger-Session': session,
        'X-LifeLogger-Device': device,
        'X-LifeLogger-Chunk': '$chunk',
        'X-LifeLogger-Checksum': '${record['checksum'] ?? ''}',
        'X-LifeLogger-Sample-Rate': '${record['sample_rate'] ?? 16000}',
        'X-LifeLogger-Format': '${record['format'] ?? 's16le'}',
        'X-LifeLogger-Chunk-Start-Ms': '${record['start_ms'] ?? ''}',
      };
      result.add(
        _SyncCandidate(
          id: 'audio:$chunk',
          kind: SyncItemKind.audio,
          path: '/v1/audio/chunk',
          headers: headers,
          bodyFile: bodyFile,
          expectedChecksum: headers['X-LifeLogger-Checksum'],
        ),
      );
    }

    final events = File('${deviceDirectory.path}/events.jsonl');
    if (await events.exists()) {
      final lines = await events.readAsLines();
      for (var index = 0; index < lines.length; index++) {
        final record = _decode(lines[index]);
        if (record == null || record['session'] != session) continue;
        final event = '${record['event'] ?? 'event'}';
        final eventTime = '${record['event_time_ms'] ?? ''}';
        result.add(
          _SyncCandidate(
            id: 'event:$index',
            kind: SyncItemKind.event,
            path: '/v1/audio/chunk',
            headers: <String, String>{
              'X-LifeLogger-Session': session,
              'X-LifeLogger-Device': device,
              'X-LifeLogger-Chunk': '0',
              'X-LifeLogger-Checksum': Crc32.header(const <int>[]),
              'X-LifeLogger-Type': 'event',
              'X-LifeLogger-Event': event,
              'X-LifeLogger-Event-Time-Ms': eventTime,
            },
          ),
        );
      }
    }

    final summaryFile = File('${sessionDirectory.path}/summary.json');
    if (await summaryFile.exists()) {
      final summary = _decode(await summaryFile.readAsString());
      final expected = summary?['expected_chunks'];
      if (expected is int && summary?['event_only'] != true) {
        result.add(
          _SyncCandidate(
            id: 'end:$expected:$generation',
            kind: SyncItemKind.end,
            path: '/v1/audio/end',
            headers: <String, String>{
              'X-LifeLogger-Session': session,
              'X-LifeLogger-Device': device,
              'X-LifeLogger-Chunk': '$expected',
              'X-LifeLogger-Checksum': Crc32.header(const <int>[]),
              'X-LifeLogger-Sample-Rate': '${summary?['sample_rate'] ?? 16000}',
              'X-LifeLogger-Format': '${summary?['format'] ?? 's16le'}',
              'X-LifeLogger-End': '1',
            },
          ),
        );
      }
    }
    return result;
  }

  Future<Set<String>> _readAcknowledged(
    Directory sessionDirectory,
    String destination,
  ) async {
    final file = File('${sessionDirectory.path}/sync-state.json');
    if (!await file.exists()) return <String>{};
    final decoded = _decode(await file.readAsString());
    final destinations = decoded?['destinations'];
    if (destinations is! Map<String, dynamic>) return <String>{};
    final items = destinations[destination];
    return items is List ? items.whereType<String>().toSet() : <String>{};
  }

  Map<String, dynamic>? _decode(String value) {
    try {
      final decoded = jsonDecode(value);
      return decoded is Map<String, dynamic> ? decoded : null;
    } on FormatException {
      return null;
    }
  }

  Future<void> _writeJsonAtomic(File target, Map<String, dynamic> value) async {
    await target.parent.create(recursive: true);
    final temporary = File('${target.path}.tmp');
    final encoded = const JsonEncoder.withIndent('  ').convert(value);
    await temporary.writeAsString(
      '$encoded\n',
      flush: true,
    );
    if (await target.exists()) await target.delete();
    await temporary.rename(target.path);
  }

  String _basename(String path) => path.split(Platform.pathSeparator).last;
}

class _SyncCandidate {
  const _SyncCandidate({
    required this.id,
    required this.kind,
    required this.path,
    required this.headers,
    this.bodyFile,
    this.expectedChecksum,
  });

  final String id;
  final SyncItemKind kind;
  final String path;
  final Map<String, String> headers;
  final File? bodyFile;
  final String? expectedChecksum;
}
