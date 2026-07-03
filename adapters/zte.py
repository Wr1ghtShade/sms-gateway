"""ZTE MC/MF series adapter.

Uses python-zte-mc801a for authentication and signal data.
SMS send/delete are implemented directly (not in the library).

Supported models (non-exhaustive): MC801a, MC889, MF286, MF289, MF286D,
MF287, MF28D, MF90 — any ZTE CPE/router with the goform HTTP API.

Authentication flow:
  1. GET  /goform/goform_get_cmd_process?cmd=LD  → {LD}
  2. pwd  = SHA256( SHA256(password).upper() + LD ).upper()
  3. GET  /goform/goform_set_cmd_process?goformId=LOGIN&password=pwd → stok cookie
  4. Subsequent requests carry the stok cookie.

Write operations require an AD token:
  AD = MD5( MD5(wa_inner_version + cr_version) + RD ).hexdigest()
  Obtained by querying cmd=wa_inner_version,cr_version,RD.

SMS content is UCS-2 hex (UTF-16 BE).

Thread-safety: each public method creates its own requests.Session, uses it
for the full operation, then closes it. No session state is stored on the
instance — safe for concurrent Flask requests.
"""
import codecs
import hashlib
import logging
from contextlib import contextmanager
import requests

from .base import RouterAdapter, NotSupportedError

log = logging.getLogger(__name__)

_SMS_FIELDS = 'sms_data_total'
_SIG_FIELDS = 'signalbar,network_type,network_provider,lte_rsrp,lte_rsrq,lte_snr'
_AD_FIELDS  = 'wa_inner_version,cr_version,RD'


class ZteAdapter(RouterAdapter):
    """Adapter for ZTE CPE routers with the goform HTTP API."""

    brand = "zte"
    supports_inbox  = True
    supports_outbox = False   # goform API doesn't expose a sent-SMS view

    def __init__(self, ip: str, password: str, user: str = 'admin'):
        self._ip       = ip
        self._password = password
        self._base     = f'http://{ip}'

    # ── Private helpers ──────────────────────────────────────────────────────

    def _get(self, session: requests.Session, params: dict) -> dict:
        r = session.get(
            f'{self._base}/goform/goform_get_cmd_process',
            params={'isTest': 'false', **params},
            headers={'Referer': f'{self._base}/'},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _post(self, session: requests.Session, data: dict) -> dict:
        r = session.post(
            f'{self._base}/goform/goform_set_cmd_process',
            data={'isTest': 'false', **data},
            headers={
                'Referer': f'{self._base}/'},
            timeout=15,
        )
        r.raise_for_status()
        return r.json()

    def _login(self) -> requests.Session:
        """Open a new session, authenticate and return it.

        The caller owns the session and must call session.close() when done.
        Nothing is stored on self — each call is independent.
        """
        session = requests.Session()
        session.headers['User-Agent'] = (
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
        )

        # Step 1: get LD challenge
        r_ld = session.get(
            f'{self._base}/goform/goform_get_cmd_process',
            params={'isTest': 'false', 'cmd': 'LD'},
            headers={'Referer': f'{self._base}/'},
            timeout=15,
        )
        r_ld.raise_for_status()
        ld = r_ld.json()['LD']

        # Step 2: hash password
        h1 = hashlib.sha256(self._password.encode()).hexdigest().upper()
        h2 = hashlib.sha256(f'{h1}{ld}'.encode()).hexdigest().upper()

        # Step 3: login
        r_login = session.get(
            f'{self._base}/goform/goform_set_cmd_process',
            params={'isTest': 'false', 'goformId': 'LOGIN', 'password': h2},
            headers={'Referer': f'{self._base}/'},
            timeout=15,
        )
        r_login.raise_for_status()
        result = r_login.json().get('result', '')
        if result != '0':
            session.close()
            raise ConnectionError(
                f'Connexion ZTE échouée ({self._ip}) — résultat: {result!r}'
            )

        return session

    @contextmanager
    def _client(self):
        """Yield a fresh, authenticated session, closed automatically on exit."""
        session = self._login()
        try:
            yield session
        finally:
            session.close()

    def _get_ad(self, session: requests.Session) -> str:
        """Compute the AD write-token from router version fields."""
        data = self._get(session, {'cmd': _AD_FIELDS, 'multi_data': '1'})
        m1 = hashlib.md5(
            (data['wa_inner_version'] + data['cr_version']).encode()
        ).hexdigest()
        return hashlib.md5(f'{m1}{data["RD"]}'.encode()).hexdigest()

    @staticmethod
    def _decode_content(hex_content: str) -> str:
        """Decode UCS-2 hex to a readable string."""
        try:
            return bytes.fromhex(hex_content).decode('utf-16-be')
        except Exception:
            try:
                return codecs.decode(hex_content, 'hex').replace(b'\x00', b'').decode('latin-1')
            except Exception:
                return hex_content

    @staticmethod
    def _encode_content(message: str) -> str:
        """Encode a string to UCS-2 hex for sending."""
        return message.encode('utf-16-be').hex().upper()

    @staticmethod
    def _parse_date(raw: str) -> str:
        """Convert ZTE date format 'YY,MM,DD,HH,MM,SS,TZ' to readable string."""
        try:
            parts = raw.split(',')
            return f'20{parts[0]}-{parts[1]}-{parts[2]} {parts[3]}:{parts[4]}:{parts[5]}'
        except Exception:
            return raw

    # ── RouterAdapter interface ───────────────────────────────────────────────

    def send_sms(self, numbers: list, message: str) -> None:
        with self._client() as session:
            ad = self._get_ad(session)
            body_hex = self._encode_content(message)
            for number in numbers:
                result = self._post(session, {
                    'goformId':    'SEND_SMS',
                    'Number':      number,
                    'MessageBody': body_hex,
                    'ID':          '-1',
                    'encode_type': 'UNICODE',
                    'AD':          ad,
                })
                if result.get('result') not in ('success', '0', 0):
                    log.warning('ZTE send_sms result inattendu : %s', result)

    def get_inbox(self, page: int = 1, per_page: int = 20) -> dict:
        with self._client() as session:
            # ZTE pages are 0-indexed
            data = self._get(session, {
                'cmd':           'sms_data_total',
                'page':          str(page - 1),
                'data_per_page': str(per_page),
                'mem_store':     '1',   # 1 = inbox
                'tags':          '10',  # received messages
                'order_by':      'order by id desc',
            })

        messages = []
        for msg in (data.get('messages') or []):
            messages.append({
                'Index':   str(msg.get('id', '')),
                'Phone':   msg.get('number') or msg.get('from') or '—',
                'Content': self._decode_content(msg.get('content', '')),
                'Date':    self._parse_date(msg.get('date', '—')),
            })

        # ZTE doesn't tell us total count easily — use has_more heuristic
        return {
            'messages': messages,
            'page':     page,
            'has_more': len(messages) == per_page,
        }

    def get_outbox(self, page: int = 1, per_page: int = 50) -> dict:
        raise NotSupportedError("ZTE n'expose pas la boîte d'envoi via son API.")

    def delete_sms(self, index) -> None:
        with self._client() as session:
            ad = self._get_ad(session)
            self._post(session, {
                'goformId':    'DELETE_SMS',
                'msg_id':      str(index),
                'notCallback': 'true',
                'AD':          ad,
            })

    def delete_sms_batch(self, indices: list) -> int:
        """Single session for the whole batch."""
        with self._client() as session:
            ad = self._get_ad(session)
            for idx in indices:
                self._post(session, {
                    'goformId':    'DELETE_SMS',
                    'msg_id':      str(idx),
                    'notCallback': 'true',
                    'AD':          ad,
                })
        return len(indices)

    def get_status(self) -> dict:
        with self._client() as session:
            data = self._get(session, {'cmd': _SIG_FIELDS, 'multi_data': '1'})

        try:
            bars = int(data.get('signalbar', 0))
        except (ValueError, TypeError):
            bars = 0

        return {
            'status':      'ok',
            'signal_bars': bars,
            'network':     data.get('network_type') or '—',
            'operator':    data.get('network_provider') or '—',
        }

    def check_health(self) -> dict:
        with self._client():
            pass   # login succeeded → router is reachable
        return {'status': 'ok', 'router': 'reachable'}
