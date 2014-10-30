#!/usr/bin/env python

from setuptools import setup

setup(
name='django-grip',
version='1.2.0',
description='Django GRIP library',
author='Justin Karneges',
author_email='justin@fanout.io',
url='https://github.com/fanout/django-grip',
license='MIT',
py_modules=['django_grip'],
install_requires=['pubcontrol>=1.0.4', 'gripcontrol>=1.0.4'],
classifiers=[
	'Topic :: Utilities',
	'License :: OSI Approved :: MIT License'
]
)
