import sys
import unittest
import six
import django
from django.conf import settings
from django.http import HttpResponse

settings.configure()

sys.path.append("../")
from gripcontrol import Channel
from django_grip import GripMiddleware


def dummy_get_response(request):
    return None


class MockRequest(object):
    def __init__(self):
        pass


class TestMiddleware(unittest.TestCase):
    def test_hold(self):
        if django.VERSION[0] >= 4:
            m = GripMiddleware(dummy_get_response)
        else:
            m = GripMiddleware()

        req = MockRequest()
        req.method = "GET"
        req.META = {}

        self.assertFalse(hasattr(req, "grip"))

        m.process_request(req)

        self.assertTrue(hasattr(req, "grip"))

        instruct = req.grip.start_instruct()
        instruct.add_channel("apple")
        instruct.add_channel(Channel("banana", prev_id="1"))
        instruct.set_hold_stream()
        instruct.set_next_link("/endpoint")
        instruct.set_keep_alive("keepalive\n", 25)

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertEqual(resp["Grip-Hold"], "stream")
        self.assertEqual(resp["Grip-Channel"], "apple, banana; prev-id=1")
        self.assertEqual(resp["Grip-Link"], "</endpoint>; rel=next")
        self.assertEqual(
            resp["Grip-Keep-Alive"], "keepalive\\n; format=cstring; timeout=25"
        )

    def test_not_hold(self):
        if django.VERSION[0] >= 4:
            m = GripMiddleware(dummy_get_response)
        else:
            m = GripMiddleware()

        req = MockRequest()
        req.method = "GET"
        req.META = {}

        self.assertFalse(hasattr(req, "grip"))

        m.process_request(req)

        self.assertTrue(hasattr(req, "grip"))

        instruct = req.grip.start_instruct()
        instruct.add_channel("apple")
        instruct.add_channel(Channel("banana", prev_id="1"))
        instruct.meta = {"user": "123", "id_format": "channel_1:%(events-channel_1)s"}

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertFalse("Grip-Hold" in resp)
        self.assertEqual(resp["Grip-Channel"], "apple, banana; prev-id=1")
        self.assertEqual(
            resp["Grip-Set-Meta"],
            'user="123", id_format="channel_1:%(events-channel_1)s"',
        )

    def test_wscontext(self):
        if django.VERSION[0] >= 4:
            m = GripMiddleware(dummy_get_response)
        else:
            m = GripMiddleware()

        req = MockRequest()
        req.method = "GET"
        req.META = {}

        self.assertFalse(hasattr(req, "wscontext"))

        m.process_request(req)

        self.assertTrue(hasattr(req, "wscontext"))
        self.assertTrue(req.wscontext is None)

        req = MockRequest()
        req.method = "POST"
        req.META = {
            "CONTENT_TYPE": "application/websocket-events",
        }
        req.body = "OPEN\r\n"

        self.assertFalse(hasattr(req, "wscontext"))

        m.process_request(req)

        self.assertTrue(hasattr(req, "wscontext"))
        self.assertTrue(req.wscontext is not None)
        self.assertEqual(req.wscontext.is_opening(), True)

        req.wscontext.accept()

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertEqual(resp.content, six.b("OPEN\r\n"))
        self.assertEqual(resp["Content-Type"], "application/websocket-events")

    def test_meta(self):
        if django.VERSION[0] >= 4:
            m = GripMiddleware(dummy_get_response)
        else:
            m = GripMiddleware()

        req = MockRequest()
        req.method = "POST"
        req.META = {
            "CONTENT_TYPE": "application/websocket-events",
        }
        req.body = "OPEN\r\n"

        m.process_request(req)
        req.wscontext.accept()
        self.assertFalse("user" in req.wscontext.meta)

        req.wscontext.meta["user"] = "alice"

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertEqual(resp["Set-Meta-user"], "alice")

        req = MockRequest()
        req.method = "POST"
        req.META = {
            "CONTENT_TYPE": "application/websocket-events",
            "HTTP_META_USER": "alice",
        }
        req.body = "TEXT 5\r\nhello\r\n"

        m.process_request(req)
        self.assertEqual(req.wscontext.meta["user"], "alice")

        req.wscontext.meta["user"] = "bob"

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertEqual(resp["Set-Meta-user"], "bob")

        req = MockRequest()
        req.method = "POST"
        req.META = {
            "CONTENT_TYPE": "application/websocket-events",
            "HTTP_META_USER": "bob",
        }
        req.body = "TEXT 5\r\nhello\r\n"

        m.process_request(req)
        self.assertEqual(req.wscontext.meta["user"], "bob")

        del req.wscontext.meta["user"]

        resp = HttpResponse()
        resp = m.process_response(req, resp)

        self.assertEqual(resp["Set-Meta-user"], "")


if __name__ == "__main__":
    unittest.main()
