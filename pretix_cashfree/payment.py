import logging
import uuid
from cashfree_pg.api_client import Cashfree
from cashfree_pg.exceptions import NotFoundException
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
from phonenumbers import PhoneNumber
from pretix.base.models import Event, Order, OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.multidomain.urlreverse import build_absolute_uri

from .constants import (
    DEFAULT_CLIENT_KEY,
    DEFAULT_CLIENT_SECRET,
    REDIRECT_URL_QUERY_PARAM,
    RETURN_URL_QUERY_PARAM,
    SESSION_KEY_PAYMENT_ID,
    SUPPORTED_COUNTRY_CODES,
    SUPPORTED_CURRENCIES,
    X_API_VERSION,
)

logger = logging.getLogger("pretix.plugins.cashfree")


class CashfreePaymentProvider(BasePaymentProvider):
    identifier = "cashfree"
    verbose_name = _("Cashfree")
    public_name = _("Cashfree")

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "cashfree", event)

    # ---------------- SETTINGS ---------------- #

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

    # ---------------- INITIALIZATION ---------------- #

    def init_cashfree(self):
        """
        Configure Cashfree API credentials
        """
        Cashfree.XClientId = self.settings.client_id or DEFAULT_CLIENT_KEY
        Cashfree.XClientSecret = self.settings.client_secret or DEFAULT_CLIENT_SECRET
        Cashfree.XEnvironment = Cashfree.XSandbox

    # ---------------- HELPERS ---------------- #

    def _build_redirect_url(self, request: HttpRequest, session_id: str) -> str:
        return f"{build_absolute_uri(request.event, 'plugins:pretix_cashfree:redirect')}?{REDIRECT_URL_QUERY_PARAM}={session_id}"

    def _build_return_url(self, request: HttpRequest, payment: OrderPayment) -> str:
        return f"{build_absolute_uri(request.event, 'plugins:pretix_cashfree:return')}?{RETURN_URL_QUERY_PARAM}={payment.pk}"

    def _create_cashfree_order_request(
        self, request: HttpRequest, payment: OrderPayment
    ) -> CreateOrderRequest:
        phone: PhoneNumber = payment.order.phone

        if (
            not phone
            or phone.country_code not in SUPPORTED_COUNTRY_CODES
            or len(str(phone.national_number)) != 10
        ):
            messages.error(
                request,
                _(
                    f"Invalid phone number - {phone}. Please enter a valid Indian number with the country code (+91) followed by 10 digits."
                ),
            )
            raise Exception(
                "Phone number %s, is currently not supported by the Cashfree Python SDK",
                phone,
            )

        customer_details = CustomerDetails(
            customer_id=str(phone.national_number),
            customer_email=payment.order.email,
            customer_phone=str(phone.national_number),
        )

        return CreateOrderRequest(
            order_id=payment.order.full_code,
            order_amount=float(payment.amount),
            order_currency=self.event.currency,
            customer_details=customer_details,
            order_meta=OrderMeta(return_url=self._build_return_url(request, payment)),
            order_note=f"{request.event.name} tickets",
        )

    def _create_cashfree_order(self, request, payment):
        self.init_cashfree()
        request.session[SESSION_KEY_PAYMENT_ID] = payment.pk

        try:
            logger.debug("Creating Cashfree order for : %s", payment)
            create_order_request = self._create_cashfree_order_request(request, payment)
            api_response = Cashfree().PGCreateOrder(
                X_API_VERSION, create_order_request, str(uuid.uuid4())
            )

            if not api_response or not api_response.data:
                raise Exception("Cashfree order creation failed")

            return self._build_redirect_url(
                request, api_response.data.payment_session_id
            )

        except Exception as e:
            logger.exception("Error creating Cashfree order: %s", e)
            messages.error(
                request,
                _("There was an error creating the order. Please try again later."),
            )
            raise PaymentException from e

    def _handle_cashfree_order_status(self, payment, order_entity):
        match order_entity.order_status:
            case "ACTIVE":
                logger.debug("Order has no successful transaction yet")
            case "PAID":
                logger.debug("Order is PAID")
                if payment.amount == order_entity.order_amount:
                    payment.confirm()
                else:
                    raise PaymentException(f"{payment} - Amount mismatch with Cashfree")
            case "EXPIRED" | "TERMINATED":
                logger.debug("Order expired or terminated")
                payment.fail()
            case "TERMINATION_REQUESTED":
                logger.debug("Order termination requested")

    def _is_payment_confirmed(self, payment):
        return payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    # ---------------- PAYMENT FLOW ---------------- #

    def is_allowed(self, request: HttpRequest, total: Decimal = None) -> bool:
        return (
            super().is_allowed(request, total)
            and self.event.currency in SUPPORTED_CURRENCIES
        )

    def payment_is_valid_session(self, request):
        return True

    def execute_payment(self, request: HttpRequest, payment: OrderPayment):
        """
        Redirect to Cashfree to collect payment
        """
        next_page = super().execute_payment(request, payment)

        # If already confirmed, go to order details
        if self._is_payment_confirmed(payment):
            return next_page

        # First check existing payment status
        order_entity = self.verify_payment(request, payment)
        if order_entity:
            # If confirmed, go to order details. Otherwise redirect to Cashfree with existing payment_session_id
            return (
                next_page
                if self._is_payment_confirmed(payment)
                else self._build_redirect_url(request, order_entity.payment_session_id)
            )

        # Otherwise create a new Cashfree order and redirect
        return self._create_cashfree_order(request, payment)

    def verify_payment(self, request: HttpRequest, payment: OrderPayment):
        """
        Verify existing Cashfree order status and update payment accordingly
        """
        self.init_cashfree()
        order_id = payment.order.full_code

        try:
            logger.debug("Fetching Cashfree order for pretix order: %s", order_id)
            api_response = Cashfree().PGFetchOrder(X_API_VERSION, order_id)
            order_entity: OrderEntity = api_response.data

            logger.debug("Order Entity from Cashfree: %s", order_entity)
            self._handle_cashfree_order_status(payment, order_entity)

            return order_entity

        except NotFoundException:
            logger.debug("Cashfree order not found for payment: %s", payment)
            return None
        except Exception as e:
            logger.exception(
                "Error occured while fetching Cashfree order having id: %s", order_id
            )
            raise PaymentException from e

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order = None, info_data: dict = None
    ):
        return mark_safe(
            "<p>You will be redirected to Cashfree to make the payment</p>"
        )
