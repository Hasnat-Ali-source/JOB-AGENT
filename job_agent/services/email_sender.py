"""
Email Sender (Phase 6b).

Sends an approved application email. Two backends:

1. **Mail.app** (preferred on macOS) — driven by AppleScript. Uses the account
   the user already has configured, so the message appears in their Sent
   mailbox and threads normally with any reply. No credentials are involved at
   all, which is the main reason to prefer it.
2. **SMTP** — for users not running Mail.app. Authenticates with an
   **app-specific password** retrieved from the Keychain. The user's actual
   account password is never requested, stored, or accepted: app-specific
   passwords can be revoked individually and can't be used to sign in to the
   account itself.

Sending is only ever reached from an explicitly approved draft
(EmailSendService checks this), and is rate-limited per hour.
"""

import logging
import re
import smtplib
import subprocess
import tempfile
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import List, Optional

from job_agent.config import settings
from job_agent.utils.keychain import retrieve_app_password

logger = logging.getLogger(__name__)

APPLESCRIPT_TIMEOUT = 60  # seconds


@dataclass
class SendResult:
    """Outcome of sending one email."""

    sent: bool
    method: Optional[str] = None  # "mail_app" or "smtp"
    message_id: Optional[str] = None
    error_message: Optional[str] = None


class EmailSender:
    """Sends application emails via Mail.app or SMTP."""

    def __init__(
        self,
        prefer_mail_app: Optional[bool] = None,
        allow_smtp: Optional[bool] = None,
    ):
        """
        Initialize the sender.

        Args:
            prefer_mail_app: Try Mail.app first (defaults to settings)
            allow_smtp: Permit the SMTP fallback (defaults to settings)
        """
        self.prefer_mail_app = (
            settings.mail_app_enabled if prefer_mail_app is None else prefer_mail_app
        )
        self.allow_smtp = (
            settings.imap_smtp_enabled if allow_smtp is None else allow_smtp
        )

    def send(
        self,
        to_email: str,
        subject: str,
        body: str,
        attachments: Optional[List[str]] = None,
    ) -> SendResult:
        """
        Send an email, trying Mail.app then SMTP.

        Args:
            to_email: Recipient
            subject: Subject line
            body: Plain-text body
            attachments: File paths to attach

        Returns:
            SendResult
        """
        attachments = [a for a in (attachments or []) if Path(a).exists()]

        if self.prefer_mail_app:
            result = self._send_via_mail_app(to_email, subject, body, attachments)
            if result.sent:
                return result
            logger.info(f"Mail.app send failed ({result.error_message}) — trying SMTP")

        if self.allow_smtp:
            return self._send_via_smtp(to_email, subject, body, attachments)

        return SendResult(
            sent=False,
            error_message=(
                "No send method available: Mail.app failed and the SMTP fallback "
                "is disabled"
            ),
        )

    # ------------------------------------------------------------------
    # Mail.app
    # ------------------------------------------------------------------

    def _send_via_mail_app(
        self, to_email: str, subject: str, body: str, attachments: List[str]
    ) -> SendResult:
        """
        Send through Mail.app using AppleScript.

        Returns:
            SendResult
        """
        script = self._build_applescript(to_email, subject, body, attachments)

        # The script goes through a file rather than `osascript -e`: bodies are
        # multi-line and contain quotes, and shell-quoting them is exactly the
        # kind of thing that silently mangles a user's cover letter.
        with tempfile.NamedTemporaryFile(
            "w", suffix=".applescript", delete=False, encoding="utf-8"
        ) as handle:
            handle.write(script)
            script_path = Path(handle.name)

        try:
            completed = subprocess.run(
                ["osascript", str(script_path)],
                capture_output=True,
                text=True,
                timeout=APPLESCRIPT_TIMEOUT,
            )

            if completed.returncode != 0:
                error = (completed.stderr or "").strip() or "osascript failed"
                return SendResult(sent=False, method="mail_app", error_message=error)

            message_id = (completed.stdout or "").strip() or None

            logger.info(f"Sent an application email to {to_email} via Mail.app")

            return SendResult(sent=True, method="mail_app", message_id=message_id)

        except FileNotFoundError:
            return SendResult(
                sent=False, method="mail_app",
                error_message="osascript not found — this is macOS-only",
            )
        except subprocess.TimeoutExpired:
            return SendResult(
                sent=False, method="mail_app",
                error_message=f"Mail.app did not respond within {APPLESCRIPT_TIMEOUT}s",
            )
        except Exception as e:
            return SendResult(sent=False, method="mail_app", error_message=str(e))
        finally:
            script_path.unlink(missing_ok=True)

    @staticmethod
    def _build_applescript(
        to_email: str, subject: str, body: str, attachments: List[str]
    ) -> str:
        """
        Build the AppleScript that composes and sends the message.

        Args:
            to_email: Recipient
            subject: Subject line
            body: Plain-text body
            attachments: File paths

        Returns:
            AppleScript source
        """
        esc = EmailSender._applescript_escape

        attachment_lines = "\n".join(
            f'        make new attachment with properties '
            f'{{file name:POSIX file "{esc(path)}"}} at after the last paragraph'
            for path in attachments
        )

        return f'''tell application "Mail"
    set newMessage to make new outgoing message with properties {{subject:"{esc(subject)}", content:"{esc(body)}", visible:false}}
    tell newMessage
        make new to recipient at end of to recipients with properties {{address:"{esc(to_email)}"}}
{attachment_lines}
        send
    end tell
    return message id of newMessage
end tell
'''

    @staticmethod
    def _applescript_escape(value: str) -> str:
        """
        Escape a Python string for embedding in an AppleScript literal.

        Backslashes must be escaped before quotes, or the escape character
        introduced for a quote would itself be escaped.
        """
        return (
            value.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\r\n", "\\n")
            .replace("\n", "\\n")
            .replace("\r", "\\n")
        )

    # ------------------------------------------------------------------
    # SMTP
    # ------------------------------------------------------------------

    def _send_via_smtp(
        self, to_email: str, subject: str, body: str, attachments: List[str]
    ) -> SendResult:
        """
        Send through SMTP using an app-specific password from the Keychain.

        Returns:
            SendResult
        """
        username = getattr(settings, "smtp_username", None)
        host = getattr(settings, "smtp_host", None)
        port = getattr(settings, "smtp_port", 587)

        if not username or not host:
            return SendResult(
                sent=False, method="smtp",
                error_message=(
                    "SMTP is not configured — set SMTP_HOST and SMTP_USERNAME, and "
                    "store an app-specific password with "
                    "store_app_password('smtp', <username>, <app password>)"
                ),
            )

        app_password = retrieve_app_password("smtp", username)

        if not app_password:
            return SendResult(
                sent=False, method="smtp",
                error_message=(
                    f"No app-specific password in the Keychain for {username}. "
                    f"Create one in your mail provider's security settings — never "
                    f"use your account password."
                ),
            )

        message = self._build_message(username, to_email, subject, body, attachments)

        try:
            with smtplib.SMTP(host, port, timeout=30) as server:
                server.starttls()
                server.login(username, app_password)
                server.send_message(message)

            logger.info(f"Sent an application email to {to_email} via SMTP")

            return SendResult(sent=True, method="smtp", message_id=message["Message-ID"])

        except smtplib.SMTPAuthenticationError:
            return SendResult(
                sent=False, method="smtp",
                error_message=(
                    "SMTP rejected the app-specific password — it may have been "
                    "revoked or regenerated"
                ),
            )
        except Exception as e:
            return SendResult(sent=False, method="smtp", error_message=str(e))

    @staticmethod
    def _build_message(
        from_email: str,
        to_email: str,
        subject: str,
        body: str,
        attachments: List[str],
    ) -> EmailMessage:
        """
        Build the MIME message for SMTP.

        Args:
            from_email: Sender
            to_email: Recipient
            subject: Subject line
            body: Plain-text body
            attachments: File paths

        Returns:
            EmailMessage
        """
        message = EmailMessage()
        message["From"] = from_email
        message["To"] = to_email
        message["Subject"] = subject
        message["Message-ID"] = make_msgid()
        message.set_content(body)

        for path_str in attachments:
            path = Path(path_str)
            data = path.read_bytes()

            subtype = "pdf" if path.suffix.lower() == ".pdf" else "octet-stream"
            message.add_attachment(
                data, maintype="application", subtype=subtype, filename=path.name
            )

        return message

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    @staticmethod
    def is_valid_address(email: str) -> bool:
        """
        Basic sanity check on a recipient address.

        Args:
            email: Address to check

        Returns:
            True if it looks like a deliverable address
        """
        if not email or len(email) > 254:
            return False

        return bool(re.fullmatch(r"[\w.+-]+@[\w-]+\.[\w.-]*[\w]", email.strip()))
