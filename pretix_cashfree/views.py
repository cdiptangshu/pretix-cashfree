import json
import logging
from django.contrib import messages
from django.http import HttpRequest, HttpResponse
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST
from django_scopes import scopes_disabled
from pretix.base.models import Order
from pretix.helpers.http import redirect_to_url
from pretix.multidomain.urlreverse import eventreverse

from .constants import (
    REDIRECT_URL_MODE,
    REDIRECT_URL_PAYMENT_SESSION_ID,
    RETURN_URL_PARAM,
    SESSION_KEY_ORDER_ID,
    WEBHOOK_TYPE_PAYMENT,
)
from .models import PaymentAttempt
from .payment import CashfreePaymentProvider

logger = logging.getLogger("pretix.plugins.cashfree")


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    payment_session_id = request.GET.get(REDIRECT_URL_PAYMENT_SESSION_ID, "")

    r = render(
        request,
        "pretix_cashfree/redirect.html",
        {
            REDIRECT_URL_PAYMENT_SESSION_ID: payment_session_id,
            REDIRECT_URL_MODE: (
                "sandbox" if request.event and request.event.testmode else "production"
            ),
        },
    )
    r._csp_ignore = True
    return r


def return_view(request, *args, **kwargs):
    urlkwargs = {}
    if "cart_namespace" in kwargs:
        urlkwargs["cart_namespace"] = kwargs["cart_namespace"]

    order_id = request.GET.get(RETURN_URL_PARAM, "")

    if request.session.get(SESSION_KEY_ORDER_ID):
        cashfree_object = PaymentAttempt.objects.get(
            reference=request.session.get(SESSION_KEY_ORDER_ID)
        )
        payment = cashfree_object.payment
    else:
        payment = None

    if order_id == str(request.session.get(SESSION_KEY_ORDER_ID, None)):
        if payment:
            prov = CashfreePaymentProvider(request.event)

            if not prov.verify_payment(payment):
                logger.error("Failed to process payment with id: %s", order_id)
                messages.error(
                    request,
                    _(
                        "Your payment could not be verified. Please contact support for help."
                    ),
                )
                urlkwargs["step"] = "payment"
                return redirect_to_url(
                    eventreverse(
                        request.event, "presale:event.checkout", kwargs=urlkwargs
                    )
                )
    else:
        messages.error(request, _("Invalid response received from Cashfree"))
        logger.error(
            "The payment_id received from Cashfree does not match the one stored in the session under key '%s'",
            SESSION_KEY_ORDER_ID,
        )
        urlkwargs["step"] = "payment"
        return redirect_to_url(
            eventreverse(request.event, "presale:event.checkout", kwargs=urlkwargs)
        )

    if payment:
        return redirect_to_url(
            eventreverse(
                request.event,
                "presale:event.order",
                kwargs={"order": payment.order.code, "secret": payment.order.secret},
            )
            + ("?paid=yes" if payment.order.status == Order.STATUS_PAID else "")
        )
    else:
        urlkwargs["step"] = "confirm"
        return redirect_to_url(
            eventreverse(request.event, "presale:event.checkout", kwargs=urlkwargs)
        )


@csrf_exempt
@require_POST
@scopes_disabled()
def webhook_view(request: HttpRequest, *args, **kwargs):

    try:
        event_body = request.body.decode("utf-8").strip()
        event_json = json.loads(event_body)
        timestamp = request.headers["x-webhook-timestamp"]
        signature = request.headers["x-webhook-signature"]
    except Exception as e:
        logger.exception("Failed to parse webhook payload: %s", e)
        return HttpResponse(status=400)

    order_id = str(event_json["data"]["order"]["order_id"])
    type = event_json["type"]

    if type != WEBHOOK_TYPE_PAYMENT:
        logger.debug("webhook type: %s, aborting", type)
        return HttpResponse(status=200)

    if not order_id:
        logger.warning("Webhook payloads missing order_id: %s", event_json)
        return HttpResponse(status=400)

    try:
        cashfree_object = PaymentAttempt.objects.get(reference=order_id)
        prov = CashfreePaymentProvider(cashfree_object.payment.order.event)

    except PaymentAttempt.DoesNotExist:
        logger.warning("No ReferencedCashfreeObject found for order_id=%s", order_id)
        return HttpResponse(status=404)
    except Exception as e:
        logger.exception("Error while fetching ReferencedCashfreeObject: %s", e)
        return HttpResponse(status=500)

    try:
        logger.debug("Handling webhook for payment: %s", cashfree_object.payment)
        prov.handle_webhook(
            raw_payload=event_body,
            signature=signature,
            timestamp=timestamp,
            payment=cashfree_object.payment,
        )
    except Exception as e:
        logger.warning("Error occured while processing webhook: %s", e)
        return HttpResponse(status=404)

    return HttpResponse(status=200)
