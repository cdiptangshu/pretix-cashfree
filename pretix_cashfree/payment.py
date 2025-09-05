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
from django.urls import reverse
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from phonenumbers import PhoneNumber
from pretix.base.models import Event, Order, OrderPayment
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri
from urllib.parse import urlencode

from .constants import (
    REDIRECT_URL_PAYMENT_SESSION_ID,
    RETURN_URL_PARAM,
    SANDBOX_CLIENT_KEY,
    SANDBOX_CLIENT_SECRET,
    SESSION_KEY_ORDER_ID,
    SUPPORTED_COUNTRY_CODES,
    SUPPORTED_CURRENCIES,
    X_API_VERSION,
)
from .models import ReferencedCashfreeObject

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
            (
                "debug_tunnel",
                forms.URLField(
                    label=_("Debug Webhook Tunnel"),
                    required=False,
                    help_text=_(
                        "If provided, this will be used as the base URL for notify webhook"
                    ),
                ),
            ),
        ]

        return OrderedDict(list(super().settings_form_fields.items()) + fields)

    # ---------------- INITIALIZATION ---------------- #

    def init_cashfree(self):
        """
        Configure Cashfree API credentials
        """
        logger.debug("Initializing Cashfree")
        is_sandbox = self.event.testmode
        Cashfree.XClientId = (
            SANDBOX_CLIENT_KEY if is_sandbox else self.settings.client_id
        )
        Cashfree.XClientSecret = (
            SANDBOX_CLIENT_SECRET if is_sandbox else self.settings.client_secret
        )
        Cashfree.XEnvironment = (
            Cashfree.XSandbox if is_sandbox else Cashfree.XProduction
        )

    # ---------------- HELPERS ---------------- #

    def _build_redirect_url(self, request: HttpRequest, session_id: str) -> str:
        base_url = build_absolute_uri(request.event, "plugins:pretix_cashfree:redirect")
        query = urlencode({REDIRECT_URL_PAYMENT_SESSION_ID: session_id})
        return f"{base_url}?{query}"

    def _build_return_url(self, request: HttpRequest, order_id: str) -> str:
        base_url = build_absolute_uri(request.event, "plugins:pretix_cashfree:return")
        query = urlencode({RETURN_URL_PARAM: order_id})
        return f"{base_url}?{query}"

    def _build_notify_url(self, request: HttpRequest) -> str:
        return (
            f"{self.settings.debug_tunnel}{reverse('plugins:pretix_cashfree:webhook')}"
            if self.settings.debug_tunnel
            else build_global_uri("plugins:pretix_cashfree:webhook")
        )

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

        customer_phone = str(phone.national_number)
        customer_details = CustomerDetails(
            customer_id=customer_phone,
            customer_email=payment.order.email,
            customer_phone=customer_phone,
        )

        order_id = payment.order.full_code
        return CreateOrderRequest(
            order_id=order_id,
            order_amount=float(payment.amount),
            order_currency=self.event.currency,
            customer_details=customer_details,
            order_meta=OrderMeta(
                return_url=self._build_return_url(request, order_id),
                notify_url=self._build_notify_url(request),
            ),
            order_note=f"{request.event.name} tickets",
        )

    def _create_cashfree_order(self, request, payment: OrderPayment):
        self.init_cashfree()

        try:
            logger.debug("Creating Cashfree order for : %s", payment)
            create_order_request = self._create_cashfree_order_request(request, payment)

            api_response = Cashfree().PGCreateOrder(
                X_API_VERSION, create_order_request, str(uuid.uuid4())
            )

            if not api_response or not api_response.data:
                raise Exception("Cashfree order creation failed")

            return self._redirect_cashfree(request, payment, api_response.data)

        except Exception as e:
            logger.exception("Error creating Cashfree order: %s", e)
            messages.error(
                request,
                _("There was an error creating the order. Please try again later."),
            )
            raise PaymentException from e

    def _redirect_cashfree(
        self, request: HttpRequest, payment: OrderPayment, order_entity: OrderEntity
    ):
        logger.debug("Redirecting to Cashfree for payment: %s", payment)
        request.session[SESSION_KEY_ORDER_ID] = order_entity.order_id
        ReferencedCashfreeObject.objects.update_or_create(
            reference=order_entity.order_id, defaults={"payment": payment}
        )
        return self._build_redirect_url(request, order_entity.payment_session_id)

    def _handle_cashfree_order_status(
        self, payment: OrderPayment, order_entity: OrderEntity
    ):
        match order_entity.order_status:
            case "ACTIVE":
                logger.debug("Order has no successful transaction yet")
            case "PAID":
                logger.debug("%s is PAID", payment)
                if payment.amount == order_entity.order_amount:
                    payment.confirm()
                else:
                    raise PaymentException(f"{payment} - Amount mismatch with Cashfree")
            case "EXPIRED" | "TERMINATED":
                logger.debug("%s expired or terminated", payment)
                payment.fail()
            case "TERMINATION_REQUESTED":
                logger.debug("%s termination requested", payment)

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
        order_entity = self.verify_payment(payment)
        if order_entity:
            # If confirmed, go to order details. Otherwise redirect to Cashfree with existing payment_session_id
            return (
                next_page
                if self._is_payment_confirmed(payment)
                else self._redirect_cashfree(request, payment, order_entity)
            )

        # Otherwise create a new Cashfree order and redirect
        return self._create_cashfree_order(request, payment)

    def verify_payment(self, payment: OrderPayment):
        """
        Verify existing Cashfree order status and update payment accordingly
        """
        self.init_cashfree()
        order_id = payment.order.full_code

        try:
            logger.debug("Fetching Cashfree order for pretix order: %s", order_id)
            api_response = Cashfree().PGFetchOrder(X_API_VERSION, order_id)
            order_entity: OrderEntity = api_response.data
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

    def handle_webhook(self, raw_payload, signature, timestamp, payment: OrderPayment):
        """
        Verifies the signature, performs idempotency check, and processes the payment webhook payload
        """
        self.init_cashfree()
        logger.debug("Verifying webhook signature")

        # Verify signature
        try:
            webhook_response = Cashfree().PGVerifyWebhookSignature(
                signature=signature, timestamp=timestamp, rawBody=raw_payload
            )
        except Exception as e:
            logger.warning("Failed to verify webhook signature: %s", e)
            raise

        # TODO check idempotency using cf_payment_id

        if webhook_response.type != "PAYMENT_SUCCESS_WEBHOOK":
            logger.debug("webhook type is %s, skipping", webhook_response.type)
            return

        # TODO process the webhook response
        self.verify_payment(payment)

        # process_webhook.apply_async(args=[webhook_response.object])

    # ---------------- RENDERING ---------------- #

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order = None, info_data: dict = None
    ):
        return mark_safe(
            "<p>You will be redirected to Cashfree to make the payment</p>"
        )

    def test_mode_message(self):
        return mark_safe(
            _(
                "The Cashfree plugin is operating in test mode. You can use one of <a {args}>many payment modes</a> "
                "to perform a transaction. No money will actually be transferred."
            ).format(
                args='href="https://www.cashfree.com/docs/payments/online/resources/sandbox-environment#sandbox-environment" target="_blank"'
            )
        )
