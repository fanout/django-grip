Django-GRIP
===========
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

This library comes with a Django middleware class, which you must use. The middleware will parse the Grip-Sig header in any requests to detect if they came from a GRIP proxy, and it will apply any hold instructions when responding. Additionally, the middleware handles WebSocket-Over-HTTP processing so that WebSockets managed by the GRIP proxy can be controlled via HTTP responses from the Django application.

Include the middleware in your settings.py:

```python
MIDDLEWARE_CLASSES = (
    'django_grip.GripMiddleware',
    ...
)
```

The middleware should be placed as early as possible in the processing order, so that it can collect all response headers and provide them in a hold instruction if necessary.

Additionally, set GRIP_PROXIES:

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

If it's possible for clients to access the Django app directly, without necessarily going through the GRIP proxy, then you may want to avoid sending GRIP instructions to those clients. An easy way to achieve this is with the GRIP_PROXY_REQUIRED setting. If set, then any direct requests that trigger a GRIP instruction response will be given a 501 Not Implemented error instead.

```python
GRIP_PROXY_REQUIRED = True
```

To prepend a fixed string to all channels used for publishing and subscribing, set GRIP_PREFIX in your configuration:

```python
GRIP_PREFIX = 'myapp-'
```

You can set any other EPCP servers that aren't necessarily proxies with PUBLISH_SERVERS:

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
from django.http import HttpResponse, HttpResponseNotAllowed
from gripcontrol import HttpStreamFormat
from django_grip import set_hold_stream, publish

def myendpoint(request):
    if request.method == 'GET':
        # if the request didn't come through a GRIP proxy, throw 501
        if not request.grip_proxied:
            return HttpResponse('Not Implemented\n', status=501)

        # subscribe every incoming request to a channel in stream mode
        resp = HttpResponse('[stream open]\n')
        set_hold_stream(resp, 'test')
        return resp
    elif request.method == 'POST':
        # publish data to subscribers
        data = request.POST['data']
        publish('test', HttpStreamFormat(data + '\n'))
        return HttpResponse('Ok\n')
    else:
        return HttpResponseNotAllowed(['GET', 'POST'])
```

Stateless WebSocket echo service with broadcast endpoint:

```python
from django.http import HttpResponse, HttpResponseNotAllowed
from gripcontrol import WebSocketMessageFormat
from django_grip import websocket_only, publish

# decorator means reject non-websocket-related requests. it also means we
#   don't need to return an HttpResponse object. the middleware will take care
#   of that for us.
@websocket_only
def echo(request):
    # since we used the decorator, this will always be a non-None value
    ws = request.wscontext

    # if this is a new connection, accept it and subscribe it to a channel
    if ws.is_opening():
        ws.accept()
        ws.subscribe('test')

    # here we loop over any messages
    while ws.can_recv():
        message = ws.recv()

        # if return value is None, then the connection is closed
        if message is None:
            ws.close()
            break

        # echo the message
        ws.send(message)

def broadcast(request):
    if request.method == 'POST':
        # publish data to all clients that are connected to the echo endpoint
        data = request.POST['data']
        publish('test', WebSocketMessageFormat(data))
        return HttpResponse('Ok\n')
    else:
        return HttpResponseNotAllowed(['POST'])
```
