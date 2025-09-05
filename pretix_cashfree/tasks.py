import logging
from pretix.celery_app import app

logger = logging.getLogger("pretix.plugins.cashfree")


@app.task
def process_webhook(payload):
    logger.debug("handle webhook - payload: %s", payload)
