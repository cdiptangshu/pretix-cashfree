import logging
from cashfree_pg.api_client import Cashfree, PGWebhookEvent
from cashfree_pg.exceptions import NotFoundException
from cashfree_pg.models.create_order_request import CreateOrderRequest
from cashfree_pg.models.customer_details import CustomerDetails
from cashfree_pg.models.order_create_refund_request import OrderCreateRefundRequest
from cashfree_pg.models.order_entity import OrderEntity
from cashfree_pg.models.order_meta import OrderMeta
from cashfree_pg.models.refund_entity import RefundEntity
from collections import OrderedDict
from datetime import datetime
from decimal import Decimal
from django import forms
from django.contrib import messages
from django.core.cache import caches
from django.http import HttpRequest
from django.template.loader import get_template
from django.urls import reverse
from django.utils.formats import date_format
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _
from phonenumber_field.formfields import PhoneNumberField
from phonenumber_field.phonenumber import PhoneNumber
from pretix.base.forms.questions import (
    WrappedPhoneNumberPrefixWidget,
    guess_phone_prefix_from_request,
)
from pretix.base.models import Event, Order, OrderPayment, OrderRefund
from pretix.base.payment import BasePaymentProvider, PaymentException
from pretix.base.settings import SettingsSandbox
from pretix.base.templatetags.rich_text import rich_text
from pretix.helpers.urls import build_absolute_uri as build_global_uri
from pretix.multidomain.urlreverse import build_absolute_uri
from urllib.parse import urlencode

from .constants import (
    DATE_FORMAT,
    PAYMENT_STATUS_SUCCESS,
    REDIRECT_URL_PAYMENT_SESSION_ID,
    RETURN_URL_PARAM,
    SESSION_KEY_ORDER_ID,
    SUPPORTED_COUNTRY_CODES,
    SUPPORTED_CURRENCIES,
    X_API_VERSION,
)
from .models import (
    CashfreePaymentInfo,
    CashfreeRefundInfo,
    PaymentAttempt,
)
from .utils import create_request_id

logger = logging.getLogger("pretix.plugins.cashfree")

try:
    cache = caches["redis"]
except Exception:
    cache = caches["default"]


class CashfreePaymentProvider(BasePaymentProvider):
    identifier = "cashfree"
    verbose_name = "Cashfree"
    public_name = "Cashfree"

    def __init__(self, event: Event):
        super().__init__(event)
        self.settings = SettingsSandbox("payment", "cashfree", event)
        self.init_cashfree()

    @property
    def settings_form_fields(self):
        fields = [
            (
                "client_id",
                forms.CharField(
                    label=_("Client ID"),
                    required=True,
                ),
            ),
            (
                "client_secret",
                forms.CharField(
                    label=_("Client Secret"),
                    required=True,
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

    @property
    def payment_phone_session_key(self):
        return f"payment_{self.identifier}_phone"

    def init_cashfree(self):
        """
        Configure Cashfree API credentials
        """
        is_sandbox = self.event.testmode
        Cashfree.XClientId = self.settings.client_id
        Cashfree.XClientSecret = self.settings.client_secret
        Cashfree.XEnvironment = (
            Cashfree.XSandbox if is_sandbox else Cashfree.XProduction
        )

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

        phone: PhoneNumber = request.session[self.payment_phone_session_key]

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

        try:
            logger.debug("Creating Cashfree order for : %s", payment)
            create_order_request = self._create_cashfree_order_request(request, payment)

            x_request_id = create_request_id()
            api_response = Cashfree().PGCreateOrder(
                x_api_version=X_API_VERSION,
                create_order_request=create_order_request,
                x_request_id=x_request_id,
            )

            if not api_response or not api_response.data:
                raise Exception("Did not receive order details")

            order_entity = api_response.data
            payment.info_data = self._create_payment_info(x_request_id, order_entity)
            payment.save()
            return self._redirect_cashfree(request, payment, order_entity)

        except Exception as e:
            logger.exception("Error creating Cashfree order: %s", e)
            messages.error(
                request,
                _("There was an error creating the order. Please try again later."),
            )
            raise PaymentException from e

    def _create_payment_info(self, x_request_id: str, order_entity: OrderEntity):
        local_dt = datetime.now()
        updated_at = date_format(local_dt, DATE_FORMAT)
        obj = CashfreePaymentInfo(
            x_request_id=x_request_id,
            order_id=order_entity.order_id,
            cf_order_id=order_entity.cf_order_id,
            order_status=order_entity.order_status,
            order_currency=order_entity.order_currency,
            order_amount=order_entity.order_amount,
            customer_id=order_entity.customer_details.customer_id,
            updated_at=updated_at,
        )
        return obj.dict()

    def _create_refund_info(self, x_request_id: str, refund_entity: RefundEntity):
        local_dt = datetime.now()
        updated_at = date_format(local_dt, DATE_FORMAT)
        date = (
            date_format(refund_entity.processed_at, DATE_FORMAT)
            if refund_entity.processed_at
            else None
        )
        logger.debug("processed at date: %s", date)
        obj = CashfreeRefundInfo(
            x_request_id=x_request_id,
            order_id=refund_entity.order_id,
            refund_id=refund_entity.refund_id,
            cf_refund_id=refund_entity.cf_refund_id,
            cf_payment_id=refund_entity.cf_payment_id,
            refund_type=refund_entity.refund_type,
            refund_status=refund_entity.refund_status,
            refund_amount=refund_entity.refund_amount,
            refund_currency=refund_entity.refund_currency,
            processed_at=refund_entity.processed_at,
            updated_at=updated_at,
        )
        return obj.dict()

    def _redirect_cashfree(
        self, request: HttpRequest, payment: OrderPayment, order_entity: OrderEntity
    ):
        logger.debug("Redirecting to Cashfree for payment: %s", payment)
        request.session[SESSION_KEY_ORDER_ID] = order_entity.order_id
        PaymentAttempt.objects.update_or_create(
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
                    logger.error(f"{payment} - Amount mismatch with Cashfree")
                    payment.fail()
            case "EXPIRED" | "TERMINATED":
                logger.debug("%s expired or terminated", payment)
                payment.fail()
            case "TERMINATION_REQUESTED":
                logger.debug("%s termination requested", payment)

    def _is_payment_confirmed(self, payment):
        return payment.state == OrderPayment.PAYMENT_STATE_CONFIRMED

    def _verify_webhook_signature(self, signature, timestamp, raw_payload):
        try:
            webhook_response = Cashfree().PGVerifyWebhookSignature(
                signature=signature, timestamp=timestamp, rawBody=raw_payload
            )
        except Exception as e:
            logger.Error("Error: %s", e)
            return None

        return webhook_response

    def _check_webhook_payload(
        self, payment: OrderPayment, webhook_event: PGWebhookEvent
    ):
        # Check idempotency using cf_payment_id
        cf_payment_obj = webhook_event.object["data"]["payment"]
        cf_payment_id = str(cf_payment_obj["cf_payment_id"])
        payment_status = cf_payment_obj["payment_status"]

        key = f"plugins:pretix_cachfree:webhook:payment:{cf_payment_id}"
        if cache.has_key(key=key):
            logger.debug(
                "Webhook payload with cf_payment_id: %s already processed. Skipping...",
                cf_payment_id,
            )
            return True

        cache.set(key=key, value=webhook_event.raw)

        # Verify the payment if Cashfree reports it as successful
        return payment_status == PAYMENT_STATUS_SUCCESS

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
        # If already confirmed, go to order details
        if self._is_payment_confirmed(payment):
            return None

        # Check existing payment status
        order_entity = self.verify_payment(payment)
        if order_entity:
            # If confirmed, go to order details. Otherwise redirect to Cashfree with existing payment_session_id
            return (
                None
                if self._is_payment_confirmed(payment)
                else self._redirect_cashfree(request, payment, order_entity)
            )

        # Otherwise create a new Cashfree order and redirect
        return self._create_cashfree_order(request, payment)

    def verify_payment(self, payment: OrderPayment):
        """
        Verify existing Cashfree order status and update payment accordingly
        """

        order_id = payment.order.full_code

        try:
            logger.debug("Fetching Cashfree order for pretix order: %s", order_id)
            x_request_id = create_request_id()
            api_response = Cashfree().PGFetchOrder(
                x_api_version=X_API_VERSION,
                order_id=order_id,
                x_request_id=x_request_id,
            )

            order_entity = api_response.data
            self._handle_cashfree_order_status(payment, order_entity)
            payment.info_data = self._create_payment_info(
                x_request_id=x_request_id, order_entity=order_entity
            )
            payment.save()
            return order_entity

        except NotFoundException:
            logger.debug("Cashfree order not found for payment: %s", payment)
            return None
        except Exception as e:
            logger.debug(
                "Error occured while fetching Cashfree order having id: %s", order_id
            )
            logger.error(e)
            raise PaymentException from e

    def handle_webhook(self, raw_payload, signature, timestamp, payment: OrderPayment):
        webhook_event = self._verify_webhook_signature(
            signature=signature, timestamp=timestamp, raw_payload=raw_payload
        )
        if not webhook_event:
            raise Exception(
                "Could not verify webhook signature of payment: %s", payment
            )
        if self._check_webhook_payload(payment=payment, webhook_event=webhook_event):
            self.verify_payment(payment)

    def checkout_prepare(self, request, cart):
        # HACK The following method call validats payment form and copies the form values to the session.
        if not super().checkout_prepare(request, cart):
            return False

        phone: PhoneNumber = request.session[self.payment_phone_session_key]

        is_valid_country = phone.country_code in SUPPORTED_COUNTRY_CODES
        is_valid_length = len(str(phone.national_number)) == 10
        valid = is_valid_country and is_valid_length

        if not valid:
            messages.error(
                request,
                _("Please provide a number with +91 extension followed by 10 digits."),
            )
            return False

        return True

    def checkout_confirm_render(
        self, request: HttpRequest, order: Order = None, info_data: dict = None
    ):
        payment_phone = request.session[self.payment_phone_session_key]
        template = get_template("pretix_cashfree/checkout_confirm.html")
        return template.render({"payment_phone": payment_phone})

    def test_mode_message(self):
        return mark_safe(
            _(
                "The Cashfree plugin is operating in test mode. You can use one of <a {args}>many payment modes</a> "
                "to perform a transaction. No money will actually be transferred."
            ).format(
                args='href="https://www.cashfree.com/docs/payments/online/resources/sandbox-environment#sandbox-environment" target="_blank"'
            )
        )

    def payment_form_render(self, request, total, order: Order = None):

        if not request.session[self.payment_phone_session_key]:
            phone = (
                str(order.phone)
                if order
                else (
                    next(iter((request.session.get("carts") or {}).values()), {})
                    .get("contact_form_data", {})
                    .get("phone")
                )
            )
            phone_prefix = guess_phone_prefix_from_request(request, self.event)
            request.session[self.payment_phone_session_key] = (
                PhoneNumber().from_string(phone)
                if phone
                else "+{}.".format(phone_prefix) if phone_prefix else None
            )
        return super().payment_form_render(request, total, order)

    @property
    def payment_form_fields(self):
        logger.debug("payment_form_fields() called")
        fields = [
            (
                "phone",
                PhoneNumberField(
                    label=_("Phone number"),
                    required=True,
                    help_text=rich_text(
                        _("Payment alerts would be sent to this phone number")
                    ),
                    widget=WrappedPhoneNumberPrefixWidget(),
                ),
            )
        ]
        return OrderedDict(fields)

    def payment_control_render(self, request, payment):
        template = get_template("pretix_cashfree/payment_control.html")
        return template.render({"payment_info": payment.info_data})

    def payment_refund_supported(self, payment):
        return True

    def payment_partial_refund_supported(self, payment):
        return False

    def execute_refund(self, refund: OrderRefund):

        order_id = refund.order.full_code

        logger.debug("Creating a refund for order_id: %s", order_id)

        create_refund_request = OrderCreateRefundRequest(
            refund_id=refund.full_id,
            refund_amount=float(refund.amount),
            refund_note=refund.comment,
        )
        x_request_id = create_request_id()

        try:
            api_response = Cashfree().PGOrderCreateRefund(
                x_api_version=X_API_VERSION,
                order_id=order_id,
                order_create_refund_request=create_refund_request,
                x_request_id=x_request_id,
            )

            if not api_response or not api_response.data:
                raise Exception("Did not receive refund details")

            refund.info_data = self._create_refund_info(
                x_request_id=x_request_id, refund_entity=api_response.data
            )
            refund.save()
            refund.done()

        except Exception as e:
            logger.error("Error occurred: %s", e)
            raise PaymentException from e

    def refund_control_render(self, request, refund):
        template = get_template("pretix_cashfree/refund_control.html")
        return template.render({"refund_info": refund.info_data})

    def matching_id(self, payment):
        return CashfreePaymentInfo(**payment.info_data).x_request_id

    def refund_matching_id(self, refund):
        return CashfreeRefundInfo(**refund.info_data).x_request_id
