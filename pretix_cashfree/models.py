from django.db import models
from pydantic import BaseModel


class PaymentAttempt(models.Model):
    order_id = models.CharField(max_length=190, db_index=True, unique=True)
    payment = models.ForeignKey(
        "pretixbase.OrderPayment",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text="Latest payment attempt for this order",
    )


class PaymentWebhookEvent(models.Model):
    cf_payment_id = models.CharField(max_length=50, db_index=True, unique=True)
    order_code = models.CharField(max_length=50)
    payment_local_id = models.CharField(max_length=50)
    payment_status = models.CharField(max_length=50)
    payload = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)


class CashfreePaymentInfo(BaseModel):
    x_request_id: str
    order_id: str
    order_status: str
    order_amount: float
    order_currency: str
    customer_id: str
    updated_at: str
