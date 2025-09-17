from typing import Optional

from django.db import models
from pydantic import BaseModel


class PaymentAttempt(models.Model):
    reference = models.CharField(max_length=190, db_index=True, unique=True)
    payment = models.ForeignKey(
        "pretixbase.OrderPayment",
        null=True,
        blank=True,
        on_delete=models.CASCADE,
        help_text="Latest payment attempt for this order",
    )


class CashfreePaymentInfo(BaseModel):
    x_request_id: str
    order_id: str
    cf_order_id: str
    order_status: str
    order_amount: float
    order_currency: str
    customer_id: str
    updated_at: str


class CashfreeRefundInfo(BaseModel):
    x_request_id: str
    order_id: str
    refund_id: str
    cf_refund_id: str
    cf_payment_id: str
    refund_type: str
    refund_status: str
    refund_amount: float
    refund_currency: str
    processed_at: Optional[str]
    updated_at: str
