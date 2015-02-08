#!/usr/bin/env python

from setuptools import setup

setup(
name='django-grip',
version='1.4.1',
description='Django GRIP library',
author='Justin Karneges',
author_email='justin@fanout.io',
url='https://github.com/fanout/django-grip',
license='MIT',
py_modules=['django_grip'],
install_requires=['pubcontrol>=2,<3', 'gripcontrol>=2.3,<3'],
classifiers=[
	'Topic :: Utilities',
	'License :: OSI Approved :: MIT License'
]
)
