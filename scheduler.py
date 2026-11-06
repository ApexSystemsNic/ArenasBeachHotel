"""Runs two automated reports and wires them to cloud-folder sync + email
delivery:
  - Checkout/payment report, every REPORT_SCHEDULE_DAYS days (default 15)
  - Inventory consumption report, every INVENTORY_REPORT_SCHEDULE_DAYS days (default 7)
"""
import os
import logging
from datetime import datetime, timedelta
from apscheduler.schedulers.background import BackgroundScheduler

import database as db
import report_export
import delivery
import backup

logger = logging.getLogger('scheduler')

REPORT_SCHEDULE_DAYS = int(os.environ.get('REPORT_SCHEDULE_DAYS', '15'))
INVENTORY_REPORT_SCHEDULE_DAYS = int(os.environ.get('INVENTORY_REPORT_SCHEDULE_DAYS', '7'))
INCREMENTAL_BACKUP_DAYS = int(os.environ.get('INCREMENTAL_BACKUP_DAYS', '2'))
FULL_BACKUP_DAYS = int(os.environ.get('FULL_BACKUP_DAYS', '7'))

_scheduler = None


def _resolve_period(schedule_days, start_date, end_date):
    if end_date is None:
        end_date = datetime.now().date()
    else:
        end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
    if start_date is None:
        start_date = end_date - timedelta(days=schedule_days)
    else:
        start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
    return start_date.isoformat(), end_date.isoformat()


def run_periodic_report(triggered_by='scheduled', start_date=None, end_date=None):
    """Checkout/payment report for the given period (defaults to the last
    REPORT_SCHEDULE_DAYS days), delivered and logged."""
    start_str, end_str = _resolve_period(REPORT_SCHEDULE_DAYS, start_date, end_date)

    result = report_export.generate_report(start_str, end_str)

    cloud_result = delivery.sync_to_cloud_folder(result['file_path'])
    email_result = delivery.send_report_email(result['file_path'], f'{start_str} to {end_str}')

    run = db.create_report_run(
        report_type='checkout_payment',
        period_start=start_str,
        period_end=end_str,
        file_name=result['file_name'],
        file_path=result['file_path'],
        cloud_synced=cloud_result.get('synced', False),
        cloud_path=cloud_result.get('target_path'),
        emailed=email_result.get('sent', False),
        email_error=email_result.get('error'),
        triggered_by=triggered_by,
    )

    logger.info(
        'Checkout/payment report run %s: %s checkouts, %s orders, cloud_synced=%s, emailed=%s',
        run['id'], result['checkouts_count'], result['orders_count'],
        cloud_result.get('synced'), email_result.get('sent'),
    )

    return {**run, 'report_summary': result, 'cloud_result': cloud_result, 'email_result': email_result}


def run_inventory_report(triggered_by='scheduled', start_date=None, end_date=None):
    """Stock consumption report for the given period (defaults to the last
    INVENTORY_REPORT_SCHEDULE_DAYS days), delivered and logged."""
    start_str, end_str = _resolve_period(INVENTORY_REPORT_SCHEDULE_DAYS, start_date, end_date)

    result = report_export.generate_inventory_report(start_str, end_str)

    cloud_result = delivery.sync_to_cloud_folder(result['file_path'])
    email_result = delivery.send_report_email(result['file_path'], f'{start_str} to {end_str} (inventory)')

    run = db.create_report_run(
        report_type='inventory',
        period_start=start_str,
        period_end=end_str,
        file_name=result['file_name'],
        file_path=result['file_path'],
        cloud_synced=cloud_result.get('synced', False),
        cloud_path=cloud_result.get('target_path'),
        emailed=email_result.get('sent', False),
        email_error=email_result.get('error'),
        triggered_by=triggered_by,
    )

    logger.info(
        'Inventory report run %s: %s items consumed, %s low stock, cloud_synced=%s, emailed=%s',
        run['id'], result['items_consumed'], result['low_stock_count'],
        cloud_result.get('synced'), email_result.get('sent'),
    )

    return {**run, 'report_summary': result, 'cloud_result': cloud_result, 'email_result': email_result}


def run_incremental_backup(triggered_by='scheduled'):
    result = backup.create_backup('incremental', triggered_by=triggered_by)
    logger.info('Incremental backup %s: %s bytes, cloud_synced=%s',
                result['id'], result['file_size_bytes'], result.get('cloud_result', {}).get('synced'))
    return result


def run_full_backup(triggered_by='scheduled'):
    result = backup.create_backup('full', triggered_by=triggered_by)
    logger.info('Full backup %s: %s bytes, cloud_synced=%s',
                result['id'], result['file_size_bytes'], result.get('cloud_result', {}).get('synced'))
    return result


def init_scheduler():
    """Call this only once per real running process — app.py guards against
    the Flask debug reloader's duplicate parent process before calling it."""
    global _scheduler
    if _scheduler is not None:
        return _scheduler

    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        run_periodic_report,
        trigger='interval',
        days=REPORT_SCHEDULE_DAYS,
        id='periodic_checkout_report',
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(days=REPORT_SCHEDULE_DAYS),
    )
    _scheduler.add_job(
        run_inventory_report,
        trigger='interval',
        days=INVENTORY_REPORT_SCHEDULE_DAYS,
        id='periodic_inventory_report',
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(days=INVENTORY_REPORT_SCHEDULE_DAYS),
    )
    _scheduler.add_job(
        run_incremental_backup,
        trigger='interval',
        days=INCREMENTAL_BACKUP_DAYS,
        id='incremental_backup',
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(days=INCREMENTAL_BACKUP_DAYS),
    )
    _scheduler.add_job(
        run_full_backup,
        trigger='interval',
        days=FULL_BACKUP_DAYS,
        id='full_backup',
        replace_existing=True,
        next_run_time=datetime.now() + timedelta(days=FULL_BACKUP_DAYS),
    )
    _scheduler.start()
    logger.info(
        'Scheduler started: checkout report every %s days, inventory report every %s days, '
        'incremental backup every %s days, full backup every %s days',
        REPORT_SCHEDULE_DAYS, INVENTORY_REPORT_SCHEDULE_DAYS, INCREMENTAL_BACKUP_DAYS, FULL_BACKUP_DAYS,
    )
    return _scheduler


def next_run_time(job_id='periodic_checkout_report'):
    if _scheduler is None:
        return None
    job = _scheduler.get_job(job_id)
    return job.next_run_time.isoformat() if job and job.next_run_time else None
