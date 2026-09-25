#!/usr/bin/env python3
"""Read-only llama.cpp dashboard. Python standard library; no inference requests."""
import copy
import glob
import json
import math
import os
import re
import secrets
import socket
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

BASE = os.environ.get('LLAMA_BASE', 'http://127.0.0.1:8080').rstrip('/')
PORT = int(os.environ.get('DASH_PORT', '8090'))
METRIC_INTERVAL = 20
HARDWARE_INTERVAL = 30
CACHE = Path(os.environ.get('DASH_CACHE', str(Path.home() / '.cache/llama-dashboard/session.json')))
ROOT = Path(__file__).resolve().parent
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
ADMIN_TOKEN = Path(os.environ.get('LLAMA_DASH_ADMIN_TOKEN', '/etc/llama-dashboard/admin-token')).read_text().strip() if Path(os.environ.get('LLAMA_DASH_ADMIN_TOKEN', '/etc/llama-dashboard/admin-token')).exists() else ''
# NOTE: 部署时请把 admin-token 放到 /etc/llama-dashboard/admin-token，或设置环境变量
# LLAMA_DASH_ADMIN_TOKEN 指向自定义路径。避免在源码里硬编码用户名。



CALLER_STATE = Path('/run/llama-dashboard-callers/state.json')
DEVICE_NAMES = Path.home() / '.config/llama-dashboard/device-names.json'

def caller_snapshot():
    try:
        data = json.loads(CALLER_STATE.read_text())
        data['stale'] = time.time() - data.get('at', 0) > 15
    except (OSError, ValueError):
        return {'stale': True, 'rows': [], 'connections': [], 'error': '调用来源监测暂不可用'}
    try:
        names = json.loads(DEVICE_NAMES.read_text())
    except (OSError, ValueError):
        names = {}
    for row in data.get('rows', []):
        row['computer'] = names.get(row['ip']) or row.get('hostname') or '未识别名称'
    return data


def control_request(request):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(45)
        client.connect('/run/llama-dashboard-control/control.sock')
        client.sendall(json.dumps(request).encode() + b'\n')
        with client.makefile('rb') as reader:
            return json.loads(reader.readline(4 * 1024 * 1024))


def read(path):
    return Path(path).read_text().strip()


def number(path, divisor=1):
    try:
        return float(read(path)) / divisor
    except (OSError, ValueError):
        return None


def fetch(path, as_json=True):
    with OPENER.open(BASE + path, timeout=5) as response:
        body = response.read().decode()
        return (json.loads(body) if as_json else body), response.headers


def parse_metrics(body):
    values, positions = {}, {}
    for line in body.splitlines():
        match = re.fullmatch(r'llamacpp:([\w]+)(\{[^}]*\})?\s+([^\s]+)(?:\s+\S+)?', line.strip())
        if not match:
            continue
        try:
            value = float(match[3])
        except ValueError:
            continue
        if not math.isfinite(value):
            continue
        if not match[2]:
            values[match[1]] = value
        elif match[1] == 'spec_decode_num_accepted_tokens_per_pos_total':
            pos = re.search(r'position="(\d+)"', match[2])
            if pos:
                positions[pos[1]] = value
    if 'tokens_predicted_total' not in values:
        raise ValueError('指标接口未返回生成 Token 计数')
    return values, positions


def ratio(a, b, scale=1):
    return a / b * scale if a is not None and b is not None and b > 0 else None


def process_info(cached_pid=None):
    """Match the server port, and use boot ID + start ticks to survive PID reuse."""
    port = urllib.parse.urlsplit(BASE).port or 80
    candidates = [str(cached_pid)] if cached_pid else []
    candidates += [p.name for p in Path('/proc').iterdir() if p.name.isdigit() and p.name not in candidates]
    for pid in candidates:
        try:
            args = Path('/proc', pid, 'cmdline').read_bytes().decode().strip('\0').split('\0')
            if not args or Path(args[0]).name != 'llama-server':
                continue
            proc_port = 8080
            for i, arg in enumerate(args):
                if arg == '--port' and i + 1 < len(args):
                    proc_port = int(args[i + 1])
                elif arg.startswith('--port='):
                    proc_port = int(arg.split('=', 1)[1])
            if proc_port != port:
                continue
            stat = read('/proc/' + pid + '/stat').rsplit(')', 1)[1].split()
            ticks = int(stat[19])
            boot = read('/proc/sys/kernel/random/boot_id')
            uptime = float(read('/proc/uptime').split()[0])
            started = time.time() - uptime + ticks / os.sysconf('SC_CLK_TCK')
            keys = ('--spec-type', '--spec-draft-n-max', '-ctk', '-ctv', '-b', '-ub', '-t', '--parallel', '--sleep-idle-seconds')
            settings = {k: args[args.index(k) + 1] for k in keys if k in args and args.index(k) + 1 < len(args)}
            return {'id': f'{boot}:{pid}:{ticks}', 'pid': int(pid), 'started_at': started, 'settings': settings}
        except (OSError, ValueError, IndexError):
            continue
    return None


def hardware(previous_cpu=None):
    mem = {}
    for line in read('/proc/meminfo').splitlines():
        key, value = line.split(':', 1)
        mem[key] = int(value.split()[0]) * 1024
    cpu = [int(v) for v in read('/proc/stat').splitlines()[0].split()[1:9]]
    total, idle = sum(cpu), cpu[3] + cpu[4]
    cpu_pct = None
    if previous_cpu:
        dt, di = total - previous_cpu[0], idle - previous_cpu[1]
        if dt > 0:
            cpu_pct = max(0, min(100, 100 * (dt - di) / dt))
    gpus = []
    for device in sorted(glob.glob('/sys/class/drm/card[0-9]*/device')):
        try:
            if read(device + '/vendor') != '0x1002':
                continue
        except OSError:
            continue
        card = {'name': Path(device).parent.name, 'util': number(device + '/gpu_busy_percent')}
        for kind in ('vram', 'gtt'):
            for field in ('used', 'total'):
                card[kind + '_' + field] = number(device + '/mem_info_' + kind + '_' + field)
        sensors = sorted(glob.glob(device + '/hwmon/hwmon*'))
        card['temperature'] = number(sensors[0] + '/temp1_input', 1000) if sensors else None
        card['power'] = number(sensors[0] + '/power1_average', 1e6) if sensors else None
        gpus.append(card)
    return {'at': time.time(), 'cpu': cpu_pct, 'ram_total': mem['MemTotal'],
            'ram_used': mem['MemTotal'] - mem.get('MemAvailable', 0),
            'ram_available': mem.get('MemAvailable'), 'swap_total': mem.get('SwapTotal', 0),
            'swap_used': mem.get('SwapTotal', 0) - mem.get('SwapFree', 0), 'gpus': gpus}, (total, idle)


class Monitor:
    def __init__(self, cache=CACHE):
        self.lock = threading.RLock()
        self.cache = cache
        self.state = {'version': 2, 'server_ok': False, 'metric_at': None, 'metric_error': None,
                      'slot_at': None, 'slot_error': None, 'hardware_error': None,
                      'is_sleeping': None, 'slot_poll_mode': 'unknown', 'slots_stale': True,
                      'metrics': {}, 'positions': {}, 'slots': [], 'model': {}, 'process': None,
                      'epoch': None, 'header_id': None, 'history': [], 'hardware_history': [],
                      'hardware': {}, 'reset_at': None, 'reset_reason': None, 'cache_error': None}
        try:
            data = json.loads(cache.read_text())
            if data.get('version') == 2:
                for key in ('epoch', 'header_id', 'history', 'hardware_history', 'reset_at', 'reset_reason', 'process', 'slots', 'slot_at'):
                    if key in data:
                        self.state[key] = data[key]
        except (OSError, ValueError, TypeError):
            pass

    def accept(self, metrics, positions, proc, header, now):
        """Only successful metric reads can advance/reset the statistical session."""
        with self.lock:
            s = self.state
            epoch = proc['id'] if proc else s['epoch'] or ('metrics:' + header if header else 'unidentified')
            old = s['metrics'] or (s['history'][-1]['counters'] if s['history'] else {})
            counters = {k: v for k, v in metrics.items() if k.endswith('_total')}
            dropped = any(k in old and v < old[k] for k, v in counters.items())
            proc_changed = bool(proc and s['epoch'] and not s['epoch'].startswith('metrics:') and epoch != s['epoch'])
            header_changed = bool(header and s['header_id'] and header != s['header_id'])
            if proc_changed or header_changed or dropped:
                s['history'] = []
                s['hardware_history'] = []
                s['slots'] = []
                s['slot_at'] = None
                s['model'] = {}
                s['reset_at'] = now
                s['reset_reason'] = '服务启动标识变化' if proc_changed or header_changed else '服务累计计数清零'
            s.update(epoch=epoch, header_id=header or s['header_id'], metrics=metrics,
                     positions=positions, server_ok=True, metric_error=None, metric_at=now)
            if proc:
                s['process'] = proc
            s['history'].append({'at': now, 'counters': counters})
            s['history'] = [p for p in s['history'] if p['at'] >= now - 3600][-190:]
            s['hardware_history'] = [p for p in s['hardware_history'] if p['at'] >= now - 3600][-125:]

    def failure(self, exc):
        with self.lock:
            self.state['server_ok'] = False
            self.state['metric_error'] = str(exc)

    def save(self):
        try:
            with self.lock:
                data = {k: copy.deepcopy(self.state[k]) for k in
                        ('version', 'epoch', 'header_id', 'history', 'hardware_history', 'reset_at', 'reset_reason', 'process', 'slots', 'slot_at')}
            self.cache.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache.with_suffix('.tmp')
            tmp.write_text(json.dumps(data, allow_nan=False))
            tmp.replace(self.cache)
            with self.lock:
                self.state['cache_error'] = None
        except OSError as exc:
            with self.lock:
                self.state['cache_error'] = str(exc)

    def metrics_once(self):
        with self.lock:
            cached_pid = (self.state['process'] or {}).get('pid')
        proc = process_info(cached_pid)
        try:
            raw, headers = fetch('/metrics', False)
            metrics, positions = parse_metrics(raw)
            # A restart during the fetch is ambiguous: discard it and sample next cycle.
            after = process_info(proc['pid'] if proc else None)
            if proc and after and proc['id'] != after['id']:
                raise ValueError('采集期间服务重启，等待下一次采样')
            self.accept(metrics, positions, after or proc, headers.get('Process-Start-Time-Unix'), time.time())
        except Exception as exc:
            self.failure(exc)
            return
        props = {}
        try:
            props, _ = fetch('/props')
            sleeping = props.get('is_sleeping')
            if not isinstance(sleeping, bool):
                raise ValueError('无法确认模型休眠状态，暂停 Slot 查询')
            with self.lock:
                self.state['is_sleeping'] = sleeping
                self.state['slot_error'] = None
        except Exception as exc:
            sleeping = None
            with self.lock:
                self.state.update(is_sleeping=None, slot_error=str(exc))
        # An idle Slot GET can reset the sleep timer or wake a sleeping model.
        # Poll only when fresh metrics report active inference, never on idle.
        if sleeping is False and metrics.get('requests_processing', 0) > 0:
            try:
                slots, _ = fetch('/slots')
                allowed = ('id', 'id_task', 'n_ctx', 'is_processing', 'n_prompt_tokens',
                           'n_prompt_tokens_processed', 'n_prompt_tokens_cache', 'speculative')
                clean = []
                for slot in slots:
                    item = {k: slot.get(k) for k in allowed}
                    nt = slot.get('next_token') or {}
                    if isinstance(nt, list):
                        nt = nt[0] if nt else {}
                    item['generated'] = nt.get('n_decoded') if slot.get('is_processing') else None
                    clean.append(item)
                with self.lock:
                    self.state.update(slots=clean, slot_at=time.time(), slot_error=None, slots_stale=False)
            except Exception as exc:
                with self.lock:
                    self.state['slot_error'] = str(exc)
                    self.state['slots_stale'] = True
            with self.lock:
                self.state['slot_poll_mode'] = 'active'
        else:
            with self.lock:
                self.state.update(slots_stale=True,
                                  slot_poll_mode='sleeping' if sleeping else 'idle' if sleeping is False else 'unknown')
        with self.lock:
            model_at = self.state['model'].get('at', 0)
        if props and time.time() - model_at >= 300:
            try:
                models, _ = fetch('/v1/models')
                model = (models.get('data') or [{}])[0]
                meta = model.get('meta', {})
                info = {'name': (props.get('model_path') or model.get('id') or '').rsplit('/', 1)[-1],
                        'context': props.get('default_generation_settings', {}).get('n_ctx') or meta.get('n_ctx'),
                        'slots': props.get('total_slots'), 'quant': meta.get('ftype'),
                        'parameters': meta.get('n_params'), 'build': props.get('build_info'), 'at': time.time()}
                with self.lock:
                    self.state['model'] = info
            except Exception:
                pass
        self.save()

    def snapshot(self):
        with self.lock:
            s = copy.deepcopy(self.state)
        now = time.time()
        s['now'] = now
        s['callers'] = caller_snapshot()
        s['metric_interval'] = METRIC_INTERVAL
        s['hardware_interval'] = HARDWARE_INTERVAL
        s['metric_stale'] = not s['metric_at'] or now - s['metric_at'] > METRIC_INTERVAL * 2 + 5
        s['hardware_stale'] = not s['hardware'].get('at') or now - s['hardware']['at'] > HARDWARE_INTERVAL * 2 + 5
        m = s['metrics']
        get = m.get
        s['derived'] = {
            'generation_speed': ratio(get('tokens_predicted_total'), get('tokens_predicted_seconds_total')),
            'prompt_speed': ratio(get('prompt_tokens_total'), get('prompt_seconds_total')),
            'cache_rate': ratio(get('prompt_tokens_cached_total'),
                                get('prompt_tokens_cached_total', 0) + get('prompt_tokens_total', 0), 100),
            'mtp_rate': ratio(get('spec_decode_num_accepted_tokens_total'), get('spec_decode_num_draft_tokens_total'), 100),
            'accepted_per_step': ratio(get('spec_decode_num_accepted_tokens_total'), get('spec_decode_num_drafts_total')),
        }
        series = []
        for a, b in zip(s['history'], s['history'][1:]):
            dt = b['at'] - a['at']
            dc = b['counters'].get('tokens_predicted_total', 0) - a['counters'].get('tokens_predicted_total', 0)
            series.append({'at': b['at'], 'value': dc / dt if 0 < dt <= METRIC_INTERVAL * 2 + 5 and dc >= 0 else None})
        s['throughput'] = series
        windows = {}
        for seconds in (60, 300, 900):
            points = [p for p in s['history'] if p['at'] >= now - seconds]
            if len(points) >= 2 and not s['metric_stale'] and s['server_ok']:
                dt = points[-1]['at'] - points[0]['at']
                value = ratio(points[-1]['counters'].get('tokens_predicted_total', 0) - points[0]['counters'].get('tokens_predicted_total', 0), dt)
                windows[str(seconds)] = {'value': value, 'span': round(dt)}
            else:
                windows[str(seconds)] = {'value': None, 'span': 0}
        s['windows'] = windows
        s.pop('history')
        s.pop('epoch')
        s.pop('header_id')
        return s

    def run_metrics(self):
        periodic(self.metrics_once, METRIC_INTERVAL, self.failure)

    def run_hardware(self):
        cpu = None
        def sample():
            nonlocal cpu
            value, cpu = hardware(cpu)
            with self.lock:
                self.state['hardware'] = value
                self.state['hardware_error'] = None
                self.state['hardware_history'].append(value)
                self.state['hardware_history'] = [p for p in self.state['hardware_history'] if p['at'] >= time.time() - 3600][-125:]
        def fail(exc):
            with self.lock:
                self.state['hardware_error'] = str(exc)
        periodic(sample, HARDWARE_INTERVAL, fail)


def periodic(fn, interval, fail):
    while True:
        start = time.monotonic()
        try:
            fn()
        except Exception as exc:
            fail(exc)
        time.sleep(max(0.1, interval - (time.monotonic() - start)))


def main():
    monitor = Monitor()
    class Handler(BaseHTTPRequestHandler):
        def reply_json(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', 'application/json; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if self.path != '/api/control':
                return self.reply_json(404, {'error': 'Not found'})
            expected = 'http://' + self.headers.get('Host', '')
            if self.headers.get('Origin') != expected or self.headers.get('X-Dashboard-Request') != '1':
                return self.reply_json(403, {'error': '请从 Dashboard 页面提交管理操作'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if length <= 0 or length > 65536:raise ValueError('请求大小不正确')
                request = json.loads(self.rfile.read(length))
                if request.get('action') not in ('set_power','set_policy','preview_model','validate_model','apply_model','no_keepalive','unload_model'):raise ValueError('不支持此操作')
                token = self.headers.get('Authorization', '').removeprefix('Bearer ')
                if request['action'] != 'preview_model' and (not ADMIN_TOKEN or not secrets.compare_digest(token, ADMIN_TOKEN)):
                    return self.reply_json(403, {'error': '管理口令不正确'})
                result = control_request(request)
                self.reply_json(409 if result.get('error') else 200, result)
            except Exception as exc:
                self.reply_json(400, {'error': str(exc)})

        def do_GET(self):
            path = self.path.split('?', 1)[0]
            if path == '/api/power':
                try:
                    result = control_request({'action': 'power_status'})
                    return self.reply_json(200 if result.get('available') else 503, result)
                except Exception as exc:
                    return self.reply_json(503, {'error': str(exc), 'available': False})
            if path in ('/api/editor','/api/inventory','/api/model-job'):
                try:
                    action={'/api/editor':'editor','/api/inventory':'inventory','/api/model-job':'model_job'}[path]
                    result=control_request({'action':action})
                    return self.reply_json(200 if 'error' not in result else 503,result)
                except Exception as exc:
                    return self.reply_json(503,{'error':str(exc)})
            if path == '/api/control':
                try:
                    result = control_request({'action': 'status'})
                    return self.reply_json(200 if 'error' not in result else 503, result)
                except Exception as exc:
                    return self.reply_json(503, {'error': str(exc)})
            if path == '/api/state':
                body = json.dumps(monitor.snapshot(), ensure_ascii=False, allow_nan=False).encode()
                kind = 'application/json; charset=utf-8'
            elif path in ('/', '/index.html'):
                body, kind = (ROOT / 'llama_dashboard.html').read_bytes(), 'text/html; charset=utf-8'
            elif path == '/model-manager.js':
                body, kind = (ROOT / 'model-manager.js').read_bytes(), 'text/javascript; charset=utf-8'
            elif path == '/power-manager.js':
                body, kind = (ROOT / 'power-manager.js').read_bytes(), 'text/javascript; charset=utf-8'
            elif path == '/downloads/dashboard-manager-share.zip' and (ROOT / 'dashboard-manager-share.zip').is_file():
                body, kind = (ROOT / 'dashboard-manager-share.zip').read_bytes(), 'application/zip'
            elif path == '/healthz':
                body, kind = b'{"status":"ok","version":2}', 'application/json'
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    threading.Thread(target=monitor.run_metrics, daemon=True).start()
    threading.Thread(target=monitor.run_hardware, daemon=True).start()
    print(f'Dashboard v2 on :{PORT}; metrics={METRIC_INTERVAL}s hardware={HARDWARE_INTERVAL}s', flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
