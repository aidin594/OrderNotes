"""Dependency-free local SMSGate API client. No POST retries on ambiguous outcomes."""
import base64
import ipaddress
import json
from urllib import error, request
from urllib.parse import urlsplit
from phone import normalized_phone, valid_phone


class GatewayError(Exception):
    def __init__(self, message, uncertain=False):
        super().__init__(message)
        self.uncertain = uncertain


class _NoRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, hdrs, newurl):
        return None


def check_url(value: str) -> str:
    p = urlsplit((value or '').strip())
    if p.scheme not in ('http', 'https') or not p.hostname or p.path not in ('', '/') or p.query or p.fragment or p.username or p.password:
        raise GatewayError('Enter a local gateway URL, such as http://192.168.1.10:8080')
    try:
        addr = ipaddress.ip_address(p.hostname)
        if not (addr.is_private or addr.is_loopback) or addr.is_link_local:
            raise GatewayError('Gateway must use a private/local address.')
    except ValueError:
        if p.hostname.lower() != 'localhost':
            raise GatewayError('Gateway must be a private IP or localhost.')
    try:
        port = p.port
        if port is not None and not 1 <= port <= 65535:
            raise GatewayError('Invalid gateway port.')
    except ValueError as exc:
        raise GatewayError('Invalid gateway port.') from exc
    return value.strip().rstrip('/')


class Gateway:
    def __init__(self, url, username, password, country='46'):
        self.url = check_url(url)
        if not username or not password:
            raise GatewayError('Enter gateway username and password in Settings.')
        self.country = country
        token = base64.b64encode(f'{username}:{password}'.encode('utf-8')).decode('ascii')
        self.auth = 'Basic ' + token
        self.opener = request.build_opener(_NoRedirect)

    def _request(self, route, payload=None):
        data = json.dumps(payload, ensure_ascii=False).encode('utf-8') if payload is not None else None
        req = request.Request(
            self.url + route,
            data=data,
            method='POST' if data is not None else 'GET',
            headers={'Authorization': self.auth, 'Accept': 'application/json', 'Content-Type': 'application/json'},
        )
        sending = data is not None
        try:
            with self.opener.open(req, timeout=8) as resp:
                raw = resp.read()
                if not raw:
                    return {'accepted': True, 'http_status': resp.status} if sending else {}
                try:
                    result = json.loads(raw)
                    return result if isinstance(result, (dict, list)) else {'response': result}
                except ValueError:
                    if sending and resp.status in (200, 201, 202, 204):
                        return {'accepted': True, 'http_status': resp.status}
                    raise GatewayError('Unexpected response from SMS gateway.', uncertain=sending)
        except error.HTTPError as exc:
            if exc.code in (301, 302, 303, 307, 308):
                raise GatewayError('Gateway redirected the connection. Check its URL.') from exc
            if exc.code in (401, 403):
                raise GatewayError('Authentication failed; check gateway login.') from exc
            raise GatewayError(f'Gateway HTTP error {exc.code}.') from exc
        except (error.URLError, TimeoutError, OSError) as exc:
            # A timeout can happen after the server accepted a message.
            raise GatewayError('Gateway unavailable or timed out. Check outgoing messages before retrying.', uncertain=sending) from exc

    def health(self):
        return self._request('/health')

    def send(self, phone: str, message: str, sim: int):
        dest = normalized_phone(phone, self.country)
        if not valid_phone(phone, self.country):
            raise GatewayError('Enter a valid phone number (6–16 digits).')
        if not (message or '').strip():
            raise GatewayError('SMS message is empty.')
        if sim not in (1, 2):
            raise GatewayError('Select SIM 1 or SIM 2.')
        payload = {
            'phoneNumbers': [dest],
            'textMessage': {'text': message},
            'withDeliveryReport': True,
            'simNumber': sim,
        }
        return self._request('/messages', payload)
