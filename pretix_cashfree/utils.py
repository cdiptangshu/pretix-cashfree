import re


def sanitize_phone(phone, default="9999999999"):
    """
    Cashfree requires exactly 10 digits for phone numbers.
    The '+' override mentioned in docs does not work,
    so we always take the last 10 digits.
    """
    if not phone or not re.fullmatch(r"\+?\d+", str(phone)):
        return default

    value = str(phone)[-10:] if phone else ""
    return value if len(value) == 10 else default
