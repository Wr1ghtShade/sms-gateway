"""Netgear LTE router adapter using eternalegypt (async wrapped for Flask)."""
import asyncio
import threading
import aiohttp
import eternalegypt
from .base import RouterAdapter, NotSupportedError


class NetgearAdapter(RouterAdapter):
    """Adapter for Netgear LTE modems (LB1120, LB2120, MR1100…).

    eternalegypt is fully async (asyncio + aiohttp). Rather than opening a
    new aiohttp session and logging in again on every call, this adapter
    runs a single background event loop for its lifetime and keeps the
    session + authenticated modem alive across calls — the UI polls
    get_status() every few seconds, so a fresh login per call was the
    heaviest network/CPU cost in the project.
    """

    brand = "netgear"
    supports_inbox = True
    supports_outbox = False   # Netgear API exposes inbox only

    def __init__(self, ip: str, password: str):
        self._ip = ip
        self._password = password

        self._session = None
        self._modem = None
        self._modem_lock = asyncio.Lock()

        # Coroutines are submitted to this loop from Flask's request threads
        # (and from background bulk-send/delete threads) via
        # run_coroutine_threadsafe — the loop itself only ever runs on
        # _loop_thread, so eternalegypt/aiohttp never see concurrent access.
        self._loop = asyncio.new_event_loop()
        self._loop_thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._loop_thread.start()

    # --- Lifecycle ---

    def close(self) -> None:
        """Tear down the persistent session and stop the background loop."""
        if not self._loop.is_running():
            return
        fut = asyncio.run_coroutine_threadsafe(self._aclose(), self._loop)
        try:
            fut.result(timeout=5)
        except Exception:
            pass
        self._loop.call_soon_threadsafe(self._loop.stop)

    async def _aclose(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None
        self._modem = None

    # --- Async helpers ---

    def _run(self, coro):
        """Submit a coroutine to the persistent loop and block for the result."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result()

    async def _ensure_modem(self):
        if self._modem is not None:
            return self._modem
        async with self._modem_lock:
            if self._modem is None:  # re-check: another coroutine may have won the race
                jar = aiohttp.CookieJar(unsafe=True)
                self._session = aiohttp.ClientSession(cookie_jar=jar)
                modem = eternalegypt.Modem(hostname=self._ip, websession=self._session)
                await modem.login(password=self._password)
                self._modem = modem
        return self._modem

    async def _with_modem(self, fn):
        """Run fn against the persistent, authenticated modem session."""
        modem = await self._ensure_modem()
        try:
            return await fn(modem)
        except eternalegypt.Error:
            # The session may have expired server-side — drop it so the
            # *next* call re-authenticates instead of failing repeatedly on
            # a dead session. Not retried here to avoid replaying a
            # send_sms whose delivery status is unknown.
            await self._aclose()
            raise

    # --- RouterAdapter interface ---

    def send_sms(self, numbers: list, message: str) -> None:
        async def _send(modem):
            for number in numbers:
                await modem.sms(phone=number, message=message)

        self._run(self._with_modem(_send))

    def get_inbox(self, page: int = 1, per_page: int = 20) -> dict:
        async def _fetch(modem):
            info = await modem.information()
            return info.sms

        all_sms = self._run(self._with_modem(_fetch))

        # Sort newest first
        all_sms = sorted(all_sms, key=lambda s: s.id, reverse=True)

        # Paginate in memory (Netgear returns all at once)
        start = (page - 1) * per_page
        end = start + per_page
        page_sms = all_sms[start:end]

        messages = [
            {
                'Index': str(sms.id),
                'Phone': sms.sender or '—',
                'Content': sms.message or '',
                'Date': (
                    sms.timestamp.strftime('%Y-%m-%d %H:%M:%S')
                    if sms.timestamp else '—'
                ),
            }
            for sms in page_sms
        ]

        return {
            'messages': messages,
            'page': page,
            'has_more': len(all_sms) > end,
        }

    def get_outbox(self, page: int = 1, per_page: int = 50) -> dict:
        raise NotSupportedError(
            "Les routeurs Netgear n'exposent pas la boîte d'envoi via leur API."
        )

    def delete_sms(self, index) -> None:
        async def _delete(modem):
            await modem.delete_sms(int(index))

        self._run(self._with_modem(_delete))

    def delete_sms_batch(self, indices: list) -> int:
        """Delete multiple messages in a single session (Netgear has no batch delete)."""
        async def _delete_all(modem):
            for idx in indices:
                await modem.delete_sms(int(idx))

        self._run(self._with_modem(_delete_all))
        return len(indices)

    def get_status(self) -> dict:
        async def _fetch(modem):
            return await modem.information()

        info = self._run(self._with_modem(_fetch))

        # radio_quality is 0-100 → map to 0-5 bars
        quality = info.radio_quality or 0
        signal_bars = min(round(quality / 20), 5)

        # Pick the most informative network label available
        network = (
            info.connection_type
            or info.current_nw_service_type
            or info.current_ps_service_type
            or '—'
        )
        operator = info.register_network_display or '—'

        return {
            'status': 'ok',
            'signal_bars': signal_bars,
            'network': network,
            'operator': operator,
        }

    def check_health(self) -> dict:
        async def _ping(modem):
            await modem.information()

        self._run(self._with_modem(_ping))
        return {'status': 'ok', 'router': 'reachable'}
