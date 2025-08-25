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

from .constants import SESSION_KEY_ORDER_ID, SESSION_KEY_PAYMENT_ID
from .utils import sanitize_phone

SUPPORTED_CURRENCIES = ["INR"]

logger = logging.getLogger("pretix.plugins.cashfree")
x_api_version = "2023-08-01"
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

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):

        self.init_cashfree()

        payment_id = payment.pk
        order_id = payment.order.full_code
        customer_phone = sanitize_phone(payment.order.phone)
        customer_details = CustomerDetails(
            customer_id=customer_phone,
            customer_email=payment.order.email,
            customer_phone=customer_phone,
        )
        return_url = f"{build_absolute_uri(request.event, 'plugins:pretix_cashfree:return')}?pid={payment_id}"
        create_order_request = CreateOrderRequest(
            order_id=order_id,
            order_amount=float(payment.amount),
            order_currency=self.event.currency,
            customer_details=customer_details,
            order_meta=OrderMeta(return_url=return_url),
            order_note=_("{event} tickets").format(event=request.event.name),
        )
        x_request_id = str(uuid.uuid4())

        request.session[SESSION_KEY_PAYMENT_ID] = payment_id
        request.session[SESSION_KEY_ORDER_ID] = order_id

        try:
            api_response = Cashfree().PGCreateOrder(
                x_api_version, create_order_request, x_request_id
            )
            if not (api_response and api_response.data):
                raise Exception("Cashfree order creation failed")
            order_entity: OrderEntity = api_response.data
            payment_session_id = order_entity.payment_session_id
            return f"{build_absolute_uri(request.event, 'plugins:pretix_cashfree:redirect')}?payment_session_id={payment_session_id}"

        except Exception as e:
            logger.exception(e)
            messages.error(
                request,
                _("There was an error creating the order. Please try again later."),
            )
            return super().execute_payment(request, payment)

    def verify_payment(self, request: HttpRequest, payment: OrderPayment):
        self.init_cashfree()

        order_id = payment.order.full_code
        if order_id != request.session[SESSION_KEY_ORDER_ID]:
            raise PaymentException(
                f"Order id did not match with the one stored in session under key '{SESSION_KEY_ORDER_ID}'"
            )

        try:
            api_response = Cashfree().PGFetchOrder(x_api_version, order_id)
            order_entity: OrderEntity = api_response.data

            # FIXME Code not correct. Handle exceptions correctly. Also, handle ACTIVE
            logger.debug(f"Order Entity from Cashfree: {order_entity}")
            match order_entity.order_status:
                case "PAID":
                    if payment.amount == order_entity.order_amount:
                        payment.confirm()
                    else:
                        raise PaymentException(
                            f"{payment} - Payment amount and order amount did not match"
                        )
                case _:
                    raise PaymentException(
                        f"{payment} - Cashfree order status {order_entity.order_status} is not handled."
                    )

        except Exception as e:
            raise PaymentException(
                f"{payment} - Failed to fetch order details from Cashfree for 'order_id': {order_id}"
            ) from e

    def payment_prepare(self, request: HttpRequest, payment: OrderPayment):
        logger.debug("in payment_prepare")

        # TODO Redirect to Cashfree
        logger.debug("payment full id: %s, pk: %s", payment.full_id, payment.pk)
        logger.debug("order id: %s", payment.order.full_code)

        return super().payment_prepare(request, payment)

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order = None, info_data: dict = None
    ):
        return mark_safe("<p>Cashfree</p>")
