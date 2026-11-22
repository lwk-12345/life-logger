import 'dart:convert';

class PassportRequest {
  const PassportRequest({
    required this.method,
    required this.path,
    this.headers = const <String, String>{},
    this.body = const <int>[],
  });

  final String method;
  final String path;
  final Map<String, String> headers;
  final List<int> body;

  PassportRequest copyWith({
    String? method,
    String? path,
    Map<String, String>? headers,
    List<int>? body,
  }) =>
      PassportRequest(
        method: method ?? this.method,
        path: path ?? this.path,
        headers: headers ?? this.headers,
        body: body ?? this.body,
      );

  String? header(String name) {
    final key = name.toLowerCase();
    for (final entry in headers.entries) {
      if (entry.key.toLowerCase() == key) return entry.value;
    }
    return null;
  }
}

class PassportResponse {
  const PassportResponse({required this.statusCode, required this.payload});

  final int statusCode;
  final Map<String, dynamic> payload;

  Map<String, dynamic> get json => payload;

  List<int> encode() => utf8.encode(jsonEncode(payload));
}

class StoredResult {
  const StoredResult({required this.record, this.summary});

  final Map<String, dynamic> record;
  final Map<String, dynamic>? summary;
}

enum SyncItemKind { audio, event, end }

class SyncItem {
  const SyncItem({
    required this.id,
    required this.kind,
    required this.device,
    required this.session,
    required this.path,
    required this.headers,
    required this.body,
  });

  final String id;
  final SyncItemKind kind;
  final String device;
  final String session;
  final String path;
  final Map<String, String> headers;
  final List<int> body;
}

class SyncReport {
  const SyncReport({
    required this.acknowledged,
    required this.queued,
    required this.route,
    this.error,
  });

  final int acknowledged;
  final int queued;
  final String? route;
  final String? error;
}
