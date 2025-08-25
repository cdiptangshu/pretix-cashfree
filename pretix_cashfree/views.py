import logging
from django.contrib import messages
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from pretix.helpers.http import redirect_to_url
from pretix.multidomain.urlreverse import eventreverse

from .constants import SESSION_KEY_PAYMENT_ID
from .payment import CashfreePaymentProvider

logger = logging.getLogger("pretix.plugins.cashfree")


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    payment_session_id = request.GET.get("payment_session_id", "")

    r = render(
        request,
        "pretix_cashfree/redirect.html",
        {
            "payment_session_id": payment_session_id,
        },
    )
    r._csp_ignore = True
    return r


def success(request, *args, **kwargs):
    urlkwargs = {}
    if "cart_namespace" in kwargs:
        urlkwargs["cart_namespace"] = kwargs["cart_namespace"]

    payment_id = request.GET.get("pid", "")

    if request.session.get(SESSION_KEY_PAYMENT_ID):
        payment = OrderPayment.objects.get(
            pk=request.session.get(SESSION_KEY_PAYMENT_ID)
        )
    else:
        payment = None

    if payment_id == str(request.session.get(SESSION_KEY_PAYMENT_ID, None)):
        if payment:
            prov = CashfreePaymentProvider(request.event)

            try:
                prov.verify_payment(request, payment)
            except PaymentException as e:
                logger.exception(e)
                messages.error(request, str(e))
                urlkwargs["step"] = "payment"
                return redirect_to_url(
                    eventreverse(
                        request.event, "presale:event.checkout", kwargs=urlkwargs
                    )
                )
    else:
        messages.error(request, _("Invalid response received from Cashfree"))
        logger.error(
            f"The payment_id received from Cashfree does not match the one stored in the session under key '{SESSION_KEY_PAYMENT_ID}'"
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


def abort(request, *args, **kwargs):
    raise Exception("Not implemented yet!")
