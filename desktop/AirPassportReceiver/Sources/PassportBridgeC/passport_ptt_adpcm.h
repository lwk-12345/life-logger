#ifndef PASSPORT_PTT_ADPCM_H
#define PASSPORT_PTT_ADPCM_H

#include <stddef.h>
#include <stdint.h>

#define PASSPORT_PTT_ADPCM_SAMPLE_RATE_HZ 16000U
#define PASSPORT_PTT_ADPCM_BLOCK_DURATION_MS 20U
#define PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK 320U
#define PASSPORT_PTT_ADPCM_HEADER_SIZE 4U
#define PASSPORT_PTT_ADPCM_PAYLOAD_SIZE 160U
#define PASSPORT_PTT_ADPCM_BLOCK_SIZE 164U

typedef enum {
    PASSPORT_PTT_ADPCM_OK = 0,
    PASSPORT_PTT_ADPCM_INVALID_ARGUMENT = -1,
    PASSPORT_PTT_ADPCM_MALFORMED_BLOCK = -2,
} passport_ptt_adpcm_result_t;

/* Caller-owned stream state. Initialize once per stream, then pass the same
 * state to each sequential block operation; do not reinitialize between blocks. */
typedef struct {
    int16_t predictor;
    uint8_t step_index;
    uint8_t initialized;
} passport_ptt_adpcm_state_t;

/* Block bytes: signed initial predictor as little-endian PCM16 (bytes 0..1),
 * IMA step index 0..88 (byte 2), reserved zero (byte 3), followed by 320
 * signed 4-bit codes in 160 bytes. Even sample codes occupy the low nibble;
 * odd sample codes occupy the high nibble. The header stores the state at the
 * start of that block. State objects belong to the caller and advance in place. */
void passport_ptt_adpcm_state_init(passport_ptt_adpcm_state_t *state);

passport_ptt_adpcm_result_t passport_ptt_adpcm_encode_block(
    passport_ptt_adpcm_state_t *state, const int16_t *pcm, size_t sample_count,
    uint8_t *encoded, size_t encoded_capacity);

passport_ptt_adpcm_result_t passport_ptt_adpcm_decode_block(
    passport_ptt_adpcm_state_t *state, const uint8_t *encoded,
    size_t encoded_size, int16_t *pcm, size_t sample_capacity);

#endif
