import 'crc32.dart';
import 'models.dart';
import 'passport_store.dart';

class PassportProtocol {
  const PassportProtocol({required this.store});

  final PassportStore store;

  Future<PassportResponse> handle(PassportRequest request) async {
    if (request.method.toUpperCase() == 'GET') {
      if (request.path != '/health') {
        return _response(404, <String, dynamic>{'error': 'not found'});
      }
      return _response(200, <String, dynamic>{
        'ok': true,
        'service': 'LifeLoggerReceiver',
        'version': 1,
        'port': 8766,
      });
    }

    if (request.method.toUpperCase() != 'POST' ||
        (request.path != '/v1/audio/chunk' && request.path != '/v1/audio/end')) {
      return _response(404, <String, dynamic>{'error': 'not found'});
    }

    final session = safeIdentifier(
      request.header('X-LifeLogger-Session') ?? '',
      fallback: 'unknown',
    );
    final device = safeIdentifier(
      request.header('X-LifeLogger-Device') ?? '',
      fallback: 'device',
    );
    final chunkText = request.header('X-LifeLogger-Chunk') ?? '0';
    final chunk = int.tryParse(chunkText);
    if (chunk == null) {
      return _response(400, <String, dynamic>{'error': 'invalid stream headers'});
    }
    if (chunk < 0 || chunk > 1_000_000) {
      return _response(400, <String, dynamic>{'error': 'invalid stream counters'});
    }

    final checksum = (request.header('X-LifeLogger-Checksum') ?? '').toLowerCase();
    if (checksum != Crc32.header(request.body)) {
      return _response(422, <String, dynamic>{'error': 'checksum mismatch'});
    }

    final streamType = (request.header('X-LifeLogger-Type') ?? 'audio').toLowerCase();
    if (streamType == 'event') {
      if (request.body.isNotEmpty) {
        return _response(400, <String, dynamic>{'error': 'event body must be empty'});
      }
      try {
        final record = await store.receiveEvent(
          device: device,
          session: session,
          event: safeIdentifier(
            request.header('X-LifeLogger-Event') ?? '',
            fallback: 'event',
          ),
          eventTimeMilliseconds:
              request.header('X-LifeLogger-Event-Time-Ms') ?? '',
          checksum: checksum,
        );
        return _response(200, <String, dynamic>{'ok': true, ...record.record});
      } on Object {
        return _response(500, <String, dynamic>{'error': 'storage failure'});
      }
    }

    if (streamType != 'audio') {
      return _response(400, <String, dynamic>{'error': 'unsupported stream type'});
    }
    final sampleRate = int.tryParse(
      request.header('X-LifeLogger-Sample-Rate') ?? '0',
    );
    if (sampleRate == null) {
      return _response(400, <String, dynamic>{'error': 'invalid sample rate'});
    }
    final format = request.header('X-LifeLogger-Format') ?? '';
    final isEnd = request.path == '/v1/audio/end' ||
        request.header('X-LifeLogger-End') == '1';

    if (isEnd && request.body.isNotEmpty) {
      return _response(
        400,
        <String, dynamic>{'error': 'end marker must have an empty body'},
      );
    }
    if (!isEnd && (request.body.isEmpty || request.body.length.isOdd)) {
      return _response(400, <String, dynamic>{'error': 'invalid PCM chunk size'});
    }
    if (format != 's16le' || sampleRate <= 0) {
      return _response(400, <String, dynamic>{'error': 'unsupported audio format'});
    }

    try {
      final result = isEnd
          ? await store.finishAudio(
              device: device,
              session: session,
              expectedChunks: chunk,
              sampleRate: sampleRate,
              format: format,
              checksum: checksum,
            )
          : await store.receiveAudioChunk(
              device: device,
              session: session,
              chunk: chunk,
              body: request.body,
              checksum: checksum,
              sampleRate: sampleRate,
              format: format,
              startMilliseconds:
                  request.header('X-LifeLogger-Chunk-Start-Ms') ?? '',
            );
      return _response(200, <String, dynamic>{
        'ok': true,
        ...result.record,
        if (result.summary != null) 'summary': result.summary,
      });
    } on PassportStoreError catch (error) {
      switch (error.code) {
        case 'conflicting_duplicate':
          return _response(409, <String, dynamic>{'error': error.message});
        case 'invalid_chunk_count':
          return _response(400, <String, dynamic>{'error': error.message});
        default:
          return _response(500, <String, dynamic>{'error': 'storage failure'});
      }
    } on Object {
      return _response(500, <String, dynamic>{'error': 'storage failure'});
    }
  }

  PassportResponse _response(int status, Map<String, dynamic> payload) =>
      PassportResponse(statusCode: status, payload: payload);
}
