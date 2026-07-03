"""GL.iNet router adapter using python-glinet (JSON-RPC 2.0, firmware 4.x+)."""
import logging
import os
import tempfile
from contextlib import contextmanager
from .base import RouterAdapter, NotSupportedError

log = logging.getLogger(__name__)

# Dossier cache isolé pour éviter les conflits avec le user www-data
_CACHE_DIR = os.path.join(tempfile.gettempdir(), 'glinet_cache')


class GlinetAdapter(RouterAdapter):
    """Adapter for GL.iNet routers with firmware 4.x+ and a cellular modem.

    Supported models (non-exhaustive): GL-X3000, GL-XE3000, GL-X750, GL-MiFi,
    GL-E750, GL-AP1300LTE — any GL.iNet device with a built-in LTE/5G modem.

    Authentication uses challenge-response (SHA256/MD5/SHA512) via python-glinet.
    The keep-alive background thread is disabled to stay Flask-compatible.

    The modem ``bus`` path (e.g. ``1-1`` for USB, ``0001:01:00.0`` for PCIe) is
    auto-discovered at first use and cached for the lifetime of the adapter.
    It can also be forced via the ``bus`` constructor parameter.
    """

    brand = "glinet"
    supports_inbox = True
    supports_outbox = False   # GL.iNet API doesn't expose sent SMS

    def __init__(self, ip: str, password: str, user: str = 'root', bus: str = None):
        self._ip = ip
        self._password = password
        self._username = user or 'root'
        self._url = f'https://{ip}/rpc'
        self._bus = bus          # None → auto-discovered on first use
        # Cache dir may contain serialised credentials — keep it private to
        # the running user. Chmod every time to repair perms if something
        # else loosened them.
        os.makedirs(_CACHE_DIR, exist_ok=True)
        try:
            os.chmod(_CACHE_DIR, 0o700)
        except OSError as e:
            log.warning("Impossible de durcir les permissions du cache GL.iNet : %s", e)

    # ── Private helpers ──────────────────────────────────────────────────────

    def _get_client(self):
        """Create, login and return a GlInet client (no keep-alive thread)."""
        from pyglinet import GlInet
        return GlInet(
            url=self._url,
            username=self._username,
            password=self._password,
            keep_alive=False,
            verify_ssl_certificate=False,
            cache_folder=_CACHE_DIR,
        ).login()

    @contextmanager
    def _client(self):
        """Yield a logged-in client, logging out automatically on exit."""
        client = self._get_client()
        try:
            yield client
        finally:
            client.logout()

    def _discover_bus(self, client) -> str | None:
        """Try to discover the modem bus path from get_info."""
        if self._bus:
            return self._bus
        try:
            result = client.request('call', ['modem', 'get_info'])
            info = getattr(result, 'result', result)
            # Response varies by model — handle list and dict shapes
            modems = None
            if isinstance(info, list):
                modems = info
            elif hasattr(info, 'modems'):
                modems = info.modems
            elif hasattr(info, '__dict__'):
                # Flatten: some firmwares return {bus: {info...}}
                for attr in ('bus', 'path', 'device'):
                    val = getattr(info, attr, None)
                    if val and isinstance(val, str):
                        self._bus = val
                        return self._bus

            if modems and len(modems) > 0:
                first = modems[0]
                for key in ('bus', 'path', 'device'):
                    val = first.get(key) if isinstance(first, dict) else getattr(first, key, None)
                    if val:
                        self._bus = val
                        log.info("GL.iNet bus auto-découvert : %s", self._bus)
                        return self._bus
        except Exception as e:
            log.warning("Impossible d'auto-découvrir le bus GL.iNet : %s", e)
        return None

    def _call(self, client, method: str, params: dict = None) -> dict:
        """Make a modem RPC call, injecting the bus if known."""
        payload = {}
        bus = self._discover_bus(client)
        if bus:
            payload['bus'] = bus
        if params:
            payload.update(params)
        result = client.request('call', ['modem', method, payload] if payload else ['modem', method])
        return getattr(result, 'result', result)

    @staticmethod
    def _parse_sms(raw) -> list:
        """Normalise GL.iNet SMS objects to our standard dict format."""
        items = []
        sms_list = None

        if isinstance(raw, list):
            sms_list = raw
        elif hasattr(raw, 'sms_list'):
            sms_list = raw.sms_list
        elif hasattr(raw, 'messages'):
            sms_list = raw.messages
        elif isinstance(raw, dict):
            sms_list = raw.get('sms_list') or raw.get('messages') or []

        if not sms_list:
            return []

        for sms in sms_list:
            if isinstance(sms, dict):
                items.append({
                    'Index': str(sms.get('hash') or sms.get('id') or sms.get('index', '')),
                    'Phone': sms.get('sender') or sms.get('phone_number') or '—',
                    'Content': sms.get('content') or sms.get('body') or sms.get('text') or '',
                    'Date': sms.get('date') or sms.get('time') or '—',
                })
            else:
                # python-glinet ResultContainer object
                items.append({
                    'Index': str(getattr(sms, 'hash', None) or getattr(sms, 'id', None) or ''),
                    'Phone': getattr(sms, 'sender', None) or getattr(sms, 'phone_number', '—'),
                    'Content': getattr(sms, 'content', None) or getattr(sms, 'body', '') or '',
                    'Date': getattr(sms, 'date', None) or getattr(sms, 'time', '—') or '—',
                })
        return items

    # ── RouterAdapter interface ───────────────────────────────────────────────

    def send_sms(self, numbers: list, message: str) -> None:
        with self._client() as client:
            bus = self._discover_bus(client)
            for number in numbers:
                params = {'phone_number': number, 'body': message, 'timeout': 10}
                if bus:
                    params['bus'] = bus
                client.request('call', ['modem', 'send_sms', params])

    def get_inbox(self, page: int = 1, per_page: int = 20) -> dict:
        with self._client() as client:
            raw = self._call(client, 'get_sms_list')
            all_sms = self._parse_sms(raw)

        # Sort newest first, paginate in memory
        start = (page - 1) * per_page
        end = start + per_page
        return {
            'messages': all_sms[start:end],
            'page': page,
            'has_more': len(all_sms) > end,
        }

    def get_outbox(self, page: int = 1, per_page: int = 50) -> dict:
        raise NotSupportedError(
            "GL.iNet n'expose pas la boîte d'envoi via son API."
        )

    def delete_sms(self, index) -> None:
        with self._client() as client:
            bus = self._discover_bus(client)
            params = {'hash': str(index)}
            if bus:
                params['bus'] = bus
            client.request('call', ['modem', 'remove_sms', params])

    def delete_sms_batch(self, indices: list) -> int:
        with self._client() as client:
            bus = self._discover_bus(client)
            for idx in indices:
                params = {'hash': str(idx)}
                if bus:
                    params['bus'] = bus
                client.request('call', ['modem', 'remove_sms', params])
        return len(indices)

    def get_status(self) -> dict:
        with self._client() as client:
            raw = self._call(client, 'get_cells_info')

        # Normalise cells info — field names vary by model/firmware
        def _get(obj, *keys, default='—'):
            for k in keys:
                val = obj.get(k) if isinstance(obj, dict) else getattr(obj, k, None)
                if val is not None:
                    return val
            return default

        signal_dbm = _get(raw, 'rsrp', 'rssi', 'signal', default=None)
        # Convert dBm to 0-5 bars (rough scale: -140→0, -50→5)
        if signal_dbm is not None:
            try:
                bars = min(5, max(0, round((float(signal_dbm) + 140) / 18)))
            except (ValueError, TypeError):
                bars = 0
        else:
            bars = 0

        return {
            'status':      'ok',
            'signal_bars': bars,
            'network':     _get(raw, 'network_type', 'rat', 'mode', 'type'),
            'operator':    _get(raw, 'operator', 'provider', 'plmn_name', 'carrier'),
        }

    def check_health(self) -> dict:
        with self._client() as client:
            client.request('call', ['modem', 'get_status'])
        return {'status': 'ok', 'router': 'reachable'}
