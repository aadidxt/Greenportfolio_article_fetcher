from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.database import Database
from app.emailer import WeeklyEmailSender
from app.main import app, database, settings
from app.scheduler import format_next_run, trigger_from_schedule


def test_recipient_crud_and_validation(tmp_path):
    db = Database(tmp_path / "test_cms.db")

    # Add recipient
    rec = db.add_recipient("alex@example.com", active=True)
    assert rec["email"] == "alex@example.com"
    assert rec["active"] is True
    assert rec["id"]

    # Duplicate prevention
    with pytest.raises(ValueError, match="already exists"):
        db.add_recipient("alex@example.com")

    # Case-insensitive duplicate prevention
    with pytest.raises(ValueError, match="already exists"):
        db.add_recipient("ALEX@example.com")

    # Invalid email
    with pytest.raises(ValueError, match="valid email"):
        db.add_recipient("not-an-email")

    # List recipients
    recipients = db.list_recipients()
    assert len(recipients) == 1
    assert recipients[0]["email"] == "alex@example.com"

    # Add second recipient (disabled)
    rec2 = db.add_recipient("bob@example.com", active=False)
    assert len(db.list_recipients()) == 2

    # Active recipient emails only
    active_emails = db.get_active_recipient_emails()
    assert active_emails == ["alex@example.com"]

    # Update recipient: disable alex, enable bob
    db.update_recipient(rec["id"], active=False)
    db.update_recipient(rec2["id"], active=True)
    assert db.get_active_recipient_emails() == ["bob@example.com"]

    # Delete recipient
    assert db.delete_recipient(rec["id"]) is True
    assert len(db.list_recipients()) == 1
    assert db.delete_recipient("non-existent-id") is False


def test_automation_schedule_persistence_and_trigger(tmp_path):
    db = Database(tmp_path / "test_sched.db")

    # Default schedule
    default_sched = db.get_automation_schedule("Asia/Kolkata")
    assert default_sched["enabled"] is True
    assert default_sched["frequency"] == "weekly"
    assert default_sched["day_of_week"] == "mon"
    assert default_sched["hour"] == 9
    assert default_sched["minute"] == 0
    assert default_sched["timezone"] == "Asia/Kolkata"

    # Save custom schedule: Daily at 14:30
    saved = db.save_automation_schedule({
        "enabled": True,
        "frequency": "daily",
        "hour": 14,
        "minute": 30,
        "timezone": "Asia/Kolkata",
    })
    assert saved["frequency"] == "daily"
    assert saved["hour"] == 14
    assert saved["minute"] == 30

    # Retrieve saved
    loaded = db.get_automation_schedule()
    assert loaded["frequency"] == "daily"
    assert loaded["hour"] == 14

    # Trigger generation
    trigger = trigger_from_schedule(loaded)
    now = datetime(2026, 9, 8, 10, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    next_fire = trigger.get_next_fire_time(None, now)
    assert next_fire is not None
    assert next_fire.hour == 14
    assert next_fire.minute == 30

    # Formatted next run
    formatted = format_next_run(next_fire, "Asia/Kolkata")
    assert "02:30 PM" in formatted

    # Invalid validations
    with pytest.raises(ValueError, match="frequency"):
        db.save_automation_schedule({"frequency": "hourly"})

    with pytest.raises(ValueError, match="Hour"):
        db.save_automation_schedule({"hour": 25})

    with pytest.raises(ValueError, match="Minute"):
        db.save_automation_schedule({"minute": 60})

    with pytest.raises(ValueError, match="timezone"):
        db.save_automation_schedule({"timezone": "Fake/Zone"})


def test_weekly_email_uses_active_recipients(test_settings, tmp_path):
    from dataclasses import replace
    db = Database(tmp_path / "test_email.db")
    db.add_recipient("active@example.com", active=True)
    db.add_recipient("inactive@example.com", active=False)

    sender = WeeklyEmailSender(replace(test_settings, email_recipients=()), database=db)
    recipients = sender.get_recipients()
    assert recipients == ["active@example.com"]
    assert sender.configuration_status()["recipients_count"] == 1


def test_admin_api_endpoints():
    client = TestClient(app)

    health_resp = client.get("/health")
    assert health_resp.status_code == 200
    assert health_resp.json() == {"status": "ok"}
    assert health_resp.headers["cache-control"] == "no-store"

    # Overview endpoint contains CMS fields
    overview_resp = client.get("/api/overview")
    assert overview_resp.status_code == 200
    data = overview_resp.json()
    assert "automation_schedule" in data
    assert "active_recipients_count" in data
    assert "total_recipients_count" in data

    # Add recipient via API
    add_resp = client.post("/api/admin/recipients", json={"email": "cms-test@example.com", "active": True})
    assert add_resp.status_code == 201
    rec = add_resp.json()
    rec_id = rec["id"]
    assert rec["email"] == "cms-test@example.com"

    # List recipients
    list_resp = client.get("/api/admin/recipients")
    assert list_resp.status_code == 200
    assert any(item["id"] == rec_id for item in list_resp.json()["items"])

    # Update recipient (disable)
    update_resp = client.put(f"/api/admin/recipients/{rec_id}", json={"active": False})
    assert update_resp.status_code == 200
    assert update_resp.json()["active"] is False

    # Delete recipient
    del_resp = client.delete(f"/api/admin/recipients/{rec_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # Save automation schedule via API
    auto_resp = client.post(
        "/api/admin/automation",
        json={
            "enabled": True,
            "frequency": "weekly",
            "day_of_week": "wed",
            "hour": 10,
            "minute": 15,
            "timezone": "Asia/Kolkata",
        },
    )
    assert auto_resp.status_code == 200
    auto_data = auto_resp.json()
    assert auto_data["status"] == "saved"
    assert auto_data["schedule"]["day_of_week"] == "wed"
    assert auto_data["next_scheduled_fetch_formatted"] is not None


def test_resend_email_delivery(test_settings, tmp_path):
    from dataclasses import replace
    from unittest.mock import MagicMock, patch

    db = Database(tmp_path / "test_resend.db")
    db.add_recipient("user@example.com", active=True)

    settings = replace(
        test_settings,
        email_enabled=True,
        resend_api_key="re_mock_12345",
        email_from="Alerts <onboarding@resend.dev>",
        email_recipients=(),
    )
    sender = WeeklyEmailSender(settings, database=db)

    # Verify status
    status = sender.configuration_status()
    assert status["enabled"] is True
    assert status["configured"] is True
    assert status["provider"] == "Resend"
    assert "Ready (Resend)" in status["message"]

    # Mock requests.post success
    mock_resp = MagicMock()
    mock_resp.ok = True
    mock_resp.status_code = 200

    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)
    with patch("requests.post", return_value=mock_resp) as mock_post:
        sender.send(
            new_xlsx=b"fake-new-xlsx",
            all_xlsx=b"fake-all-xlsx",
            new_count=5,
            fetched_at=now,
        )

        mock_post.assert_called_once()
        call_args, call_kwargs = mock_post.call_args
        assert call_args[0] == "https://api.resend.com/emails"
        assert call_kwargs["headers"]["Authorization"] == "Bearer re_mock_12345"
        payload = call_kwargs["json"]
        assert payload["to"] == ["user@example.com"]
        assert payload["from"] == "Alerts <onboarding@resend.dev>"
        assert "5 new article(s)" in payload["subject"]
        assert len(payload["attachments"]) == 2
        assert payload["attachments"][0]["filename"] == "new_articles_2026-09-08.xlsx"
        assert payload["attachments"][1]["filename"] == "green_portfolio_all_articles_2026-09-08.xlsx"

    # Test error handling
    mock_err_resp = MagicMock()
    mock_err_resp.ok = False
    mock_err_resp.status_code = 403
    mock_err_resp.text = "Forbidden"
    mock_err_resp.json.return_value = {"message": "Invalid API key"}

    with patch("requests.post", return_value=mock_err_resp):
        with pytest.raises(RuntimeError, match=r"Resend email delivery failed \(403\): Invalid API key"):
            sender.send(
                new_xlsx=b"new",
                all_xlsx=b"all",
                new_count=1,
                fetched_at=now,
            )


def test_email_includes_google_sheet_link(test_settings):
    from dataclasses import replace
    settings = replace(
        test_settings,
        google_sheets_enabled=True,
        google_sheet_id="11KZA69WxbxLeDP0O_Fg94CnooIWpzVnvJyatRuSP0rw",
    )
    sender = WeeklyEmailSender(settings)
    now = datetime(2026, 9, 8, 12, 0, tzinfo=UTC)

    plain = sender._render_plain_text(3, now)
    assert "Live Google Sheet Datastore:" in plain
    assert "11KZA69WxbxLeDP0O_Fg94CnooIWpzVnvJyatRuSP0rw" in plain

    html = sender._render_html(3, now)
    assert "Live Google Sheet Datastore" in html
    assert "11KZA69WxbxLeDP0O_Fg94CnooIWpzVnvJyatRuSP0rw" in html
    assert "Open Google Sheet ↗" in html
