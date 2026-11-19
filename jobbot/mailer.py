import smtplib
import ssl
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
from pathlib import Path


def send(cfg, to, subject, body, resume_path):
    env, profile = cfg["env"], cfg["profile"]
    msg = EmailMessage()
    msg["From"] = formataddr((f"{profile['first_name']} {profile['last_name']}", env["GMAIL_ADDRESS"]))
    msg["To"] = to
    msg["Subject"] = subject
    msg["Message-ID"] = make_msgid(domain=env["GMAIL_ADDRESS"].split("@")[1])
    msg.set_content(body)
    msg.add_attachment(Path(resume_path).read_bytes(), maintype="application", subtype="pdf",
                       filename=cfg["attachment_name"])
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context(), timeout=60) as smtp:
        smtp.login(env["GMAIL_ADDRESS"], env["GMAIL_APP_PASSWORD"].replace(" ", ""))
        smtp.send_message(msg)
