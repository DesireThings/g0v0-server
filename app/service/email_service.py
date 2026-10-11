"""
Email verification service.
"""

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import secrets
import smtplib
import string

from app.config import settings
from app.log import logger


class EmailService:
    """Email sending service."""

    def __init__(self):
        self.smtp_server = settings.smtp_server
        self.smtp_port = settings.smtp_port
        self.smtp_username = settings.smtp_username
        self.smtp_password = settings.smtp_password
        self.from_email = settings.from_email
        self.from_name = settings.from_name

    def generate_verification_code(self) -> str:
        """Generate 8-digit verification code."""
        # Use only digits to avoid ambiguity
        return "".join(secrets.choice(string.digits) for _ in range(8))

    async def send_verification_email(self, email: str, code: str, username: str) -> bool:
        """Send verification email."""
        try:
            msg = MIMEMultipart()
            msg["From"] = f"{self.from_name} <{self.from_email}>"
            msg["To"] = email
            msg["Subject"] = "Email Verification"

            # HTML email content
            html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <style>
        .container {{
            max-width: 600px;
            margin: 0 auto;
            font-family: Arial, sans-serif;
            line-height: 1.6;
        }}
        .header {{
            background: #ED8EA6;
            color: white;
            padding: 20px;
            text-align: center;
            border-radius: 10px 10px 0 0;
        }}
        .content {{
            background: #f9f9f9;
            padding: 30px;
            border: 1px solid #ddd;
        }}
        .code {{
            background: #fff;
            border: 2px solid #ED8EA6;
            border-radius: 8px;
            padding: 15px;
            text-align: center;
            font-size: 24px;
            font-weight: bold;
            letter-spacing: 3px;
            margin: 20px 0;
            color: #333;
        }}
        .footer {{
            background: #333;
            color: #fff;
            padding: 15px;
            text-align: center;
            border-radius: 0 0 10px 10px;
            font-size: 12px;
        }}
        .warning {{
            background: #fff3cd;
            border: 1px solid #ffeaa7;
            border-radius: 5px;
            padding: 10px;
            margin: 15px 0;
            color: #856404;
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>osu! Email Verification</h1>
            <p>Email Verification</p>
        </div>

        <div class="content">
            <h2>Hello {username}!</h2>
            <p>Thank you for registering on our osu! server. To complete your account verification, please enter the following verification code:</p>

            <div class="code">{code}</div>

            <p>This verification code will expire in <strong>10 minutes</strong>.</p>

            <div class="warning">
                <strong>Important:</strong>
                <ul>
                    <li>Do not share this verification code with anyone</li>
                    <li>If you did not request this verification code, please ignore this email</li>
                    <li>The verification code can only be used once</li>
                </ul>
            </div>

            <p>If you have any questions, please contact our support team.</p>
        </div>

        <div class="footer">
            <p>© 2025 g0v0! Private Server. This email was sent automatically, please do not reply.</p>
        </div>
    </div>
</body>
</html>
            """  # noqa: E501

            msg.attach(MIMEText(html_content, "html", "utf-8"))

            with smtplib.SMTP(self.smtp_server, self.smtp_port) as server:
                if self.smtp_username and self.smtp_password:
                    server.starttls()
                    server.login(self.smtp_username, self.smtp_password)

                server.send_message(msg)

            logger.info(f"Successfully sent verification code to {email}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False


# Global email service instance
email_service = EmailService()
