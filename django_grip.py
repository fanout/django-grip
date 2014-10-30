from copy import deepcopy
from struct import pack, unpack
from urlparse import urlparse, parse_qs
from urllib import urlencode
from base64 import b64decode
import threading
import atexit
from functools import wraps
from django.utils.decorators import available_attrs
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from pubcontrol import PubControl, Item
from gripcontrol import Channel, WebSocketEvent, validate_sig, decode_websocket_events, encode_websocket_events, websocket_control_message

class PubControlCallbackHandler(object):
	def __init__(self, num_calls, callback):
		self.num_calls = num_calls
		self.callback = callback
		self.success = True
		self.first_error_message = None

	def handler(self, success, message):
		if not success and self.success:
			self.success = False
			self.first_error_message = message

		self.num_calls -= 1
		if self.num_calls <= 0:
			self.callback(self.success, self.first_error_message)

class PubControlSet(object):
	def __init__(self):
		self.pubs = list()
		atexit.register(self._finish)

	def clear(self):
		self.pubs = list()

	def add(self, pub):
		self.pubs.append(pub)

	def apply_config(self, config):
		for entry in config:
			pub = PubControl(entry['uri'])
			if 'iss' in entry:
				pub.set_auth_jwt({'iss': entry['iss']}, entry['key'])

			self.pubs.append(pub)

	def apply_grip_config(self, config):
		for entry in config:
			if 'control_uri' not in entry:
				continue

			pub = PubControl(entry['control_uri'])
			if 'control_iss' in entry:
				pub.set_auth_jwt({'iss': entry['control_iss']}, entry['key'])

			self.pubs.append(pub)

	def publish(self, channel, item, blocking=False, callback=None):
		if blocking:
			for pub in self.pubs:
				pub.publish(channel, item)
		else:
			if callback is not None:
				cb = PubControlCallbackHandler(len(self.pubs), callback).handler
			else:
				cb = None

			for pub in self.pubs:
				pub.publish_async(channel, item, callback=cb)

	def _finish(self):
		for pub in self.pubs:
			pub.finish()

_threadlocal = threading.local()

def _get_pubcontrolset():
	if not hasattr(_threadlocal, 'pubcontrolset'):
		pcs = PubControlSet()
		pcs.apply_config(getattr(settings, 'PUBLISH_SERVERS', []))
		pcs.apply_grip_config(getattr(settings, 'GRIP_PROXIES', []))

		_threadlocal.pubcontrolset = pcs
	return _threadlocal.pubcontrolset

def _make_grip_channel_header(channels):
	if isinstance(channels, Channel):
		channels = [channels]
	elif isinstance(channels, basestring):
		channels = [Channel(channels)]
	assert(len(channels) > 0)

	parts = list()
	for channel in channels:
		s = channel.name
		if channel.prev_id is not None:
			s += '; prev-id=%s' % channel.prev_id
		parts.append(s)
	return ', '.join(parts)

def publish(channel, formats, id=None, prev_id=None, blocking=False, callback=None):
	pcs = _get_pubcontrolset()
	pcs.publish(channel, Item(formats, id=id, prev_id=prev_id), blocking=blocking, callback=callback)

def set_hold_longpoll(response, channels, timeout=None):
	response['Grip-Hold'] = 'response'
	response['Grip-Channel'] = _make_grip_channel_header(channels)
	if timeout:
		response['Grip-Timeout'] = str(timeout)

def set_hold_stream(response, channels):
	response['Grip-Hold'] = 'stream'
	response['Grip-Channel'] = _make_grip_channel_header(channels)

class WebSocketContext(object):
	def __init__(self, id, meta, in_events):
		self.id = id
		self.in_events = in_events
		self.read_index = 0
		self.accepted = False
		self.close_code = None
		self.closed = False
		self.out_close_code = None
		self.out_events = []
		self.orig_meta = meta
		self.meta = deepcopy(meta)

	def is_opening(self):
		return (self.in_events and self.in_events[0].type == 'OPEN')

	def accept(self):
		self.accepted = True

	def close(self, code=None):
		self.closed = True
		if code is not None:
			self.out_close_code = code
		else:
			self.out_close_code = 0

	def can_recv(self):
		for n in range(self.read_index, len(self.in_events)):
			if self.in_events[n].type in ('TEXT', 'BINARY', 'CLOSE', 'DISCONNECT'):
				return True
		return False

	def recv(self):
		e = None
		while e is None and self.read_index < len(self.in_events):
			if self.in_events[self.read_index].type in ('TEXT', 'BINARY', 'CLOSE', 'DISCONNECT'):
				e = self.in_events[self.read_index]
			elif self.in_events[self.read_index].type == 'PING':
				self.out_events.append(WebSocketEvent('PONG'))
			self.read_index += 1
		if e is None:
			raise IndexError('read from empty buffer')

		if e.type == 'TEXT' or e.type == 'BINARY':
			return e.content
		elif e.type == 'CLOSE':
			if e.content and len(e.content) == 2:
				self.close_code = unpack('H', e.content)[0]
			return None
		else: # DISCONNECT
			raise IOError('client disconnected unexpectedly')

	def send(self, message):
		self.out_events.append(WebSocketEvent('TEXT', 'm:' + message))

	def send_control(self, message):
		self.out_events.append(WebSocketEvent('TEXT', 'c:' + message))

	def subscribe(self, channel):
		self.send_control(websocket_control_message('subscribe', {'channel': channel}))

	def unsubscribe(self, channel):
		self.send_control(websocket_control_message('unsubscribe', {'channel': channel}))

	def detach(self):
		self.send_control(websocket_control_message('detach'))

def _convert_header_name(name):
	out = ''
	for c in name:
		if c == '_':
			out += '-'
		else:
			out += c.lower()
	return out

def websocket_only(view_func):
	def wrapped_view(*args, **kwargs):
		response = view_func(*args, **kwargs)
		if response is None:
			# supply a response if the view didn't
			return HttpResponse()
		return response

	wrapped_view.websocket_only = True
	return wraps(view_func, assigned=available_attrs(view_func))(wrapped_view)

class GripMiddleware(object):
	def process_request(self, request):
		# make sure these are always set
		request.grip_proxied = False
		request.wscontext = None

		grip_signed = False
		grip_sig_header = request.META.get('HTTP_GRIP_SIG')
		if grip_sig_header:
			# did any of the known proxies sign this request?
			for entry in getattr(settings, 'GRIP_PROXIES', []):
				if validate_sig(grip_sig_header, entry['key']):
					grip_signed = True
					break

		content_type = request.META.get('CONTENT_TYPE')
		if content_type:
			at = content_type.find(';')
			if at != -1:
				content_type = content_type[:at]

		# legacy check using accept
		accept_types = request.META.get('HTTP_ACCEPT')
		if accept_types:
			tmp = accept_types.split(',')
			accept_types = list()
			for s in tmp:
				accept_types.append(s.strip())

		wscontext = None
		if request.method == 'POST' and ((content_type and content_type == 'application/websocket-events') or (accept_types and 'application/websocket-events' in accept_types)):
			cid = request.META.get('HTTP_CONNECTION_ID')
			meta = dict()
			for k, v in request.META.iteritems():
				if k.startswith('HTTP_META_'):
					meta[_convert_header_name(k[10:])] = v
			try:
				events = decode_websocket_events(request.body)
			except:
				return HttpResponseBadRequest('Error parsing WebSocket events.\n')

			wscontext = WebSocketContext(cid, meta, events)

		request.grip_proxied = grip_signed
		request.wscontext = wscontext

	def process_view(self, request, view_func, view_args, view_kwargs):
		if getattr(view_func, 'websocket_only', False) and not request.wscontext:
			return HttpResponseBadRequest('Request must contain WebSocket events.\n')

	def process_response(self, request, response):
		# if this was a successful websocket-events request, then hijack the response
		if getattr(request, 'wscontext', None) and response.status_code == 200 and len(response.content) == 0:
			wscontext = request.wscontext

			# meta to remove?
			meta_remove = set()
			for k, v in wscontext.orig_meta.iteritems():
				found = False
				for nk, nv in wscontext.meta:
					if nk.lower() == k:
						found = True
						break
				if not found:
					meta_remove.add(k)

			# meta to set?
			meta_set = dict()
			for k, v in wscontext.meta.iteritems():
				lname = k.lower()
				need_set = True
				for ok, ov in wscontext.orig_meta:
					if lname == ok and v == ov:
						need_set = False
						break
				if need_set:
					meta_set[lname] = v

			events = list()
			if wscontext.accepted:
				events.append(WebSocketEvent('OPEN'))
			events.extend(wscontext.out_events)
			if wscontext.closed:
				events.append(WebSocketEvent('CLOSE', pack('H', wscontext.out_close_code)))

			response = HttpResponse(encode_websocket_events(events), content_type='application/websocket-events')
			if wscontext.accepted:
				response['Sec-WebSocket-Extensions'] = 'grip'
			for k in meta_remove:
				response['Set-Meta-' + k] = ''
			for k, v in meta_set.iteritems():
				response['Set-Meta-' + k] = v

		return response

def parse_grip_uri(uri):
	parsed = urlparse(uri)
	params = parse_qs(parsed.query)
	iss = None
	key = None
	if 'iss' in params:
		iss = params['iss'][0]
		del params['iss']
	if 'key' in params:
		key = params['key'][0]
		del params['key']
	if key is not None and key.startswith('base64:'):
		key = b64decode(key[7:])
	qs = urlencode(params, True)
	path = parsed.path
	if path.endswith('/'):
		path = path[:-1]
	control_uri = parsed.scheme + '://' + parsed.netloc + path
	if qs:
		control_uri += '?' + qs
	out = {'control_uri': control_uri}
	if iss:
		out['control_iss'] = iss
	if key:
		out['key'] = key
	return out
