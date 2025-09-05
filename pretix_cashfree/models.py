from django.db import models


class ReferencedCashfreeObject(models.Model):
    reference = models.CharField(max_length=190, db_index=True, unique=True)
    payment = models.ForeignKey(
        "pretixbase.OrderPayment", null=True, blank=True, on_delete=models.CASCADE
    )
