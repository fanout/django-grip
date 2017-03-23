from copy import deepcopy
from struct import pack, unpack
import threading
import six
import sys
from functools import wraps
import django
from django.utils.decorators import available_attrs
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from pubcontrol import Item
from gripcontrol import Channel, Response, GripPubControl, WebSocketEvent, \
	validate_sig, create_grip_channel_header, create_hold, \
	decode_websocket_events, encode_websocket_events, \
	websocket_control_message

if django.VERSION[0] > 1 or (django.VERSION[0] == 1 and django.VERSION[1] >= 10):
	from django.utils.deprecation import MiddlewareMixin

	middleware_parent = MiddlewareMixin
else:
	middleware_parent = object

# The PubControl instance and lock used for synchronization.
_pubcontrol = None
_lock = threading.Lock()


def _is_basestring_instance(instance):
	try:
		if isinstance(instance, basestring):
			return True
	except NameError:
		if isinstance(instance, str):
			return True
	return False


def _get_pubcontrol():
	global _pubcontrol
	_lock.acquire()
	if _pubcontrol is None:
		_pubcontrol = GripPubControl()
		_pubcontrol.apply_config(getattr(settings, 'PUBLISH_SERVERS', []))
		_pubcontrol.apply_grip_config(getattr(settings, 'GRIP_PROXIES', []))
	_lock.release()
	return _pubcontrol

def _get_prefix():
	return getattr(settings, 'GRIP_PREFIX', '')

# convert input to list of Channel objects
def _convert_channels(channels):
	if isinstance(channels, Channel) or _is_basestring_instance(channels):
		channels = [channels]
	out = list()
	for c in channels:
		if _is_basestring_instance(c):
			c = Channel(c)
		assert(isinstance(c, Channel))
		out.append(c)
	return out

def publish(channel, formats, id=None, prev_id=None, blocking=False, callback=None):
	pub = _get_pubcontrol()
	pub.publish(_get_prefix() + channel,
		Item(formats, id=id, prev_id=prev_id),
		blocking=blocking,
		callback=callback)

def set_hold_longpoll(request, channels, timeout=None):
	request.grip_info = {
		'hold': 'response',
		'channels': _convert_channels(channels)}
	if timeout:
		request.grip_info['timeout'] = timeout

def set_hold_stream(request, channels):
	request.grip_info = {
		'hold': 'stream',
		'channels': _convert_channels(channels)}

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

		if e.type == 'TEXT':
			if e.content:
				if sys.version_info[0] >= 3:
					return e.content
				else:
					return e.content.decode('utf-8')
			else:
				return ''
		elif e.type == 'BINARY':
			if e.content:
				return e.content
			else:
				return ''
		elif e.type == 'CLOSE':
			if e.content and len(e.content) == 2:
				self.close_code = unpack('>H', e.content)[0]
			return None
		else: # DISCONNECT
			raise IOError('client disconnected unexpectedly')

	def send(self, message):
		if sys.version_info[0] >= 3:
			self.out_events.append(WebSocketEvent('TEXT', 'm:' + str(message)))
		else:
			if isinstance(message, unicode):
				message = message.encode('utf-8')
			self.out_events.append(WebSocketEvent('TEXT', 'm:' + message))

	def send_binary(self, message):
		if sys.version_info[0] >= 3:
			self.out_events.append(WebSocketEvent('BINARY', 'm:' +  str(message)))
		else:
			if isinstance(message, unicode):
				message = message.encode('utf-8')
			self.out_events.append(WebSocketEvent('BINARY', 'm:' + message))

	def send_control(self, message):
		if sys.version_info[0] >= 3:
			self.out_events.append(WebSocketEvent('TEXT', 'c:' + str(message)))
		else:
			if isinstance(message, unicode):
				message = message.encode('utf-8')
			self.out_events.append(WebSocketEvent('TEXT', 'c:' + message))

	def subscribe(self, channel):
		self.send_control(websocket_control_message(
			'subscribe', {'channel': _get_prefix() + channel}))

	def unsubscribe(self, channel):
		self.send_control(websocket_control_message(
			'unsubscribe', {'channel': _get_prefix() + channel}))

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

class GripMiddleware(middleware_parent):
	def process_request(self, request):
		# make sure these are always set
		request.grip_proxied = False
		request.grip_signed = False
		request.wscontext = None

		grip_proxied = False
		grip_signed = False

		grip_sig_header = request.META.get('HTTP_GRIP_SIG')
		if grip_sig_header:
			proxies = getattr(settings, 'GRIP_PROXIES', [])

			all_proxies_have_keys = True
			for entry in proxies:
				if 'key' not in entry:
					all_proxies_have_keys = False
					break

			if all_proxies_have_keys:
				# if all proxies have keys, then don't
				#   consider the request to be proxied unless
				#   one of them signed it
				for entry in proxies:
					if validate_sig(grip_sig_header, entry['key']):
						grip_proxied = True
						grip_signed = True
						break
			else:
				# if even one proxy doesn't have a key, then
				#   don't require verification in order to
				#   consider the request to have been proxied
				grip_proxied = True

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
			for k, v in six.iteritems(request.META):
				if k.startswith('HTTP_META_'):
					meta[_convert_header_name(k[10:])] = v
			body = request.body

			if sys.version_info[0] >= 3:
				body = body.decode('utf-8')
			else:
				assert(not isinstance(body, unicode))

			try:
				events = decode_websocket_events(body)
			except:
				return HttpResponseBadRequest('Error parsing WebSocket events.\n')

			wscontext = WebSocketContext(cid, meta, events)

		request.grip_proxied = grip_proxied
		request.grip_signed = grip_signed
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
			for k, v in six.iteritems(wscontext.orig_meta):
				found = False
				for nk, nv in wscontext.meta:
					if nk.lower() == k:
						found = True
						break
				if not found:
					meta_remove.add(k)

			# meta to set?
			meta_set = dict()
			for k, v in six.iteritems(wscontext.meta):
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
				events.append(WebSocketEvent('CLOSE', pack('>H', wscontext.out_close_code)))

			response = HttpResponse(encode_websocket_events(events), content_type='application/websocket-events')
			if wscontext.accepted:
				response['Sec-WebSocket-Extensions'] = 'grip'
			for k in meta_remove:
				response['Set-Meta-' + k] = ''
			for k, v in six.iteritems(meta_set):
				response['Set-Meta-' + k] = v
		else:
			grip_info = None
			if hasattr(request, 'grip_info'):
				grip_info = request.grip_info
			elif hasattr(response, 'grip_info'):
				# old django-grip versions required passing an HttpResponse to the
				#   set_hold_* methods, so fall back to that for backwards compat
				grip_info = response.grip_info

			if grip_info:
				if not request.grip_proxied and getattr(settings, 'GRIP_PROXY_REQUIRED', False):
					return HttpResponse('Not Implemented\n', status=501)

				channels = grip_info['channels']

				# apply prefix to channels if needed
				prefix = _get_prefix()
				if prefix:
					for c in channels:
						c.name = prefix + c.name

				# code 304 only allows certain headers. if the webserver
				#   strictly enforces this, then we won't be able to use
				#   Grip- headers to talk to the proxy. work around this by
				#   using body instructions instead.
				if response.status_code == 304:
					headers = list()
					for k, v in response.items():
						headers.append([k, v])
					iresponse = Response(
						code=response.status_code,
						reason=getattr(response, 'reason_phrase', None),
						headers=headers,
						body=response.content)
					body = create_hold(
						grip_info['hold'],
						channels,
						iresponse,
						timeout=grip_info.get('timeout'))
					response = HttpResponse(body + '\n', content_type='application/grip-instruct')
				else:
					response['Grip-Hold'] = grip_info['hold']
					response['Grip-Channel'] = create_grip_channel_header(channels)
					if 'timeout' in grip_info:
						response['Grip-Timeout'] = str(grip_info['timeout'])

		return response
