#include "passport_ptt_frame.h"

#include <stdbool.h>

static bool valid_payload(passport_ptt_frame_type_t type,
                          const uint8_t *payload, size_t payload_size)
{
    if (type == PASSPORT_PTT_FRAME_TYPE_BEGIN ||
        type == PASSPORT_PTT_FRAME_TYPE_END ||
        type == PASSPORT_PTT_FRAME_TYPE_ENTER) {
        return payload == NULL && payload_size == 0U;
    }
    return type == PASSPORT_PTT_FRAME_TYPE_AUDIO && payload != NULL &&
           payload_size == PASSPORT_PTT_FRAME_AUDIO_PAYLOAD_SIZE;
}

size_t passport_ptt_frame_encode(uint8_t *frame, size_t capacity,
                                 passport_ptt_frame_type_t type,
                                 uint16_t sequence, uint32_t monotonic_ms,
                                 const uint8_t *payload, size_t payload_size)
{
    if (frame == NULL || !valid_payload(type, payload, payload_size) ||
        payload_size > PASSPORT_PTT_FRAME_MAX_SIZE -
                           PASSPORT_PTT_FRAME_HEADER_SIZE ||
        capacity < PASSPORT_PTT_FRAME_HEADER_SIZE + payload_size) {
        return 0U;
    }

    frame[0] = PASSPORT_PTT_FRAME_VERSION;
    frame[1] = (uint8_t)type;
    frame[2] = (uint8_t)(sequence & 0xFFU);
    frame[3] = (uint8_t)(sequence >> 8U);
    frame[4] = (uint8_t)(monotonic_ms & 0xFFU);
    frame[5] = (uint8_t)((monotonic_ms >> 8U) & 0xFFU);
    frame[6] = (uint8_t)((monotonic_ms >> 16U) & 0xFFU);
    frame[7] = (uint8_t)(monotonic_ms >> 24U);
    for (size_t i = 0U; i < payload_size; ++i) {
        frame[PASSPORT_PTT_FRAME_HEADER_SIZE + i] = payload[i];
    }
    return PASSPORT_PTT_FRAME_HEADER_SIZE + payload_size;
}

passport_ptt_frame_result_t passport_ptt_frame_decode(
    const uint8_t *frame, size_t frame_size, passport_ptt_frame_t *decoded)
{
    if (frame == NULL || decoded == NULL) {
        return PASSPORT_PTT_FRAME_INVALID_ARGUMENT;
    }
    *decoded = (passport_ptt_frame_t){ 0 };
    if (frame_size < PASSPORT_PTT_FRAME_HEADER_SIZE) {
        return PASSPORT_PTT_FRAME_MALFORMED;
    }
    if (frame[0] != PASSPORT_PTT_FRAME_VERSION) {
        return PASSPORT_PTT_FRAME_UNSUPPORTED_VERSION;
    }

    const passport_ptt_frame_type_t type = (passport_ptt_frame_type_t)frame[1];
    size_t expected_payload_size;
    switch (type) {
    case PASSPORT_PTT_FRAME_TYPE_BEGIN:
    case PASSPORT_PTT_FRAME_TYPE_END:
    case PASSPORT_PTT_FRAME_TYPE_ENTER:
        expected_payload_size = 0U;
        break;
    case PASSPORT_PTT_FRAME_TYPE_AUDIO:
        expected_payload_size = PASSPORT_PTT_FRAME_AUDIO_PAYLOAD_SIZE;
        break;
    default:
        return PASSPORT_PTT_FRAME_UNKNOWN_TYPE;
    }
    const size_t payload_size = frame_size - PASSPORT_PTT_FRAME_HEADER_SIZE;
    if (payload_size != expected_payload_size) {
        return PASSPORT_PTT_FRAME_MALFORMED;
    }

    decoded->type = type;
    decoded->sequence = (uint16_t)((uint16_t)frame[2] |
                                   ((uint16_t)frame[3] << 8U));
    decoded->monotonic_ms = (uint32_t)frame[4] |
        ((uint32_t)frame[5] << 8U) |
        ((uint32_t)frame[6] << 16U) |
        ((uint32_t)frame[7] << 24U);
    decoded->payload = payload_size == 0U
        ? NULL : frame + PASSPORT_PTT_FRAME_HEADER_SIZE;
    decoded->payload_size = payload_size;
    return PASSPORT_PTT_FRAME_OK;
}
