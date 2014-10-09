import threading
import atexit
from django.conf import settings
from pubcontrol import PubControl, Item
from gripcontrol import Channel

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
		if hasattr(settings, 'PUBLISH_SERVERS'):
			pcs.apply_config(settings.PUBLISH_SERVERS)
		if hasattr(settings, 'GRIP_PROXIES'):
			pcs.apply_grip_config(settings.GRIP_PROXIES)

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
