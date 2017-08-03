import sys
import unittest
from struct import pack
import json
from django.conf import settings
settings.configure()

sys.path.append('../')
from gripcontrol import WebSocketEvent
from django_grip import WebSocketContext

is_python3 = sys.version_info >= (3,)

class TestWebSocketContext(unittest.TestCase):
	def test_context(self):
		in_events = []
		ws = WebSocketContext('ws-1', {}, in_events)
		self.assertFalse(ws.is_opening())

		in_events = []
		in_events.append(WebSocketEvent('OPEN'))
		ws = WebSocketContext('ws-1', {}, in_events)
		self.assertTrue(ws.is_opening())
		ws.accept()
		self.assertTrue(ws.accepted)

		in_events = []
		ws = WebSocketContext('ws-1', {}, in_events)
		self.assertFalse(ws.is_opening())
		ws.close(100)
		self.assertTrue(ws.closed)
		self.assertEqual(ws.out_close_code, 100)

		in_events = []
		in_events.append(WebSocketEvent('CLOSE', pack('>H', 100)))
		ws = WebSocketContext('ws-1', {}, in_events)
		self.assertTrue(ws.can_recv())
		msg = ws.recv()
		self.assertTrue(msg is None)
		self.assertEqual(ws.close_code, 100)
		self.assertFalse(ws.can_recv())

		in_events = []
		if is_python3:
			in_events.append(WebSocketEvent('TEXT', b'hello world'))
			in_events.append(WebSocketEvent('BINARY', b'h3110 w0r1d'))
		else:
			in_events.append(WebSocketEvent('TEXT', 'hello world'))
			in_events.append(WebSocketEvent('BINARY', 'h3110 w0r1d'))
		ws = WebSocketContext('ws-1', {}, in_events)
		self.assertTrue(ws.can_recv())
		msg = ws.recv()
		self.assertEqual(msg, 'hello world')
		self.assertTrue(ws.can_recv())
		msg = ws.recv()
		if is_python3:
			self.assertEqual(msg, b'h3110 w0r1d')
		else:
			self.assertEqual(msg, 'h3110 w0r1d')
		self.assertFalse(ws.can_recv())
		e = None
		try:
			ws.recv()
		except Exception as tmp:
			e = tmp
		self.assertTrue(isinstance(e, IndexError))
		ws.send('good day')
		if is_python3:
			ws.send_binary(b'g00d d4y')
		else:
			ws.send_binary('g00d d4y')
		self.assertEqual(len(ws.out_events), 2)
		self.assertEqual(ws.out_events[0].type, 'TEXT')
		if is_python3:
			self.assertEqual(ws.out_events[0].content, b'm:good day')
		else:
			self.assertEqual(ws.out_events[0].content, 'm:good day')
		self.assertEqual(ws.out_events[1].type, 'BINARY')
		if is_python3:
			self.assertEqual(ws.out_events[1].content, b'm:g00d d4y')
		else:
			self.assertEqual(ws.out_events[1].content, 'm:g00d d4y')

		ws = WebSocketContext('ws-1', {}, [])
		ws.subscribe('foo')
		ws.unsubscribe('foo')
		ws.detach()
		self.assertEqual(len(ws.out_events), 3)
		self.assertEqual(ws.out_events[0].type, 'TEXT')
		if is_python3:
			self.assertTrue(ws.out_events[0].content.startswith(b'c:'))
		else:
			self.assertTrue(ws.out_events[0].content.startswith('c:'))
		cmsg = json.loads(ws.out_events[0].content[2:].decode('utf-8'))
		self.assertEqual(cmsg['type'], 'subscribe')
		self.assertEqual(ws.out_events[1].type, 'TEXT')
		if is_python3:
			self.assertTrue(ws.out_events[1].content.startswith(b'c:'))
		else:
			self.assertTrue(ws.out_events[1].content.startswith('c:'))
		cmsg = json.loads(ws.out_events[1].content[2:].decode('utf-8'))
		self.assertEqual(cmsg['type'], 'unsubscribe')
		self.assertEqual(ws.out_events[2].type, 'TEXT')
		if is_python3:
			self.assertTrue(ws.out_events[2].content.startswith(b'c:'))
		else:
			self.assertTrue(ws.out_events[2].content.startswith('c:'))
		cmsg = json.loads(ws.out_events[2].content[2:].decode('utf-8'))
		self.assertEqual(cmsg['type'], 'detach')

if __name__ == '__main__':
	unittest.main()
