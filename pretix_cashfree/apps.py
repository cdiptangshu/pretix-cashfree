from django.utils.translation import gettext_lazy

from . import __version__

try:
    from pretix.base.plugins import PluginConfig
except ImportError:
    raise RuntimeError("Please use pretix 2.7 or above to run this plugin!")


class PluginApp(PluginConfig):
    default = True
    name = "pretix_cashfree"
    verbose_name = "Cashfree"

    class PretixPluginMeta:
        name = gettext_lazy("Cashfree")
        author = "Diptangshu Chakrabarty"
        description = gettext_lazy(
            "This plugin allows you to receive payments via Cashfree"
        )
        visible = True
        version = __version__
        category = "PAYMENT"
        picture = "pretix_cashfree/images/CF_Logo_dark-WhiteBG.svg"
        experimental = True
        compatibility = "pretix>=2.7.0"
        settings_links = []
        navigation_links = []

    def ready(self):
        from . import signals  # NOQA
