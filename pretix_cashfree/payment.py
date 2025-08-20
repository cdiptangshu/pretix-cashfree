import logging
from collections import OrderedDict
from decimal import Decimal
from django import forms
from django.http import HttpRequest
from django.utils.translation import gettext_lazy as _
from pretix.base.payment import BasePaymentProvider

SUPPORTED_CURRENCIES = ["INR"]

logger = logging.getLogger("pretix.plugins.cashfree")


class Cashfree(BasePaymentProvider):
    identifier = "cashfree"
    verbose_name = _("Cashfree")
    public_name = _("Cashfree")

    @property
    def settings_form_fields(self):
        fields = [
            (
                "api_key",
                forms.CharField(
                    label=_("API Key"),
                    required=False,
                ),
            ),
            (
                "api_secret",
                forms.CharField(
                    label=_("API Secret"),
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

    def payment_form_render(self, request):
        return "<p>{}</p>".format(
            _("You will be redirected to Cashfree to complete your payment.")
        )

    def execute_payment(self, request, payment):
        # Here you would redirect to Cashfree or process the payment
        return self.redirect(request, "https://www.cashfree.com/")

    def payment_pending_render(self, request):
        return "<p>{}</p>".format(_("Your payment is being processed."))

    def payment_confirm_render(self, request):
        return "<p>{}</p>".format(_("Your payment was successful."))

    def payment_control_render(self, request):
        return "<p>{}</p>".format(_("Payment control panel for Cashfree."))

    @property
    def test_mode_supported(self):
        return False
