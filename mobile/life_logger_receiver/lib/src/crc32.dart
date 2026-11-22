class Crc32 {
  Crc32._();

  static final List<int> _table = List<int>.generate(256, (value) {
    var entry = value;
    for (var bit = 0; bit < 8; bit++) {
      entry = (entry & 1) == 1 ? (entry >> 1) ^ 0xedb88320 : entry >> 1;
    }
    return entry & 0xffffffff;
  }, growable: false);

  static int checksum(List<int> bytes) {
    var value = 0xffffffff;
    for (final byte in bytes) {
      value = ((value >> 8) ^ _table[(value ^ byte) & 0xff]) & 0xffffffff;
    }
    return (value ^ 0xffffffff) & 0xffffffff;
  }

  static String header(List<int> bytes) =>
      'crc32:${checksum(bytes).toRadixString(16).padLeft(8, '0')}';
}

class PassportDiscovery {
  PassportDiscovery._();

  static String? response(List<int> request, {required int receiverPort}) {
    if (receiverPort < 1 || receiverPort > 65535) return null;
    String text;
    try {
      text = String.fromCharCodes(request);
      if (request.any((byte) => byte > 0x7f)) return null;
    } on FormatException {
      return null;
    }

    final fields = text.trim().split(RegExp(r'\s+'));
    if (fields.length != 2 ||
        fields[0] != 'LL_DISCOVER_V1' ||
        !RegExp(r'^[0-9A-Fa-f]{8}$').hasMatch(fields[1])) {
      return null;
    }
    return 'LL_RECEIVER_V1 ${fields[1]} $receiverPort';
  }
}
