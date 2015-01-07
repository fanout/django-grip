#!/usr/bin/env python

from setuptools import setup

setup(
name='django-grip',
version='1.2.1',
description='Django GRIP library',
author='Justin Karneges',
author_email='justin@fanout.io',
url='https://github.com/fanout/django-grip',
license='MIT',
py_modules=['django_grip'],
install_requires=['pubcontrol>=2.0.0', 'gripcontrol>=2.0.0'],
classifiers=[
	'Topic :: Utilities',
	'License :: OSI Approved :: MIT License'
]
)
