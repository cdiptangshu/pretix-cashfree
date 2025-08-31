from django.urls import include, re_path

from .views import redirect_view, return_view, webhook_view

event_patterns = [
    re_path(
        r"^cashfree/",
        include(
            [
                re_path(r"^return/$", return_view, name="return"),
                re_path(r"^redirect/$", redirect_view, name="redirect"),
                re_path(
                    r"w/(?P<cart_namespace>[a-zA-Z0-9]{16})/return/",
                    return_view,
                    name="return",
                ),
            ]
        ),
    ),
]

urlpatterns = [
    re_path(r"^_cashfree/webhook/$", webhook_view, name="webhook"),
]
