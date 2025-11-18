# Register your receivers here
from collections import OrderedDict
from django.dispatch import receiver
from pretix.base.signals import register_payment_providers, register_global_settings
from collections import OrderedDict

from django import forms
from django.dispatch import receiver
from django.utils.translation import gettext_lazy as _

from pretix.base.forms import SecretKeySettingsField


@receiver(register_payment_providers, dispatch_uid="payment_cashfree")
def register_payment_provider(sender, **kwargs):
    from .payment import CashfreePaymentProvider

    return CashfreePaymentProvider

@receiver(register_global_settings, dispatch_uid='cashfree_global_settings')
def register_global_settings(sender, **kwargs):
    return OrderedDict([
        ('payment_cashfree_global_client_id', forms.CharField(
            label=_('Cashfree Client ID'),
            required=False,
        )),
        ('payment_cashfree_global_client_secret', SecretKeySettingsField(
            label=_('Cashfree Client Secret'),
            required=False,
        )),
    ])
