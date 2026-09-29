from smartgpms.scheduling import AdaptiveSyncSchedule


def test_adaptive_schedule_backs_off_and_activity_restores_active_interval():
    schedule = AdaptiveSyncSchedule()

    assert [schedule.observe(False) for _ in range(2)] == [600, 600]
    assert schedule.observe(False) == 1800
    assert [schedule.observe(False) for _ in range(2)] == [1800, 1800]
    assert schedule.observe(False) == 3600

    assert schedule.observe(True) == 600
    assert schedule.empty_runs == 0


def test_adaptive_schedule_reset_clears_partial_idle_streak():
    schedule = AdaptiveSyncSchedule()
    schedule.observe(False)
    schedule.observe(False)

    assert schedule.reset()
    assert schedule.empty_runs == 0
    assert schedule.interval_seconds == 600

