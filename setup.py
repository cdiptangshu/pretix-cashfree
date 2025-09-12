from setuptools import setup, find_packages


setup(
    name="pretix-cashfree",
    description="Cashfree PG",
    author="Diptangshu Chakrabarty",
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "pretix.cashfree": ["pretix_cashfree=pretix_cashfree:PretixPluginMeta"]
    },
    install_required=["pretix"],
)
