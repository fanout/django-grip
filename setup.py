#!/usr/bin/env python

from setuptools import setup

setup(
    name="django-grip",
    version="3.5.1",
    description="Django GRIP library",
    author="Justin Karneges",
    author_email="justin@fanout.io",
    url="https://github.com/fanout/django-grip",
    license="MIT",
    py_modules=["django_grip"],
    install_requires=[
        "Django>=1.9",
        "pubcontrol>=3.0,<4",
        "gripcontrol>=4.0,<5",
        "Werkzeug>=1.0,<4",
        "six>=1.10,<2",
    ],
    classifiers=["Topic :: Utilities", "License :: OSI Approved :: MIT License"],
)
