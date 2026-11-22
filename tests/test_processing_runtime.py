from life_logger.runtime_queue import TaskQueue


# 读模型层（projection / DayViewStore / DayProjector）已于 2026-11-04 删除，
# 原先两条 projection 用例一并删除；保留下面的队列优先级用例（纯 TaskQueue 行为）。
def test_selected_cold_day_promotes_existing_background_prepare(tmp_path):
    queue=TaskQueue(tmp_path)
    queue.enqueue('projection','older',{'date':'2026-10-20'},priority=10)
    selected=queue.enqueue('projection','selected',{'date':'2026-10-21'},priority=10)
    queue.enqueue('projection','selected',{'date':'2026-10-21'},priority=0)
    assert queue.claim('worker',kinds=('projection',))['id']==selected['id']
