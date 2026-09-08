from __future__ import annotations

import base64
import logging
import smtplib
from datetime import datetime
from email.message import EmailMessage
from typing import TYPE_CHECKING

import requests

from .config import Settings

if TYPE_CHECKING:
    from .database import Database

logger = logging.getLogger(__name__)


class WeeklyEmailSender:
    def __init__(self, settings: Settings, database: Database | None = None):
        self.settings = settings
        self.database = database

    def get_recipients(self) -> list[str]:
        if self.database is not None:
            active = self.database.get_active_recipient_emails()
            if active:
                return active
        return list(self.settings.email_recipients)

    def configuration_status(self) -> dict[str, object]:
        recipients = self.get_recipients()
        if not self.settings.email_enabled:
            return {
                "enabled": False,
                "configured": True,
                "message": "Disabled",
                "recipients_count": len(recipients),
                "provider": "Disabled",
            }

        has_resend = bool(self.settings.resend_api_key and recipients)
        has_smtp = bool(
            self.settings.smtp_host
            and (self.settings.smtp_from or self.settings.email_from)
            and recipients
        )
        configured = has_resend or has_smtp
        provider = "Resend" if has_resend else ("SMTP" if has_smtp else "Unconfigured")
        message = (
            f"Ready ({provider})"
            if configured
            else "RESEND_API_KEY or SMTP credentials required"
        )
        return {
            "enabled": True,
            "configured": configured,
            "message": message,
            "provider": provider,
            "recipients_count": len(recipients),
        }

    def send(
        self,
        *,
        new_xlsx: bytes,
        all_xlsx: bytes,
        new_count: int,
        fetched_at: datetime,
        recipients: list[str] | tuple[str, ...] | None = None,
    ) -> None:
        if not self.settings.email_enabled:
            return
        status = self.configuration_status()
        if not status["configured"]:
            raise RuntimeError(str(status["message"]))

        target_recipients = list(recipients) if recipients is not None else self.get_recipients()
        if not target_recipients:
            raise RuntimeError("No active email recipients configured")

        if self.settings.resend_api_key:
            self._send_resend(
                recipients=target_recipients,
                new_xlsx=new_xlsx,
                all_xlsx=all_xlsx,
                new_count=new_count,
                fetched_at=fetched_at,
            )
        else:
            self._send_smtp(
                recipients=target_recipients,
                new_xlsx=new_xlsx,
                all_xlsx=all_xlsx,
                new_count=new_count,
                fetched_at=fetched_at,
            )

    def get_sheet_url(self) -> str | None:
        raw = self.settings.google_sheet_id.strip()
        if not raw:
            return None
        if raw.startswith("http://") or raw.startswith("https://"):
            return raw
        return f"https://docs.google.com/spreadsheets/d/{raw}/edit"

    def _render_plain_text(self, new_count: int, fetched_at: datetime) -> str:
        date_label = fetched_at.date().isoformat()
        sheet_url = self.get_sheet_url() if self.settings.google_sheets_enabled else None
        lines = [
            "Green Portfolio Media Monitor completed successfully.",
            "",
            f"New articles found: {new_count}",
            f"Fetch completed: {fetched_at.isoformat()}",
            "",
        ]
        if sheet_url:
            lines.extend([
                "Live Google Sheet Datastore:",
                sheet_url,
                "",
            ])
        lines.extend([
            "Attached are the updated workbooks:",
            f"• new_articles_{date_label}.xlsx (Freshly discovered coverage)",
            f"• green_portfolio_all_articles_{date_label}.xlsx (Complete cumulative archive)",
        ])
        return "\n".join(lines)

    def _render_html(self, new_count: int, fetched_at: datetime) -> str:
        date_label = fetched_at.date().isoformat()
        sheet_url = self.get_sheet_url() if self.settings.google_sheets_enabled else None

        sheet_banner = ""
        sheet_list_item = ""
        if sheet_url:
            sheet_banner = (
                f"<div style='background: #eef7f0; border: 1px solid #b8e2c0; border-radius: 8px; padding: 14px 18px; margin: 18px 0;'>"
                f"<div style='margin-bottom: 8px;'>"
                f"<strong style='color: #003134; font-size: 14px;'>📊 Live Google Sheet Datastore</strong>"
                f"<p style='margin: 4px 0 0; color: #4b6358; font-size: 12px;'>Access the real-time cloud spreadsheet with all collected media articles:</p>"
                f"</div>"
                f"<a href='{sheet_url}' target='_blank' style='display: inline-block; background: #004d40; color: #ffffff; text-decoration: none; padding: 8px 16px; border-radius: 6px; font-size: 12px; font-weight: bold;'>Open Google Sheet ↗</a>"
                f"</div>"
            )
            sheet_list_item = f"<li><a href='{sheet_url}' target='_blank' style='color: #004d40; font-weight: bold;'>Live Google Sheet</a> — View online</li>"

        return (
            "<div style='font-family: sans-serif; color: #003134; max-width: 600px; line-height: 1.5;'>"
            "<h2 style='color: #003134; margin-bottom: 8px;'>Green Portfolio Media Monitor</h2>"
            f"<p style='color: #607371; font-size: 13px;'>Automated media crawl completed on <strong>{fetched_at.strftime('%A, %d %b %Y — %I:%M %p')} UTC</strong>.</p>"
            f"<div style='background: #f4f6ef; border-left: 4px solid #85b000; padding: 14px 18px; border-radius: 6px; margin: 18px 0;'>"
            f"<p style='margin: 0; font-size: 15px; font-weight: bold;'>{new_count} new verified article(s) discovered.</p>"
            "</div>"
            f"{sheet_banner}"
            "<p>Attached are the updated workbooks:</p>"
            "<ul>"
            f"<li><strong>new_articles_{date_label}.xlsx</strong> — Freshly discovered coverage from this fetch</li>"
            f"<li><strong>green_portfolio_all_articles_{date_label}.xlsx</strong> — Complete cumulative media archive</li>"
            f"{sheet_list_item}"
            "</ul>"
            "<hr style='border: none; border-top: 1px solid #d8e1de; margin: 24px 0;' />"
            "<p style='font-size: 11px; color: #829390;'>Internal automated intelligence report for Green Portfolio.</p>"
            "</div>"
        )

    def _send_resend(
        self,
        *,
        recipients: list[str],
        new_xlsx: bytes,
        all_xlsx: bytes,
        new_count: int,
        fetched_at: datetime,
    ) -> None:
        date_label = fetched_at.date().isoformat()
        sender_from = (
            self.settings.email_from
            or self.settings.smtp_from
            or "Green Portfolio Media Monitor <onboarding@resend.dev>"
        )

        payload: dict[str, object] = {
            "from": sender_from,
            "to": recipients,
            "subject": f"Green Portfolio media monitor — {new_count} new article(s)",
            "text": self._render_plain_text(new_count, fetched_at),
            "html": self._render_html(new_count, fetched_at),
            "attachments": [
                {
                    "filename": f"new_articles_{date_label}.xlsx",
                    "content": base64.b64encode(new_xlsx).decode("utf-8"),
                },
                {
                    "filename": f"green_portfolio_all_articles_{date_label}.xlsx",
                    "content": base64.b64encode(all_xlsx).decode("utf-8"),
                },
            ],
        }
        if self.settings.email_reply_to:
            payload["reply_to"] = self.settings.email_reply_to

        response = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {self.settings.resend_api_key.strip()}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.settings.request_timeout_seconds,
        )

        if not response.ok:
            error_msg = response.text
            try:
                error_msg = response.json().get("message", error_msg)
            except Exception:
                pass
            raise RuntimeError(f"Resend email delivery failed ({response.status_code}): {error_msg}")

        logger.info("Weekly report emailed via Resend to %d recipient(s)", len(recipients))

    def _send_smtp(
        self,
        *,
        recipients: list[str],
        new_xlsx: bytes,
        all_xlsx: bytes,
        new_count: int,
        fetched_at: datetime,
    ) -> None:
        date_label = fetched_at.date().isoformat()
        sender_from = self.settings.smtp_from or self.settings.email_from
        message = EmailMessage()
        message["Subject"] = f"Green Portfolio media monitor — {new_count} new article(s)"
        message["From"] = sender_from
        message["To"] = ", ".join(recipients)
        if self.settings.email_reply_to:
            message["Reply-To"] = self.settings.email_reply_to
        message.set_content(self._render_plain_text(new_count, fetched_at))
        message.add_alternative(self._render_html(new_count, fetched_at), subtype="html")
        message.add_attachment(
            new_xlsx,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"new_articles_{date_label}.xlsx",
        )
        message.add_attachment(
            all_xlsx,
            maintype="application",
            subtype="vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            filename=f"green_portfolio_all_articles_{date_label}.xlsx",
        )

        with smtplib.SMTP(
            self.settings.smtp_host,
            self.settings.smtp_port,
            timeout=self.settings.request_timeout_seconds,
        ) as smtp:
            smtp.ehlo()
            if self.settings.smtp_use_tls:
                smtp.starttls()
                smtp.ehlo()
            if self.settings.smtp_username:
                smtp.login(self.settings.smtp_username, self.settings.smtp_password)
            smtp.send_message(message)
        logger.info("Weekly report emailed via SMTP to %d recipient(s)", len(recipients))
