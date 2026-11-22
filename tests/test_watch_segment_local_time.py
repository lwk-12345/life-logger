from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from life_logger.watch_formal_asr import WatchFormalASRProcessor


def test_segment_rows_get_local_time_from_absolute_not_segment_clock(tmp_path: Path):
    output = tmp_path / 'segment'
    output.mkdir()
    for name, data in {
        'run-summary.json': {'status': 'completed', 'transcript_count': 1},
        'recordings.json': [],
        'transcript.json': [{'text': '这是一句用于测试的普通话。', 'source_label': 'unknown',
                             'start_offset_seconds': 2, 'end_offset_seconds': 4,
                             'clock_local': '00:00'}],
    }.items():
        (output / name).write_text(json.dumps(data))
    processor = WatchFormalASRProcessor.__new__(WatchFormalASRProcessor)
    processor.store = SimpleNamespace(root=tmp_path / 'watch-sessions')
    row = processor._read_segment_result(output, 3, 60000, '2026-10-16T16:29:00+00:00')[0]
    assert row['absolute_started_at'] == '2026-10-16T16:30:02+00:00'
    assert row['started_at_local'] == '2026-10-17T00:30:02+08:00'
    assert row['clock_local'] == '00:30'
