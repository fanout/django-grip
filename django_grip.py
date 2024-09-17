from base64 import b64encode
from struct import pack
import threading
import six
from functools import WRAPPER_ASSIGNMENTS, wraps
from werkzeug.http import parse_options_header
import django
from django.conf import settings
from django.http import HttpResponse, HttpResponseBadRequest
from pubcontrol import Item
from gripcontrol import (
    Channel,
    GripPubControl,
    WebSocketEvent,
    WebSocketContext,
    parse_grip_uri,
    validate_sig,
    create_grip_channel_header,
    decode_websocket_events,
    encode_websocket_events,
)

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


class GripInstruct(object):
    def __init__(self):
        self.hold = ""
        self.channels = []
        self.timeout = 0
        self.keep_alive = ""
        self.keep_alive_timeout = 0
        self.next_link = ""
        self.next_link_timeout = 0
        self.meta = {}  # modify directly

    def add_channel(self, channel):
        if _is_basestring_instance(channel):
            channel = Channel(channel)
        assert isinstance(channel, Channel)
        self.channels.append(channel)

    def add_channels(self, channels):
        if isinstance(channels, Channel) or _is_basestring_instance(channels):
            channels = [channels]
        for c in channels:
            self.add_channel(c)

    def set_hold_longpoll(self, timeout=None):
        self.hold = "response"
        if timeout:
            self.timeout = int(timeout)

    def set_hold_stream(self):
        self.hold = "stream"

    def set_keep_alive(self, data, timeout):
        self.keep_alive = data
        self.keep_alive_timeout = int(timeout)

    def set_next_link(self, uri, timeout=None):
        self.next_link = uri
        if timeout:
            self.next_link_timeout = int(timeout)


class GripData(object):
    def __init__(self):
        self.proxied = False
        self.signed = False
        self.features = set()
        self.last = {}
        self.instruct = None

    def start_instruct(self):
        if self.instruct:
            raise ValueError("GRIP instruct already started")

        self.instruct = GripInstruct()
        return self.instruct


def _get_proxies():
    proxies = getattr(settings, "GRIP_PROXIES", [])
    grip_url = getattr(settings, "GRIP_URL", None)
    if grip_url:
        proxies.append(parse_grip_uri(grip_url))

    verify_key = getattr(settings, "GRIP_VERIFY_KEY", None)
    if verify_key:
        for p in proxies:
            if "verify_key" not in p:
                p["verify_key"] = verify_key

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
    return getattr(settings, "GRIP_PREFIX", "")


def _escape_param(s):
    out = ""
    for c in s:
        if c == '"':
            out += '\\"'
        else:
            out += c
    return out


def _cstring_encode(s):
    if not isinstance(s, six.text_type):
        s = s.decode("utf-8")
    out = ""
    for c in s:
        if c == "\\":
            out += "\\\\"
        if c == "\r":
            out += "\\r"
        elif c == "\n":
            out += "\\n"
        elif ord(c) < 0x20:
            raise ValueError("not cstring encodable")
        else:
            out += c
    return out


def _keep_alive_header(data, timeout):
    try:
        cs = _cstring_encode(data)
        hvalue = "%s; format=cstring" % cs
    except Exception:
        hvalue = "%s; format=base64" % b64encode(data)

    hvalue += "; timeout=%d" % timeout
    return hvalue


def _set_meta_header(meta):
    hvalue = ""
    for k, v in six.iteritems(meta):
        if len(hvalue) > 0:
            hvalue += ", "
        hvalue += '%s="%s"' % (k, _escape_param(v))
    return hvalue


def get_pubcontrol():
    return _get_pubcontrol()


def publish(
    channel, formats, id=None, prev_id=None, blocking=False, callback=None, meta={}
):
    pub = _get_pubcontrol()
    pub.publish(
        _get_prefix() + channel,
        Item(formats, id=id, prev_id=prev_id, meta=meta),
        blocking=blocking,
        callback=callback,
    )


def set_hold_longpoll(request, channels, timeout=None):
    instruct = request.grip.start_instruct()
    instruct.add_channels(channels)
    instruct.set_hold_longpoll(timeout=timeout)


def set_hold_stream(request, channels, keep_alive_data=None, keep_alive_timeout=None):
    instruct = request.grip.start_instruct()
    instruct.add_channels(channels)
    instruct.set_hold_stream()
    if keep_alive_data:
        if not keep_alive_timeout:
            raise ValueError(
                "if keep_alive_data is set, then " "keep_alive_timeout must also be set"
            )
        instruct.set_keep_alive(keep_alive_data, timeout=keep_alive_timeout)


def _convert_header_name(name):
    out = ""
    for c in name:
        if c == "_":
            out += "-"
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
    return wraps(view_func, assigned=WRAPPER_ASSIGNMENTS)(wrapped_view)


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

        grip_sig_header = request.META.get("HTTP_GRIP_SIG")
        if grip_sig_header:
            proxies = _get_proxies()

            all_proxies_have_keys = True
            for entry in proxies:
                if "verify_key" not in entry and "key" not in entry:
                    all_proxies_have_keys = False
                    break

            if all_proxies_have_keys:
                # if all proxies have keys, then don't
                #   consider the request to be proxied unless
                #   one of them signed it
                for entry in proxies:
                    key = entry.get("verify_key", entry["key"])
                    if validate_sig(grip_sig_header, key, iss=entry.get("verify_iss")):
                        proxied = True
                        signed = True
                        break
            else:
                # if even one proxy doesn't have a key, then
                #   don't require verification in order to
                #   consider the request to have been proxied
                proxied = True

        # parse Grip-Feature
        hvalue = request.META.get("HTTP_GRIP_FEATURE")
        if hvalue:
            parsed = hvalue.split(", ")
            features = set()
            features.update(parsed)
            request.grip.features = features

        # parse Grip-Last
        hvalue = request.META.get("HTTP_GRIP_LAST")
        if hvalue:
            last = {}
            for hval in hvalue.split(","):
                channel, params = parse_options_header(hval)
                last[channel] = params.get("last-id", "")

            request.grip.last = last

        if hasattr(request, "content_type"):
            content_type = request.content_type
        else:
            content_type = ""
            hvalue = request.META.get("CONTENT_TYPE")
            if hvalue:
                parsed = parse_options_header(hvalue)
                content_type = parsed[0]

        # detect WebSocket-Over-HTTP request
        wscontext = None
        if request.method == "POST" and content_type == "application/websocket-events":
            cid = request.META.get("HTTP_CONNECTION_ID")
            meta = {}
            for k, v in six.iteritems(request.META):
                if k.startswith("HTTP_META_"):
                    meta[_convert_header_name(k[10:])] = v
            body = request.body
            if isinstance(body, six.text_type):
                body = body.encode("utf-8")
            try:
                events = decode_websocket_events(body)
            except:
                return HttpResponseBadRequest("Error parsing WebSocket events.\n")

            wscontext = WebSocketContext(cid, meta, events, grip_prefix=_get_prefix())

        request.grip.proxied = proxied
        request.grip.signed = signed
        request.wscontext = wscontext

        # old
        request.grip_proxied = proxied
        request.grip_signed = signed

    def process_view(self, request, view_func, view_args, view_kwargs):
        if getattr(view_func, "websocket_only", False) and not request.wscontext:
            return HttpResponseBadRequest("Request must contain WebSocket events.\n")

    def process_response(self, request, response):
        # if this was a successful websocket-events request, then hijack the
        #   response
        if (
            getattr(request, "wscontext", None)
            and response.status_code == 200
            and len(response.content) == 0
        ):
            wscontext = request.wscontext

            # meta to remove?
            meta_remove = set()
            for k, v in six.iteritems(wscontext.orig_meta):
                found = False
                for nk, nv in six.iteritems(wscontext.meta):
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
                for ok, ov in six.iteritems(wscontext.orig_meta):
                    if lname == ok and v == ov:
                        need_set = False
                        break
                if need_set:
                    meta_set[lname] = v

            events = []
            if wscontext.accepted:
                events.append(WebSocketEvent("OPEN"))
            events.extend(wscontext.out_events)
            if wscontext.closed:
                events.append(
                    WebSocketEvent("CLOSE", pack(">H", wscontext.out_close_code))
                )

            response = HttpResponse(
                encode_websocket_events(events),
                content_type="application/websocket-events",
            )
            if wscontext.accepted:
                response["Sec-WebSocket-Extensions"] = "grip"
            for k in meta_remove:
                response["Set-Meta-" + k] = ""
            for k, v in six.iteritems(meta_set):
                response["Set-Meta-" + k] = v
        else:
            instruct = request.grip.instruct

            if instruct:
                if not request.grip.proxied and getattr(
                    settings, "GRIP_PROXY_REQUIRED", False
                ):
                    return HttpResponse("Not Implemented\n", status=501)

                if instruct.hold:
                    # code 304 only allows certain headers. if the webserver
                    #   strictly enforces this, then we won't be able to use
                    #   Grip- headers to talk to the proxy. switch to code
                    #   200 and use Grip-Status to specify intended status
                    if response.status_code == 304:
                        response.status_code = 200
                        response.reason_phrase = "OK"
                        response["Grip-Status"] = "304"

                    response["Grip-Hold"] = instruct.hold

                # apply prefix to channels if needed
                prefix = _get_prefix()
                if prefix:
                    for c in instruct.channels:
                        c.name = prefix + c.name

                response["Grip-Channel"] = create_grip_channel_header(instruct.channels)

                if instruct.hold:
                    if instruct.timeout > 0:
                        response["Grip-Timeout"] = str(instruct.timeout)

                    if instruct.keep_alive:
                        response["Grip-Keep-Alive"] = _keep_alive_header(
                            instruct.keep_alive, instruct.keep_alive_timeout
                        )

                if instruct.meta:
                    response["Grip-Set-Meta"] = _set_meta_header(instruct.meta)

                if instruct.next_link:
                    hvalue = "<%s>; rel=next" % instruct.next_link
                    if instruct.next_link_timeout > 0:
                        hvalue += "; timeout=%d" % instruct.next_link_timeout
                    response["Grip-Link"] = hvalue

        return response
