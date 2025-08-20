from setuptools import setup, find_packages


setup(
    name='pretix-cashfree',
    version='0.1',
    description='Cashfree PG',
    author='Diptangshu Chakrabarty',
    packages=find_packages(),
    include_package_data=True,
    entry_points={
        "pretix.cashfree": ["pretix_cashfree=pretix_cashfree:PretixPluginMeta"]
    },
    install_required=["pretix"],
)
