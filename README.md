Django-GRIP
-----------
Author: Justin Karneges <justin@fanout.io>

GRIP library for Python/Django.

Requirements
------------

* pubcontrol
* gripcontrol

Install
-------

You can install from PyPi:

    sudo pip install django-grip

Or from this repository:

    sudo python setup.py install

Sample usage
------------

Set GRIP_PROXIES in settings.py:

```python
# pushpin and/or fanout.io is used for sending realtime data to clients
GRIP_PROXIES = [
    # pushpin
    {
        'key': 'changeme',
        'control_uri': 'http://localhost:5561'
    }
    # fanout.io
    #{
    #    'key': b64decode('your-realm-key'),
    #    'control_uri': 'http://api.fanout.io/realm/your-realm',
    #    'control_iss': 'your-realm'
    #}
]
```

You can also set any other EPCP servers that aren't necessarily proxies with PUBLISH_SERVERS:

```python
PUBLISH_SERVERS = [
    {
        'uri': 'http://example.com/base-uri',
        'iss': 'your-iss',
        'key': 'your-key'
    }
]
```

Example view:

```python
from django.http import HttpResponse
from gripcontrol import HttpStreamFormat
from django_grip import publish, set_hold_stream

def myendpoint(request):
    if request.method == 'GET':
        # subscribe every incoming request to a channel in stream mode
        resp = HttpResponse('[stream open]\n')
        set_hold_stream(resp, 'test')
        return resp
    elif request.method == 'POST':
        # publish data to subscribers
        data = request.POST['data']
        publish('test', HttpStreamFormat(data + '\n'))
        return HttpResponse('Ok\n')
```
