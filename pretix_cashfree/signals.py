# Register your receivers here
from django.dispatch import receiver

from pretix.base.signals import register_payment_providers

from .payment import Cashfree

@receiver(register_payment_providers, dispatch_uid="payment_cashfree")
def register_payment_provider(sender, **kwargs):
    from .payment import Cashfree
    return Cashfree