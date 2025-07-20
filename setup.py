"""Setup package"""

from setuptools import setup, find_packages  # type: ignore

with open("README.md", "r") as fh:
    LONG_DESCRIPTION = fh.read()

setup(
    name="pescea",
    packages=find_packages(exclude=["tests", "tests.*"]),
    version="1.1.0",
    license="gpl-3.0",
    description="A python interface to the Escea fireplace controllers",
    long_description=LONG_DESCRIPTION,
    long_description_content_type="text/markdown",
    author="Laz Davila",
    author_email="laz.davila@gmail.com",
    url="https://github.com/lazdavila/pescea",
    download_url="https://github.com/lazdavila/pescea/archive/refs/tags/v1.0.12.tar.gz",
    keywords=[
        "Escea",
        "IoT",
    ],
    python_requires=">=3.9",
    tests_require=[
        "pytest",
        "pytest-asyncio",
        "pytest-mock",
    ],
    install_requires=[
        "async_timeout",
    ],
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "Topic :: Home Automation",
        "Topic :: System :: Hardware",
        "Operating System :: OS Independent",
        "License :: OSI Approved :: " "GNU General Public License v3 or later (GPLv3+)",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
    ],
)
