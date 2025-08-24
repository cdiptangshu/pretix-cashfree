import logging
from django.contrib import messages
from django.shortcuts import render
from django.utils.translation import gettext_lazy as _
from django.views.decorators.clickjacking import xframe_options_exempt
from pretix.base.models import Order, OrderPayment
from pretix.base.payment import PaymentException
from pretix.helpers.http import redirect_to_url
from pretix.multidomain.urlreverse import eventreverse

from .payment import CashfreePaymentProvider

logger = logging.getLogger("pretix.plugins.cashfree")


@xframe_options_exempt
def redirect_view(request, *args, **kwargs):
    pid = request.GET.get("pid", "")

    r = render(
        request,
        "pretix_cashfree/redirect.html",
        {
            "payment_session_id": pid,
        },
    )
    r._csp_ignore = True
    return r


def success(request, *args, **kwargs):
    urlkwargs = {}
    if "cart_namespace" in kwargs:
        urlkwargs["cart_namespace"] = kwargs["cart_namespace"]

    order_id = request.GET.get("oid", "")

    logger.debug("orderId: %s", order_id)

    # TODO how to store the payment_cashfree_payment in session from the checkout_prepare() method?
    if request.session.get("payment_cashfree_payment"):
        payment = OrderPayment.objects.get(
            pk=request.session.get("payment_cashfree_payment")
        )
    else:
        payment = None

    if order_id == request.session.get("payment_cashfree_oid", None):
        if payment:
            prov = CashfreePaymentProvider(request.event)
            try:
                resp = prov.execute_payment(request, payment)
            except PaymentException as e:
                messages.error(request, str(e))
                urlkwargs["step"] = "payment"
                return redirect_to_url(
                    eventreverse(
                        request.event, "presale:event.checkout", kwargs=urlkwargs
                    )
                )
            if resp:
                return resp
    else:
        messages.error(request, _("Invalid response from Cashfree received."))
        logger.error("Session did not contain payment_cashfree_oid")
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
