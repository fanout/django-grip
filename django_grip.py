from base64 import b64encode
from struct import pack
import threading
import six
from functools import wraps
from werkzeug.http import parse_options_header
import django
from django.utils.decorators import available_attrs
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from pubcontrol import Item
from gripcontrol import Channel, GripPubControl, WebSocketEvent, \
	WebSocketContext, parse_grip_uri, validate_sig, \
	create_grip_channel_header, decode_websocket_events, \
	encode_websocket_events

if django.VERSION[0] > 1 or (django.VERSION[0] == 1 and
		django.VERSION[1] >= 10):
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

class GripResponse(object):
	def __init__(self):
		self.hold = ''
		self.channels = []
		self.timeout = 0
		self.keep_alive = ''
		self.keep_alive_timeout = 0
		self.next_link = ''
		self.next_link_timeout = 0
		self.meta = {}

	def is_empty(self):
		return (not self.hold and not self.next_link)

class GripData(object):
	def __init__(self):
		self.proxied = False
		self.signed = False
		self.features = set()
		self.last = {}
		self.response = GripResponse()

def _get_proxies():
	proxies = getattr(settings, 'GRIP_PROXIES', [])
	grip_url = getattr(settings, 'GRIP_URL', None)
	if grip_url:
		proxies.append(parse_grip_uri(grip_url))
	return proxies

def _get_pubcontrol():
	global _pubcontrol
	_lock.acquire()
	if _pubcontrol is None:
		_pubcontrol = GripPubControl()
		_pubcontrol.apply_grip_config(_get_proxies())
	_lock.release()
	return _pubcontrol

# create the pubcontrol object right away for require_subscribers
_get_pubcontrol()

def _get_prefix():
	return getattr(settings, 'GRIP_PREFIX', '')

# convert input to list of Channel objects
def _convert_channels(channels):
	if isinstance(channels, Channel) or _is_basestring_instance(channels):
		channels = [channels]
	out = []
	for c in channels:
		if _is_basestring_instance(c):
			c = Channel(c)
		assert(isinstance(c, Channel))
		out.append(c)
	return out

def _escape_param(s):
	out = ''
	for c in s:
		if c == '"':
			out += '\\"'
		else:
			out += c
	return out

def _cstring_encode(s):
	out = ''
	for c in s:
		if c == '\n':
			out += '\\n'
		elif c == '\r':
			out += '\\r'
		elif c == '\t':
			out += '\\t'
		elif ord(c) < 0x20:
			raise ValueError('not cstring encodable')
		else:
			out += c
	return out

def _keep_alive_header(data, timeout):
	try:
		cs = _cstring_encode(data)
		hvalue = '%s; format=cstring' % cs
	except ValueError:
		hvalue = '%s; format=base64' % b64encode(data)

	hvalue += '; timeout=%d' % timeout
	return hvalue

def _set_meta_header(meta):
	hvalue = ''
	for k, v in six.iteritems(meta):
		if len(hvalue) > 0:
			hvalue += ', '
		hvalue += '%s="%s"' % (k, _escape_param(v))
	return hvalue

def get_pubcontrol():
	return _get_pubcontrol()

def publish(channel, formats, id=None, prev_id=None, blocking=False,
		callback=None, meta={}):
	pub = _get_pubcontrol()
	pub.publish(_get_prefix() + channel,
		Item(formats, id=id, prev_id=prev_id, meta=meta),
		blocking=blocking,
		callback=callback)

def set_hold_longpoll(request, channels, timeout=None):
	gresp = request.grip.response
	gresp.hold = 'response'
	gresp.channels = _convert_channels(channels)
	if timeout:
		gresp.timeout = int(timeout)

# as a special case, channels can be None to not set channels, if they
#   were already set earlier by set_channels()
def set_hold_stream(request, channels):
	gresp = request.grip.response
	gresp.hold = 'stream'
	if channels is not None:
		gresp.channels = _convert_channels(channels)

def set_keep_alive(request, data, timeout=20):
	gresp = request.grip.response
	gresp.keep_alive = data
	gresp.keep_alive_timeout = int(timeout)

def set_sub_meta(request, name, value):
	gresp = request.grip.response
	gresp.meta[name] = value

def set_next_link(request, uri, timeout=None):
	gresp = request.grip.response
	gresp.next_link = uri
	if timeout:
		gresp.next_link_timeout = int(timeout)

# use this to set channel filters without necessarily having subscriptions
def set_channels(request, channels):
	gresp = request.grip.response
	gresp.channels = _convert_channels(channels)

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
		# make sure these fields are always set
		request.grip = GripData()
		request.wscontext = None

		# old
		request.grip_proxied = False
		request.grip_signed = False

		proxied = False
		signed = False

		grip_sig_header = request.META.get('HTTP_GRIP_SIG')
		if grip_sig_header:
			proxies = _get_proxies()

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
						proxied = True
						signed = True
						break
			else:
				# if even one proxy doesn't have a key, then
				#   don't require verification in order to
				#   consider the request to have been proxied
				proxied = True

		# parse Grip-Feature
		hvalue = request.META.get('HTTP_GRIP_FEATURE')
		if hvalue:
			parsed = parse_options_header(hvalue, multiple=True)
			features = set()
			for n in range(0, len(parsed), 2):
				features.add(parsed[n])
			request.grip.features = features

		# parse Grip-Last
		hvalue = request.META.get('HTTP_GRIP_LAST')
		if hvalue:
			parsed = parse_options_header(hvalue, multiple=True)

			last = {}
			for n in range(0, len(parsed), 2):
				channel = parsed[n]
				params = parsed[n + 1]
				last_id = params.get('last-id')
				if last_id is None:
					raise ValueError(
							'channel "%s" has no last-id param' % channel)
				last[channel] = last_id

			request.grip.last = last

		if hasattr(request, 'content_type'):
			content_type = request.content_type
		else:
			content_type = ''
			hvalue = request.META.get('CONTENT_TYPE')
			if hvalue:
				parsed = parse_options_header(hvalue)
				content_type = parsed[0]

		# detect WebSocket-Over-HTTP request
		wscontext = None
		if (request.method == 'POST' and
				content_type == 'application/websocket-events'):
			cid = request.META.get('HTTP_CONNECTION_ID')
			meta = {}
			for k, v in six.iteritems(request.META):
				if k.startswith('HTTP_META_'):
					meta[_convert_header_name(k[10:])] = v
			body = request.body
			if isinstance(body, six.text_type):
				body = body.encode('utf-8')
			try:
				events = decode_websocket_events(body)
			except:
				return HttpResponseBadRequest(
						'Error parsing WebSocket events.\n')

			wscontext = WebSocketContext(cid, meta, events,
					grip_prefix=_get_prefix())

		request.grip.proxied = proxied
		request.grip.signed = signed
		request.wscontext = wscontext

		# old
		request.grip_proxied = proxied
		request.grip_signed = signed

	def process_view(self, request, view_func, view_args, view_kwargs):
		if (getattr(view_func, 'websocket_only', False) and
				not request.wscontext):
			return HttpResponseBadRequest(
					'Request must contain WebSocket events.\n')

	def process_response(self, request, response):
		# if this was a successful websocket-events request, then hijack the
		#   response
		if (getattr(request, 'wscontext', None) and
				response.status_code == 200 and
				len(response.content) == 0):
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
			meta_set = {}
			for k, v in six.iteritems(wscontext.meta):
				lname = k.lower()
				need_set = True
				for ok, ov in wscontext.orig_meta:
					if lname == ok and v == ov:
						need_set = False
						break
				if need_set:
					meta_set[lname] = v

			events = []
			if wscontext.accepted:
				events.append(WebSocketEvent('OPEN'))
			events.extend(wscontext.out_events)
			if wscontext.closed:
				events.append(WebSocketEvent('CLOSE',
						pack('>H', wscontext.out_close_code)))

			response = HttpResponse(encode_websocket_events(events),
					content_type='application/websocket-events')
			if wscontext.accepted:
				response['Sec-WebSocket-Extensions'] = 'grip'
			for k in meta_remove:
				response['Set-Meta-' + k] = ''
			for k, v in six.iteritems(meta_set):
				response['Set-Meta-' + k] = v
		else:
			gresp = request.grip.response

			if (not gresp.is_empty() and not request.grip.proxied and
					getattr(settings, 'GRIP_PROXY_REQUIRED', False)):
				return HttpResponse('Not Implemented\n', status=501)

			if gresp.hold:
				# apply prefix to channels if needed
				prefix = _get_prefix()
				if prefix:
					for c in gresp.channels:
						c.name = prefix + c.name

				# code 304 only allows certain headers. if the webserver
				#   strictly enforces this, then we won't be able to use
				#   Grip- headers to talk to the proxy. switch to code 200
				#   and use Grip-Status to specify intended status
				if response.status_code == 304:
					response.status_code = 200
					response.reason_phrase = 'OK'
					response['Grip-Status'] = '304'

				response['Grip-Hold'] = gresp.hold

				response['Grip-Channel'] = create_grip_channel_header(
						gresp.channels)

				if gresp.timeout > 0:
					response['Grip-Timeout'] = str(gresp.timeout)

				if gresp.keep_alive:
					response['Grip-Keep-Alive'] = _keep_alive_header(
						gresp.keep_alive, gresp.keep_alive_timeout)

				if gresp.meta:
					response['Grip-Set-Meta'] = _set_meta_header(gresp.meta)

			if gresp.next_link:
				hvalue = '<%s>; rel=next' % gresp.next_link
				if gresp.next_link_timeout > 0:
					hvalue += '; timeout=%d' % gresp.next_link_timeout
				response['Grip-Link'] = hvalue

		return response
