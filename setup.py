"""Module Setup File for PIP Installation."""

import pathlib

from setuptools import find_packages, setup

HERE = pathlib.Path(__file__).parent
README = (HERE / "README.md").read_text()

setup(
    name="pyiox",
    version_format="{tag}",
    license="Apache License 2.0",
    url="https://github.com/shbatm/pyiox",
    author="shbatm",
    author_email="support@shbatm.com",
    description="Python module for asynchronous communication with Universal Devices, Inc.'s ISY & IoX controllers.",
    long_description=README,
    long_description_content_type="text/markdown",
    packages=find_packages(),
    zip_safe=False,
    include_package_data=True,
    platforms="any",
    use_scm_version=True,
    setup_requires=["setuptools_scm"],
    install_requires=[
        "aiohttp>=3.8.1",
        "python-dateutil>=2.8.1",
        "requests>=2.28.1",
        "colorlog>=6.6.0",
        "xmltodict>=0.12.0",
    ],
    keywords=[
        "home automation",
        "isy",
        "isy994",
        "isy-994",
        "UDI",
        "home-assistant",
        "polisy",
        "eisy",
    ],
    classifiers=[
        "Intended Audience :: Developers",
        "License :: OSI Approved :: Apache Software License",
        "Operating System :: OS Independent",
        "Programming Language :: Python",
        "Programming Language :: Python :: 3",
        "Topic :: Home Automation",
    ],
)
