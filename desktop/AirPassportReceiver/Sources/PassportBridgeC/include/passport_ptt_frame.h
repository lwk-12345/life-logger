#ifndef PASSPORT_PTT_FRAME_H
#define PASSPORT_PTT_FRAME_H

#include <stddef.h>
#include <stdint.h>

#define PASSPORT_PTT_FRAME_VERSION 1U
#define PASSPORT_PTT_FRAME_HEADER_SIZE 8U
#define PASSPORT_PTT_FRAME_MAX_SIZE 172U
#define PASSPORT_PTT_FRAME_AUDIO_PAYLOAD_SIZE 164U

typedef enum {
    PASSPORT_PTT_FRAME_TYPE_BEGIN = 1,
    PASSPORT_PTT_FRAME_TYPE_AUDIO = 2,
    PASSPORT_PTT_FRAME_TYPE_END = 3,
    PASSPORT_PTT_FRAME_TYPE_ENTER = 4,
} passport_ptt_frame_type_t;

typedef enum {
    PASSPORT_PTT_FRAME_OK = 0,
    PASSPORT_PTT_FRAME_INVALID_ARGUMENT = -1,
    PASSPORT_PTT_FRAME_UNSUPPORTED_VERSION = -2,
    PASSPORT_PTT_FRAME_UNKNOWN_TYPE = -3,
    PASSPORT_PTT_FRAME_MALFORMED = -4,
} passport_ptt_frame_result_t;

typedef struct {
    passport_ptt_frame_type_t type;
    uint16_t sequence;
    uint32_t monotonic_ms;
    /* Borrowed view into the input frame; valid while that input buffer lives. */
    const uint8_t *payload;
    size_t payload_size;
} passport_ptt_frame_t;

/* v1 wire layout: version, type, sequence (LE16), monotonic milliseconds
 * (LE32), then optional type-specific payload. BEGIN and END have no payload;
 * AUDIO carries one 164-byte IMA-ADPCM block. Returns 0 on invalid input. */
size_t passport_ptt_frame_encode(uint8_t *frame, size_t capacity,
                                 passport_ptt_frame_type_t type,
                                 uint16_t sequence, uint32_t monotonic_ms,
                                 const uint8_t *payload, size_t payload_size);

/* Parse a complete v1 frame without copying payload bytes. */
passport_ptt_frame_result_t passport_ptt_frame_decode(
    const uint8_t *frame, size_t frame_size, passport_ptt_frame_t *decoded);

#endif
