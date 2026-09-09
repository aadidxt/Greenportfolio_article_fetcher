from datetime import UTC, datetime

from app.scheduler import weekly_trigger


def test_weekly_schedule_is_monday_0900_asia_kolkata(test_settings):
    trigger = weekly_trigger(test_settings)
    after = datetime(2026, 9, 6, 0, 0, tzinfo=UTC)  # Sunday
    next_run = trigger.get_next_fire_time(None, after)
    assert next_run is not None
    assert next_run.weekday() == 0
    assert next_run.hour == 9
    assert next_run.minute == 0
    assert next_run.tzinfo.key == "Asia/Kolkata"
    assert next_run.astimezone(UTC) == datetime(2026, 9, 7, 3, 30, tzinfo=UTC)


def test_format_next_run_weekly_vs_daily_custom():
    from app.scheduler import format_next_run

    dt = datetime(2026, 9, 7, 9, 0, tzinfo=UTC)
    # Weekly includes weekday
    weekly_str = format_next_run(dt, timezone_name="Asia/Kolkata", frequency="weekly")
    assert "Monday" in weekly_str
    assert "02:30 PM" in weekly_str

    # Daily does NOT include weekday, only time
    daily_str = format_next_run(dt, timezone_name="Asia/Kolkata", frequency="daily")
    assert "Monday" not in daily_str
    assert "02:30 PM" in daily_str

    # Custom does NOT include weekday, only time
    custom_str = format_next_run(dt, timezone_name="Asia/Kolkata", frequency="custom")
    assert "Monday" not in custom_str
    assert "02:30 PM" in custom_str


