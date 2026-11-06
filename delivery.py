"""Delivers a generated report two ways:

1. Cloud-synced folder — copies the file into a local folder that your
   Google Drive / Dropbox desktop app already watches and uploads for you.
   No API keys needed. Point CLOUD_SYNC_FOLDER_PATH (env var) at that real
   folder, e.g. "C:\\Users\\you\\Google Drive\\Arenas Beach Reports" or
   "C:\\Users\\you\\Dropbox\\Arenas Beach Reports". Until you set it, files
   land in ./cloud_sync_reports so the feature still works end-to-end.

2. Email — sends the file as an attachment over SMTP. Nothing is sent until
   SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and REPORT_TO_EMAIL are set
   (see .env.example).
"""
import os
import shutil
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_CLOUD_FOLDER = os.path.join(BASE_DIR, 'cloud_sync_reports')


def cloud_folder_path():
    return os.environ.get('CLOUD_SYNC_FOLDER_PATH', DEFAULT_CLOUD_FOLDER)


def cloud_folder_is_configured():
    return 'CLOUD_SYNC_FOLDER_PATH' in os.environ


def sync_to_cloud_folder(file_path):
    target_dir = cloud_folder_path()
    try:
        os.makedirs(target_dir, exist_ok=True)
        target_path = os.path.join(target_dir, os.path.basename(file_path))
        shutil.copy2(file_path, target_path)
        return {'synced': True, 'target_path': target_path}
    except OSError as e:
        return {'synced': False, 'error': str(e)}


def email_is_configured():
    required = ('SMTP_HOST', 'SMTP_USERNAME', 'SMTP_PASSWORD', 'REPORT_TO_EMAIL')
    return all(os.environ.get(k) for k in required)


def send_report_email(file_path, period_label):
    if not email_is_configured():
        return {
            'sent': False,
            'error': 'SMTP not configured — set SMTP_HOST, SMTP_USERNAME, SMTP_PASSWORD and '
                     'REPORT_TO_EMAIL environment variables (see .env.example)',
        }

    host = os.environ['SMTP_HOST']
    port = int(os.environ.get('SMTP_PORT', '587'))
    username = os.environ['SMTP_USERNAME']
    password = os.environ['SMTP_PASSWORD']
    from_addr = os.environ.get('REPORT_FROM_EMAIL', username)
    to_addr = os.environ['REPORT_TO_EMAIL']

    msg = MIMEMultipart()
    msg['From'] = from_addr
    msg['To'] = to_addr
    msg['Subject'] = f'Arenas Beach Hotel — Checkout & Payment Report ({period_label})'
    msg.attach(MIMEText(
        f'Attached is the checkout and payment report for {period_label}.\n\n'
        'This is an automated message from the Arenas Beach Hotel dashboard.',
        'plain',
    ))

    with open(file_path, 'rb') as f:
        part = MIMEBase('application', 'octet-stream')
        part.set_payload(f.read())
    encoders.encode_base64(part)
    part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(file_path)}"')
    msg.attach(part)

    try:
        with smtplib.SMTP(host, port, timeout=20) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)
        return {'sent': True, 'to': to_addr}
    except (smtplib.SMTPException, OSError) as e:
        return {'sent': False, 'error': str(e)}
