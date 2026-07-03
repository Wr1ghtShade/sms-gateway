from flask import Flask, request, jsonify, render_template, Response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from dataclasses import dataclass, field
from functools import wraps
from typing import Optional
import ipaddress
import json
import os
import re
import secrets
import threading
import time
import logging

from adapters import get_adapter, ADAPTER_META, ADAPTERS, NotSupportedError

# ---------------------------------------------------------------------------
# CONFIG — router_config.json est la seule source de vérité.
# S'il n'existe pas, le service démarre sans routeur configuré :
# les routes SMS renvoient 503 jusqu'à ce que l'utilisateur
# configure via l'onglet ⚙️ Config.
# ---------------------------------------------------------------------------
CONFIG_PATH = os.path.join(os.path.dirname(__file__), 'router_config.json')
_CONFIG_EMPTY = {'brand': '', 'ip': '', 'user': '', 'pass': ''}

def load_config() -> dict:
    """Load router config from router_config.json, or return empty config.

    Falls back to an empty config (with a clear log) if the file is corrupted
    or unreadable, so the service can still start and let the user fix things
    via the ⚙️ Config UI.
    """
    if not os.path.exists(CONFIG_PATH):
        return dict(_CONFIG_EMPTY)
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise ValueError(f"router_config.json racine doit être un dict, pas {type(cfg).__name__}")
        # Ensure all required keys exist (avoid KeyError later)
        return {**_CONFIG_EMPTY, **cfg}
    except (json.JSONDecodeError, OSError, ValueError) as e:
        # Don't crash on boot — let the UI repair the config
        logging.getLogger(__name__).error(
            "router_config.json illisible (%s) — démarrage avec config vide", e
        )
        return dict(_CONFIG_EMPTY)

def save_config(cfg: dict) -> None:
    # Atomic write: write to .tmp then rename — prevents JSON corruption on crash.
    # fsync forces the data to physical disk before the rename (survives a
    # power loss on the Pi), and fchmod pins the permissions to 660
    # regardless of umask so www-data keeps read/write access after the swap.
    tmp = CONFIG_PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(cfg, f, indent=2)
        f.flush()
        os.fsync(f.fileno())
        os.fchmod(f.fileno(), 0o660)
    os.replace(tmp, CONFIG_PATH)

# Module-level adapter (None si pas encore configuré).
# _adapter_lock protège les réassignations concurrentes de _config / _adapter.
_adapter_lock = threading.Lock()
_config  = load_config()
_adapter = get_adapter(_config) if _config.get('pass') else None

def current_adapter():
    # Snapshot under the lock so we never return an adapter that is mid-swap.
    with _adapter_lock:
        adapter = _adapter
    if adapter is None:
        from flask import abort
        abort(503, description="Routeur non configuré. Rendez-vous dans l'onglet ⚙️ Config.")
    return adapter

def reload_adapter(cfg: dict):
    """Swap config and adapter atomically.

    Builds the new adapter outside the lock (may raise ValueError on unknown
    brand), then publishes both _config and _adapter under the lock so a
    concurrent request never sees a torn (new-config, old-adapter) state.
    """
    new_adapter = get_adapter(cfg) if cfg.get('pass') else None
    global _adapter, _config
    with _adapter_lock:
        old_adapter = _adapter
        _config  = cfg
        _adapter = new_adapter
    if old_adapter is not None:
        old_adapter.close()

# ---------------------------------------------------------------------------
# LOGGING — sanitise le mot de passe de tout log/traceback
# ---------------------------------------------------------------------------
def _get_password() -> str:
    return _config.get('pass', '')

class _SanitizingFormatter(logging.Formatter):
    def format(self, record):
        msg = super().format(record)
        pwd = _get_password()
        return msg.replace(pwd, '********') if pwd else msg

_handler = logging.StreamHandler()
_handler.setFormatter(_SanitizingFormatter('%(asctime)s [%(levelname)s] %(message)s'))
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_handler)
logger = logging.getLogger(__name__)

def sanitize_exception(e) -> str:
    err = str(e)
    pwd = _get_password()
    return err.replace(pwd, '********') if pwd else err

# ---------------------------------------------------------------------------
# CONSTANTES
# ---------------------------------------------------------------------------
MAX_MESSAGE_LENGTH = 480   # ~3 SMS GSM-7
MAX_BULK_TASKS     = 200

PHONE_RE = re.compile(r'^(0[67][0-9]{8}|\+33[67][0-9]{8})$')

# ---------------------------------------------------------------------------
# VALIDATION IP — anti-SSRF : seules les IPv4 privées sont acceptées
# (un routeur 4G/5G est toujours sur le réseau local)
# ---------------------------------------------------------------------------
def validate_router_ip(ip: str) -> bool:
    """Accept only valid private IPv4 addresses (anti-SSRF)."""
    try:
        addr = ipaddress.ip_address(ip)
        return addr.version == 4 and addr.is_private
    except ValueError:
        return False

def normalize_number(number: str) -> str:
    return number.replace(' ', '').replace('-', '').replace('.', '')

def validate_number(number: str) -> bool:
    return bool(PHONE_RE.match(normalize_number(number)))

# ---------------------------------------------------------------------------
# ÉTAT ARRIÈRE-PLAN
# ---------------------------------------------------------------------------
@dataclass
class BulkDeleteState:
    lock:          threading.Lock  = field(default_factory=threading.Lock)
    in_progress:   bool            = False
    deleted_count: int             = 0
    error:         Optional[str]   = None

@dataclass
class BulkSendState:
    lock:           threading.Lock = field(default_factory=threading.Lock)
    in_progress:    bool  = False
    stop_requested: bool  = False
    total:          int   = 0
    sent:           int   = 0
    errors:         int   = 0
    logs:           list  = field(default_factory=list)

delete_state = BulkDeleteState()
send_state   = BulkSendState()

# ---------------------------------------------------------------------------
# CACHE STATUT ROUTEUR (thread-safe — plusieurs requêtes /router/status
# peuvent arriver concurrent depuis l'UI et les widgets de monitoring)
# ---------------------------------------------------------------------------
_router_status_cache: dict = {'data': None, 'ts': 0.0}
_router_status_lock = threading.Lock()
ROUTER_STATUS_TTL = 5  # secondes

# ---------------------------------------------------------------------------
# FLASK + CSRF + RATE LIMITING
# ---------------------------------------------------------------------------
app = Flask(__name__)
limiter = Limiter(get_remote_address, app=app, default_limits=[], storage_uri='memory://')
CSRF_TOKEN = secrets.token_hex(32)

@app.after_request
def set_security_headers(response):
    response.headers['Content-Security-Policy'] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:;"
    )
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    response.headers['Referrer-Policy'] = 'strict-origin'
    return response

def csrf_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if request.method == 'POST':
            if request.headers.get('X-CSRF-Token') != CSRF_TOKEN:
                return jsonify({'status': 'error', 'message': 'Token CSRF invalide'}), 403
        return f(*args, **kwargs)
    return decorated

# ---------------------------------------------------------------------------
# ROUTES — pages
# ---------------------------------------------------------------------------
@app.route('/')
def home():
    return render_template('index.html', csrf_token=CSRF_TOKEN)

# ---------------------------------------------------------------------------
# ROUTES — config
# ---------------------------------------------------------------------------
@app.route('/config', methods=['GET'])
def get_config():
    """Return current config (password masked) + adapter capabilities."""
    cfg = dict(_config)
    cfg['pass'] = '********' if cfg.get('pass') else ''
    meta = ADAPTER_META.get(_config.get('brand', 'huawei'), {})
    return jsonify({
        'status': 'ok',
        'config': cfg,
        'capabilities': {
            'supports_inbox':  meta.get('supports_inbox',  True),
            'supports_outbox': meta.get('supports_outbox', True),
            'needs_user':      meta.get('needs_user',      True),
        },
        'adapters': ADAPTER_META,
    }), 200

@app.route('/config', methods=['POST'])
@limiter.limit('10 per minute')
@csrf_required
def set_config():
    """Save new config and reload the adapter."""
    data = request.get_json()
    if not data:
        return jsonify({'status': 'error', 'message': 'Données manquantes'}), 400

    brand = data.get('brand', '').lower()
    ip    = (data.get('ip') or '').strip()
    user  = (data.get('user') or '').strip()
    password = data.get('pass', '')

    if not brand or not ip:
        return jsonify({'status': 'error', 'message': 'Marque et IP sont obligatoires'}), 400

    if brand not in ADAPTERS:
        return jsonify({'status': 'error', 'message': f'Marque inconnue : "{brand}". Valeurs acceptées : {", ".join(ADAPTERS)}.'}), 400

    if not validate_router_ip(ip):
        return jsonify({'status': 'error', 'message': f'IP invalide : "{ip}". Seules les adresses IPv4 privées sont acceptées (ex: 192.168.x.x).'}), 400

    # If password field is masked (unchanged), keep existing password
    if password == '********':
        password = _config.get('pass', '')

    new_cfg = {'brand': brand, 'ip': ip, 'user': user, 'pass': password}
    try:
        reload_adapter(new_cfg)
    except ValueError as e:
        return jsonify({'status': 'error', 'message': str(e)}), 400

    save_config(new_cfg)
    # Invalidate router status cache
    with _router_status_lock:
        _router_status_cache['data'] = None
        _router_status_cache['ts'] = 0.0
    logger.info("CONFIG mis à jour — marque=%s ip=%s", brand, ip)
    return jsonify({'status': 'ok', 'message': 'Configuration sauvegardée.'}), 200

@app.route('/config/test', methods=['POST'])
@limiter.limit('10 per minute')
@csrf_required
def test_config():
    """Test connectivity with the provided (or current) config."""
    data = request.get_json() or {}
    brand    = (data.get('brand') or _config.get('brand', 'huawei')).lower()
    ip       = (data.get('ip') or _config.get('ip', '')).strip()
    user     = (data.get('user') or _config.get('user', '')).strip()
    password = data.get('pass', '')
    if password == '********' or not password:
        password = _config.get('pass', '')

    if brand not in ADAPTERS:
        return jsonify({'status': 'error', 'message': f'Marque inconnue : "{brand}". Valeurs acceptées : {", ".join(ADAPTERS)}.'}), 400

    if not validate_router_ip(ip):
        return jsonify({'status': 'error', 'message': f'IP invalide : "{ip}". Seules les adresses IPv4 privées sont acceptées.'}), 400

    adapter = None
    try:
        adapter = get_adapter({'brand': brand, 'ip': ip, 'user': user, 'pass': password})
        result  = adapter.check_health()
        return jsonify({'status': 'ok', 'message': f'Connexion réussie ({brand}).', **result}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'message': f'Échec : {sanitize_exception(e)}'}), 503
    finally:
        if adapter is not None:
            adapter.close()

# ---------------------------------------------------------------------------
# ROUTES — capabilities (shortcut for the frontend)
# ---------------------------------------------------------------------------
@app.route('/capabilities', methods=['GET'])
def capabilities():
    adapter = current_adapter()
    return jsonify({
        'status': 'ok',
        'brand':           _config.get('brand', 'huawei'),
        'supports_inbox':  adapter.supports_inbox,
        'supports_outbox': adapter.supports_outbox,
    }), 200

# ---------------------------------------------------------------------------
# ROUTES — SMS
# ---------------------------------------------------------------------------
@app.route('/send', methods=['GET', 'POST'])
@limiter.limit('200 per 10 minutes')
def send_sms_api():
    if request.is_json:
        data    = request.get_json()
        number  = data.get('number')
        message = data.get('message')
    else:
        # request.values covers query string (GET) and form-encoded POST data
        number  = request.values.get('number')
        message = request.values.get('message')

    if not number or not message:
        return jsonify({'status': 'error', 'message': 'Données manquantes'}), 400
    number = normalize_number(number)
    if not validate_number(number):
        return jsonify({'status': 'error', 'message': f'Numéro invalide : {number}'}), 400
    if len(message) > MAX_MESSAGE_LENGTH:
        return jsonify({'status': 'error', 'message': f'Message trop long (max {MAX_MESSAGE_LENGTH} car.)'}), 400

    try:
        logger.info('SEND > %s', number)
        current_adapter().send_sms([number], message)
        return jsonify({'status': 'success', 'to': number}), 200
    except Exception as e:
        logger.error('ERREUR SEND', exc_info=True)
        return jsonify({'status': 'error', 'message': f'Erreur envoi : {sanitize_exception(e)}'}), 500

@app.route('/inbox', methods=['GET'])
def get_inbox():
    page     = max(1, request.args.get('page', 1, type=int))
    per_page = 20
    try:
        result = current_adapter().get_inbox(page=page, per_page=per_page)
        return jsonify({'status': 'success', **result}), 200
    except NotSupportedError as e:
        return jsonify({'status': 'error', 'message': str(e), 'not_supported': True}), 501
    except Exception as e:
        logger.error('ERREUR INBOX', exc_info=True)
        return jsonify({'status': 'error', 'message': f'Impossible de charger la réception : {sanitize_exception(e)}'}), 500

@app.route('/outbox', methods=['GET'])
def get_outbox():
    page     = max(1, request.args.get('page', 1, type=int))
    per_page = 50
    try:
        result = current_adapter().get_outbox(page=page, per_page=per_page)
        return jsonify({'status': 'success', **result}), 200
    except NotSupportedError as e:
        return jsonify({'status': 'error', 'message': str(e), 'not_supported': True}), 501
    except Exception as e:
        logger.error('ERREUR OUTBOX', exc_info=True)
        return jsonify({'status': 'error', 'message': f'Impossible de charger la boîte d\'envoi : {sanitize_exception(e)}'}), 500

@app.route('/delete', methods=['POST'])
@csrf_required
def delete_sms():
    data = request.get_json()
    try:
        idx = int(data.get('index'))
    except (TypeError, ValueError):
        return jsonify({'status': 'error', 'message': 'Index invalide'}), 400
    try:
        current_adapter().delete_sms(idx)
        return jsonify({'status': 'success', 'deleted': idx}), 200
    except Exception as e:
        logger.error('ERREUR DELETE', exc_info=True)
        return jsonify({'status': 'error', 'message': f'Impossible de supprimer : {sanitize_exception(e)}'}), 500

# ---------------------------------------------------------------------------
# BACKGROUND THREAD HELPER — never calls flask.abort() (no app context)
# ---------------------------------------------------------------------------
def _get_adapter_for_thread():
    """Return the current adapter or None, safe to call from background threads."""
    with _adapter_lock:
        return _adapter

# ---------------------------------------------------------------------------
# ROUTES — delete all outbox (background)
# ---------------------------------------------------------------------------
def perform_bulk_delete():
    adapter = _get_adapter_for_thread()
    if adapter is None:
        with delete_state.lock:
            delete_state.error = "Routeur non configuré."
            delete_state.in_progress = False
        return
    try:
        def _progress(count):
            with delete_state.lock:
                delete_state.deleted_count = count

        adapter.delete_outbox_all(on_progress=_progress)
    except Exception as e:
        logger.error('ERREUR SUPPRESSION ARRIÈRE-PLAN', exc_info=True)
        with delete_state.lock:
            delete_state.error = str(e)
    finally:
        with delete_state.lock:
            delete_state.in_progress = False

@app.route('/delete_all_sent', methods=['POST'])
@csrf_required
def delete_all_sent_api():
    if not current_adapter().supports_outbox:
        return jsonify({'status': 'error', 'message': 'Ce routeur ne supporte pas la boîte d\'envoi.', 'not_supported': True}), 501

    with delete_state.lock:
        if delete_state.in_progress:
            return jsonify({'status': 'error', 'message': 'Une suppression globale est déjà en cours.'}), 400
        delete_state.in_progress   = True
        delete_state.deleted_count = 0
        delete_state.error         = None

    threading.Thread(target=perform_bulk_delete, daemon=True).start()
    return jsonify({'status': 'success', 'message': 'Suppression globale démarrée.'}), 202

@app.route('/delete_all_sent/status', methods=['GET'])
def delete_all_sent_status():
    with delete_state.lock:
        return jsonify({
            'status':        'success',
            'in_progress':   delete_state.in_progress,
            'deleted_count': delete_state.deleted_count,
            'error':         sanitize_exception(delete_state.error) if delete_state.error else None,
        }), 200

# ---------------------------------------------------------------------------
# ROUTES — bulk send
# ---------------------------------------------------------------------------
def perform_bulk_send(tasks, delay=1.0):
    adapter = _get_adapter_for_thread()
    if adapter is None:
        with send_state.lock:
            send_state.logs.append({'number': '—', 'status': 'error', 'detail': 'Routeur non configuré.'})
            send_state.in_progress = False
        return
    try:
        for task in tasks:
            with send_state.lock:
                if send_state.stop_requested:
                    send_state.logs.append({'number': '—', 'status': 'error', 'detail': 'Envoi annulé.'})
                    break
            number  = normalize_number(task.get('number', ''))
            message = task.get('message', '')
            if not validate_number(number):
                with send_state.lock:
                    send_state.errors += 1
                    send_state.logs.append({'number': number, 'status': 'error', 'detail': 'Numéro invalide'})
                continue
            try:
                adapter.send_sms([number], message)
                with send_state.lock:
                    send_state.sent += 1
                    send_state.logs.append({'number': number, 'status': 'ok'})
            except Exception as e:
                with send_state.lock:
                    send_state.errors += 1
                    send_state.logs.append({'number': number, 'status': 'error', 'detail': sanitize_exception(e)})
            time.sleep(delay)
    except Exception as e:
        logger.error('ERREUR BULK SEND', exc_info=True)
        with send_state.lock:
            send_state.errors += 1
            send_state.logs.append({'number': '?', 'status': 'error', 'detail': f'Connexion perdue : {sanitize_exception(e)}'})
    finally:
        with send_state.lock:
            send_state.in_progress = False

@app.route('/send_bulk', methods=['POST'])
@limiter.limit('10 per 10 minutes')
@csrf_required
def send_bulk_api():
    data  = request.get_json()
    tasks = data.get('tasks', []) if data else []
    try:
        delay = float(data.get('delay', 1.0))
        delay = max(0.5, min(5.0, delay))
    except (TypeError, ValueError):
        delay = 1.0

    if not tasks:
        return jsonify({'status': 'error', 'message': 'Aucune tâche fournie.'}), 400
    if len(tasks) > MAX_BULK_TASKS:
        return jsonify({'status': 'error', 'message': f'Trop de destinataires (max {MAX_BULK_TASKS}).'}), 400

    invalid = [t.get('number') for t in tasks if not validate_number(t.get('number', ''))]
    if invalid:
        return jsonify({'status': 'error', 'message': f'Numéro(s) invalide(s) : {", ".join(invalid)}'}), 400

    too_long = [t.get('number') for t in tasks if len(t.get('message', '')) > MAX_MESSAGE_LENGTH]
    if too_long:
        return jsonify({'status': 'error', 'message': f'Message trop long (max {MAX_MESSAGE_LENGTH} car.) pour : {", ".join(too_long)}'}), 400

    with send_state.lock:
        if send_state.in_progress:
            return jsonify({'status': 'error', 'message': 'Un envoi groupé est déjà en cours.'}), 400
        send_state.in_progress    = True
        send_state.stop_requested = False
        send_state.total          = len(tasks)
        send_state.sent           = 0
        send_state.errors         = 0
        send_state.logs           = []

    threading.Thread(target=perform_bulk_send, args=(tasks, delay), daemon=True).start()
    return jsonify({'status': 'success', 'message': 'Envoi groupé démarré.', 'total': len(tasks)}), 202

@app.route('/send_bulk/stream')
@limiter.limit('20 per minute')
def send_bulk_stream():
    import json as _json

    def generate():
        deadline = time.time() + 5
        while not send_state.in_progress and time.time() < deadline:
            time.sleep(0.05)

        if not send_state.in_progress:
            # Fast-completion race: operation may have finished before stream connected
            with send_state.lock:
                logs   = list(send_state.logs)
                sent   = send_state.sent
                errors = send_state.errors
                total  = send_state.total
            for log in logs:
                yield f"data: {_json.dumps(log)}\n\n"
            yield f"data: {_json.dumps({'done': True, 'sent': sent, 'errors': errors, 'total': total})}\n\n"
            return

        last = 0
        while True:
            with send_state.lock:
                logs    = list(send_state.logs)
                in_prog = send_state.in_progress
                sent    = send_state.sent
                errors  = send_state.errors
                total   = send_state.total

            for log in logs[last:]:
                yield f"data: {_json.dumps(log)}\n\n"
            last = len(logs)

            if not in_prog:
                yield f"data: {_json.dumps({'done': True, 'sent': sent, 'errors': errors, 'total': total})}\n\n"
                return

            time.sleep(0.3)

    return Response(
        generate(),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )

@app.route('/send_bulk/stop', methods=['POST'])
@csrf_required
def send_bulk_stop():
    with send_state.lock:
        if not send_state.in_progress:
            return jsonify({'status': 'error', 'message': 'Aucun envoi en cours.'}), 400
        send_state.stop_requested = True
    return jsonify({'status': 'success', 'message': 'Arrêt demandé.'}), 200

@app.route('/send_bulk/status', methods=['GET'])
def send_bulk_status():
    with send_state.lock:
        return jsonify({
            'status':      'success',
            'in_progress': send_state.in_progress,
            'total':       send_state.total,
            'sent':        send_state.sent,
            'errors':      send_state.errors,
            'logs':        list(send_state.logs),
        }), 200

# ---------------------------------------------------------------------------
# ROUTES — router status & health
# ---------------------------------------------------------------------------
@app.route('/health', methods=['GET'])
def health():
    try:
        result = current_adapter().check_health()
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'status': 'error', 'router': 'unreachable', 'detail': sanitize_exception(e)}), 503

@app.route('/router/status', methods=['GET'])
def router_status():
    now = time.time()
    # Fast path under the lock
    with _router_status_lock:
        cached = _router_status_cache['data']
        cached_ts = _router_status_cache['ts']
    if cached and now - cached_ts < ROUTER_STATUS_TTL:
        return jsonify(cached), 200
    # Slow path: hit the router OUTSIDE the lock (don't serialize all callers)
    try:
        result = current_adapter().get_status()
        with _router_status_lock:
            _router_status_cache['data'] = result
            _router_status_cache['ts']   = now
        return jsonify(result), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': sanitize_exception(e)}), 503


if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
