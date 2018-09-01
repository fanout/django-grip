import sys
import unittest
from django.conf import settings
from django.http import HttpResponse
settings.configure()

sys.path.append('../')
from gripcontrol import Channel
from django_grip import GripMiddleware

class MockRequest(object):
	def __init__(self):
		pass

class TestMiddleware(unittest.TestCase):
	def test_hold(self):
		m = GripMiddleware()

		req = MockRequest()
		req.method = 'GET'
		req.META = {}

		self.assertFalse(hasattr(req, 'grip'))

		m.process_request(req)

		self.assertTrue(hasattr(req, 'grip'))

		instruct = req.grip.start_instruct()
		instruct.add_channel('apple')
		instruct.add_channel(Channel('banana', prev_id='1'))
		instruct.set_hold_stream()
		instruct.set_next_link('/endpoint')
		instruct.set_keep_alive('keepalive\n', 25)

		resp = HttpResponse()
		resp = m.process_response(req, resp)

		self.assertEqual(resp['Grip-Hold'], 'stream')
		self.assertEqual(resp['Grip-Channel'], 'apple, banana; prev-id=1')
		self.assertEqual(resp['Grip-Link'], '</endpoint>; rel=next')
		self.assertEqual(resp['Grip-Keep-Alive'], 'keepalive\\n; format=cstring; timeout=25')

if __name__ == '__main__':
	unittest.main()
