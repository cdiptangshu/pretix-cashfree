import logging
import uuid
from cashfree_pg.api_client import Cashfree
from cashfree_pg.models.create_order_request import CreateOrderRequest
from cashfree_pg.models.customer_details import CustomerDetails
from cashfree_pg.models.order_entity import OrderEntity
from cashfree_pg.models.order_meta import OrderMeta
from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.http import HttpRequest
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from pretix.base.models import Event, Order, OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri
from pretix.presale.views.cart import cart_session

from .utils import sanitize_phone

SUPPORTED_CURRENCIES = ["INR"]

logger = logging.getLogger("pretix.plugins.cashfree")
cashfree_api_version = "2023-08-01"
default_client_key = "TEST430329ae80e0f32e41a393d78b923034"
default_client_secret = "TESTaf195616268bd6202eeb3bf8dc458956e7192a85"


class CashfreePaymentProvider(BasePaymentProvider):
    identifier = "cashfree"
    verbose_name = _("Cashfree")
    public_name = _("Cashfree")

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "cashfree", event)

    @property
    def settings_form_fields(self):
        fields = [
            (
                "client_id",
                forms.CharField(
                    label=_("Client ID"),
                    required=False,
                ),
            ),
            (
                "client_secret",
                forms.CharField(
                    label=_("Client Secret"),
                    required=False,
                    widget=forms.PasswordInput(render_value=True),
                ),
            ),
        ]

        return OrderedDict(list(super().settings_form_fields.items()) + fields)

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return (
            super().is_allowed(request, total)
            and self.event.currency in SUPPORTED_CURRENCIES
        )

    def init_cashfree(self):
        Cashfree.XClientId = self.settings.client_id
        Cashfree.XClientSecret = self.settings.client_secret
        Cashfree.XEnvironment = Cashfree.XSandbox

    def payment_form_render(self, request, total, order=None):
        logger.debug("User has selected Cashfree payment method to pay: %s", total)
        return super().payment_form_render(request, total, order)

    def payment_is_valid_session(self, request):
        return True

    def checkout_prepare(self, request, cart):
        logger.debug("in checkout_prepare")

        # Create Cashfree Order
        self.init_cashfree()

        kwargs = {}
        if request.resolver_match and "cart_namespace" in request.resolver_match.kwargs:
            kwargs["cart_namespace"] = request.resolver_match.kwargs["cart_namespace"]

        order_id = str(uuid.uuid4())

        # Store the payment session id in session
        request.session["payment_cashfree_oid"] = order_id

        return_url = build_absolute_uri(
            request.event, "plugins:pretix_cashfree:return", kwargs=kwargs
        ) + "?oid={}".format(order_id)

        create_order_request = CreateOrderRequest(
            order_id=order_id,
            order_amount=float(cart.get("total")),
            order_currency=self.event.currency,
            customer_details=self.create_customer_details(request),
            order_meta=OrderMeta(return_url=return_url),
            order_note=_("Event tickets for {event}").format(event=request.event.name),
        )

        try:
            api_response = Cashfree().PGCreateOrder(
                cashfree_api_version, create_order_request, None, None
            )
            if api_response.data is None:
                raise Exception("OrderEntity missing in Cashfree API response")

            logger.debug(type(api_response.data))
            order_entity: OrderEntity = api_response.data
            payment_session_id = order_entity.payment_session_id

            # Redirect to Cashfree Hosted Checkout
            return build_absolute_uri(
                request.event, "plugins:pretix_cashfree:redirect", kwargs=kwargs
            ) + "?pid={}".format(payment_session_id)

        except Exception as e:
            logger.exception("Error occured: %s", e)
            messages.error(
                request,
                _("There was an error creating the order. Please try again later."),
            )
            return False

    def create_customer_details(self, request):

        cs = cart_session(request)

        customer_email = cs.get("email")
        customer_phone = sanitize_phone(cs.get("contact_form_data", {}).get("phone"))

        customer_details = CustomerDetails(
            customer_id=customer_phone,
            customer_phone=customer_phone,
            customer_email=customer_email,
        )

        return customer_details

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order = None, info_data: dict = None
    ):
        return mark_safe("<p>Cashfree</p>")

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        logger.debug("Inside execute_payment")

        order_id = request.session.get("payment_cashfree_oid", "")

        if order_id == "":
            raise PaymentException(
                _(
                    "We were unable to process your payment. See below for details on how to proceed."
                )
            )

        self.init_cashfree()

        try:

            api_response = Cashfree().PGFetchOrder(cashfree_api_version, order_id)
            order_entity: OrderEntity = api_response.data

            logger.debug("order_entity: %s", order_entity)

            # TODO Just for testing, a lot of checks need to happen!
            order_status = order_entity.order_status
            if order_status == "PAID":
                payment.confirm()
            else:
                logger.debug("Order Status not handled: %s", order_status)

        except Exception as e:
            logger.exception("Error occured: %s", e)
            messages.error(
                request,
                _(
                    "There was an error verifying the payment. Please check the payment status again."
                ),
            )
            return False

        # payment.confirm()
        return None

    def payment_pending_render(self, request, payment):
        return super().payment_pending_render(request, payment)

    def order_pending_mail_render(self, order, payment):
        return super().order_pending_mail_render(order, payment)
