#include "passport_ptt_adpcm.h"

#include <limits.h>
#include <stdbool.h>

static const int16_t step_table[89] = {
    7, 8, 9, 10, 11, 12, 13, 14, 16, 17, 19, 21, 23, 25, 28, 31,
    34, 37, 41, 45, 50, 55, 60, 66, 73, 80, 88, 97, 107, 118, 130, 143,
    157, 173, 190, 209, 230, 253, 279, 307, 337, 371, 408, 449, 494, 544,
    598, 658, 724, 796, 876, 963, 1060, 1166, 1282, 1411, 1552, 1707,
    1878, 2066, 2272, 2499, 2749, 3024, 3327, 3660, 4026, 4428, 4871,
    5358, 5894, 6484, 7132, 7845, 8630, 9493, 10442, 11487, 12635, 13899,
    15289, 16818, 18500, 20350, 22385, 24623, 27086, 29794, 32767,
};

static const int8_t index_table[16] = {
    -1, -1, -1, -1, 2, 4, 6, 8,
    -1, -1, -1, -1, 2, 4, 6, 8,
};

static int16_t clamp_predictor(int32_t value)
{
    if (value > INT16_MAX) {
        return INT16_MAX;
    }
    if (value < INT16_MIN) {
        return INT16_MIN;
    }
    return (int16_t)value;
}

static uint8_t encode_sample(passport_ptt_adpcm_state_t *state, int16_t sample)
{
    const int32_t step = step_table[state->step_index];
    int32_t difference = (int32_t)sample - state->predictor;
    uint8_t code = 0U;
    int32_t delta = step >> 3;

    if (difference < 0) {
        code = 8U;
        difference = -difference;
    }
    if (difference >= step) {
        code |= 4U;
        difference -= step;
        delta += step;
    }
    if (difference >= (step >> 1)) {
        code |= 2U;
        difference -= step >> 1;
        delta += step >> 1;
    }
    if (difference >= (step >> 2)) {
        code |= 1U;
        delta += step >> 2;
    }

    if ((code & 8U) != 0U) {
        state->predictor = clamp_predictor((int32_t)state->predictor - delta);
    } else {
        state->predictor = clamp_predictor((int32_t)state->predictor + delta);
    }

    int index = (int)state->step_index + index_table[code];
    if (index < 0) {
        index = 0;
    } else if (index > 88) {
        index = 88;
    }
    state->step_index = (uint8_t)index;
    return code;
}

static int16_t decode_sample(passport_ptt_adpcm_state_t *state, uint8_t code)
{
    const int32_t step = step_table[state->step_index];
    int32_t delta = step >> 3;

    if ((code & 4U) != 0U) {
        delta += step;
    }
    if ((code & 2U) != 0U) {
        delta += step >> 1;
    }
    if ((code & 1U) != 0U) {
        delta += step >> 2;
    }

    if ((code & 8U) != 0U) {
        state->predictor = clamp_predictor((int32_t)state->predictor - delta);
    } else {
        state->predictor = clamp_predictor((int32_t)state->predictor + delta);
    }

    int index = (int)state->step_index + index_table[code];
    if (index < 0) {
        index = 0;
    } else if (index > 88) {
        index = 88;
    }
    state->step_index = (uint8_t)index;
    return state->predictor;
}

void passport_ptt_adpcm_state_init(passport_ptt_adpcm_state_t *state)
{
    if (state == NULL) {
        return;
    }
    state->predictor = 0;
    state->step_index = 0U;
    state->initialized = 0U;
}

passport_ptt_adpcm_result_t passport_ptt_adpcm_encode_block(
    passport_ptt_adpcm_state_t *state, const int16_t *pcm, size_t sample_count,
    uint8_t *encoded, size_t encoded_capacity)
{
    if (state == NULL || pcm == NULL || encoded == NULL ||
        sample_count < PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK ||
        encoded_capacity < PASSPORT_PTT_ADPCM_BLOCK_SIZE) {
        return PASSPORT_PTT_ADPCM_INVALID_ARGUMENT;
    }
    if (state->step_index > 88U) {
        return PASSPORT_PTT_ADPCM_INVALID_ARGUMENT;
    }
    if (state->initialized == 0U) {
        state->predictor = pcm[0];
        state->step_index = 0U;
        state->initialized = 1U;
    }

    const uint16_t predictor = (uint16_t)state->predictor;
    encoded[0] = (uint8_t)(predictor & 0xffU);
    encoded[1] = (uint8_t)(predictor >> 8U);
    encoded[2] = state->step_index;
    encoded[3] = 0U;
    for (size_t i = 0; i < PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK; i += 2U) {
        const uint8_t low = encode_sample(state, pcm[i]);
        const uint8_t high = encode_sample(state, pcm[i + 1U]);
        encoded[PASSPORT_PTT_ADPCM_HEADER_SIZE + i / 2U] =
            (uint8_t)(low | (uint8_t)(high << 4U));
    }
    return PASSPORT_PTT_ADPCM_OK;
}

passport_ptt_adpcm_result_t passport_ptt_adpcm_decode_block(
    passport_ptt_adpcm_state_t *state, const uint8_t *encoded,
    size_t encoded_size, int16_t *pcm, size_t sample_capacity)
{
    if (state == NULL || encoded == NULL || pcm == NULL ||
        encoded_size < PASSPORT_PTT_ADPCM_BLOCK_SIZE ||
        sample_capacity < PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK) {
        return PASSPORT_PTT_ADPCM_INVALID_ARGUMENT;
    }
    if (encoded[2] > 88U || encoded[3] != 0U) {
        return PASSPORT_PTT_ADPCM_MALFORMED_BLOCK;
    }

    const uint16_t raw_predictor =
        (uint16_t)encoded[0] | (uint16_t)((uint16_t)encoded[1] << 8U);
    int32_t signed_predictor = raw_predictor;
    if ((raw_predictor & 0x8000U) != 0U) {
        signed_predictor -= 65536;
    }
    state->predictor = (int16_t)signed_predictor;
    state->step_index = encoded[2];
    state->initialized = 1U;

    for (size_t i = 0; i < PASSPORT_PTT_ADPCM_SAMPLES_PER_BLOCK; ++i) {
        const uint8_t packed =
            encoded[PASSPORT_PTT_ADPCM_HEADER_SIZE + i / 2U];
        const uint8_t code = (i & 1U) == 0U ? (uint8_t)(packed & 0x0fU)
                                            : (uint8_t)(packed >> 4U);
        pcm[i] = decode_sample(state, code);
    }
    return PASSPORT_PTT_ADPCM_OK;
}
