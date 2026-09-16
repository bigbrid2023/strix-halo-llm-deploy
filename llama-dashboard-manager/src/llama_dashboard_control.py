#!/usr/bin/python3
"""Restricted local service policy helper. No caller-supplied paths or commands."""
import fcntl
import glob
import json
import os
import re
import subprocess
import sys
import time
import socket
import socketserver
import struct
import urllib.request
from pathlib import Path

CONFIG = Path('/etc/llama-dashboard/server.json')
DROPIN = Path('/root/.config/systemd/user/llama-server.service.d/90-dashboard-policy.conf')
UNIT = 'llama-server.service'
ENV = dict(os.environ, XDG_RUNTIME_DIR='/run/user/0')

def run(args, env=None, check=True):
    p = subprocess.run(args, env=env, capture_output=True, text=True, timeout=15)
    if check and p.returncode:
        raise RuntimeError(p.stderr.strip() or p.stdout.strip())
    return p.stdout.strip()

def ctl(*args, check=True):
    return run(['systemctl', '--user', *args], env=ENV, check=check)

def info(*args):
    return dict(line.split('=', 1) for line in ctl('show', UNIT, *args).splitlines() if '=' in line)

def processes():
    found = []
    for p in Path('/proc').iterdir():
        if not p.name.isdigit(): continue
        try:
            if (p/'comm').read_text().strip() != 'llama-server': continue
            argv = (p/'cmdline').read_bytes().decode().strip('\0').split('\0')
            model = argv[argv.index('-m')+1] if '-m' in argv else 'unknown'
            cgroup=(p/'cgroup').read_text()
            found.append({'pid': int(p.name), 'model': Path(model).name,
                          'managed': '/llama-server.service' in cgroup and 'user-0.slice/user@0.service/' in cgroup})
        except (OSError, ValueError): pass
    return found

def audit_units():
    roots = [(p,None) for p in ('/etc/systemd/system','/usr/lib/systemd/system','/usr/local/lib/systemd/system')]
    roots += [('/root/.config/systemd/user', 0)]
    for directory in glob.glob('/home/*/.config/systemd/user') + glob.glob('/home/*/.local/share/systemd/user'):
        owner = Path(directory).parts[2]
        try:
            import pwd
            uid = pwd.getpwnam(owner).pw_uid
            roots.append((directory, uid))
        except KeyError: pass
    found = []
    seen = set()
    for directory, uid in roots:
        for path in Path(directory).glob('*.service'):
            if (uid, path.name) in seen or (uid == 0 and path.name == UNIT): continue
            seen.add((uid, path.name))
            try:
                body = path.read_text()
                # Include launch scripts referenced by ExecStart.
                for executable in re.findall(r'^ExecStart=-?([^\s]+)', body, re.M):
                    p = Path(executable)
                    if p.is_file() and p.stat().st_size < 65536:
                        try: body += '\n' + p.read_text()
                        except (UnicodeError, OSError): pass
                if 'llama-server' not in body and not re.search(r'(?i)qwen.*gguf', body): continue
                args = ['systemctl']
                env = None
                if uid is not None:
                    import pwd
                    args = ['runuser', '-u', pwd.getpwuid(uid).pw_name, '--', 'env',
                            f'XDG_RUNTIME_DIR=/run/user/{uid}', 'systemctl', '--user']
                state = run(args+['is-enabled', path.name], env=env, check=False)
                active = run(args+['is-active', path.name], env=env, check=False)
                if any(link.is_symlink() for link in Path(directory).glob('*.wants/'+path.name)):
                    state = 'enabled' if not state.startswith('enabled') else state
                found.append({'name':path.name, 'path':str(path), 'enabled':state,
                              'active':active, 'conflict':state.startswith('enabled') or active in ('active','activating','reloading')})
            except OSError: pass
    for base in ('/var/spool/cron', '/etc/cron.d'):
        if not Path(base).is_dir(): continue
        for path in Path(base).iterdir():
            if not path.is_file(): continue
            try:
                if any('llama-server' in line and not line.lstrip().startswith('#') for line in path.read_text().splitlines()):
                    found.append({'name':'cron: '+path.name,'path':str(path),'enabled':'configured','active':'scheduled','conflict':True})
            except (UnicodeError, OSError): pass
    return found

def status():
    config = json.loads(CONFIG.read_text())
    unit = info('-p','ActiveState','-p','SubState','-p','MainPID','-p','UnitFileState','-p','Restart')
    units = audit_units()
    live = processes()
    conflicts = [u for u in units if u['conflict']]
    conflicts += [{'name':'unmanaged llama-server', 'pid':p['pid'], 'model':p['model'], 'conflict':True}
                  for p in live if not p['managed']]
    return {'unit':unit,'model':Path(config['argv'][config['argv'].index('-m')+1]).name,
            'sleep_idle_seconds':config['sleep_idle_seconds'],
            'autostart':unit.get('UnitFileState','').startswith('enabled'),
            'restart_on_failure':unit.get('Restart') not in ('no',None),
            'processes':live,'other_units':units,'conflicts':conflicts,'checked_at':time.time()}

def quote(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'

def write_policy(config, test_seconds=None):
    argv = list(config['argv'])
    if '--sleep-idle-seconds' in argv:
        i = argv.index('--sleep-idle-seconds'); del argv[i:i+2]
    seconds = config['sleep_idle_seconds'] if test_seconds is None else test_seconds
    argv += ['--sleep-idle-seconds', str(seconds)]
    model = argv[argv.index('-m')+1]
    modelpath = str(Path(config['cwd'])/model) if not Path(model).is_absolute() else model
    contents = '[Unit]\nDescription=Qwen3.8 Flash Next current model managed by Dashboard\nStartLimitIntervalSec=0\n\n[Service]\n'
    contents += 'WorkingDirectory='+config['cwd'].replace('%','%%')+'\nExecStartPre=\nExecStartPre=/usr/bin/test -r '+quote(modelpath)+'\n'
    contents += 'ExecStart=\nExecStart='+' '.join(quote(a) for a in argv)+'\n'
    contents += 'Restart='+('on-failure' if config['restart_on_failure'] else 'no')+'\nRestartSec=5\nTimeoutStopSec=120\n'
    for key, value in config.get('environment',{}).items():
        contents += 'Environment='+quote(key+'='+value)+'\n'
    DROPIN.parent.mkdir(parents=True,exist_ok=True)
    temp=DROPIN.with_suffix('.tmp');temp.write_text(contents);temp.replace(DROPIN)

def set_policy(request):
    import llama_dashboard_models as models
    models.ensure_no_job()
    if set(request) - {'action','autostart','restart_on_failure','sleep_idle_seconds','confirm_restart'}:
        raise ValueError('Unknown policy fields')
    for key in ('autostart','restart_on_failure'):
        if type(request.get(key)) is not bool: raise ValueError('Policy flags must be boolean')
    if type(request.get('sleep_idle_seconds')) is not int or (request['sleep_idle_seconds'] != -1 and not 30 <= request['sleep_idle_seconds'] <= 86400):
        raise ValueError('Sleep must be disabled or between 30 and 86400 seconds')
    current=status()
    if current['conflicts']:
        raise ValueError('Other llama-server processes or keepalive tasks exist. Resolve conflicts before changing policy.')
    config=json.loads(CONFIG.read_text())
    changed_sleep=config['sleep_idle_seconds'] != request['sleep_idle_seconds']
    if changed_sleep:
        if request.get('confirm_restart') is not True:raise ValueError('Changing idle sleep requires a server restart')
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8080/metrics',timeout=5) as response:
            metrics=response.read().decode()
        for key in ('requests_processing','requests_deferred'):
            match=re.search(r'^llamacpp:'+key+r'\s+(\S+)',metrics,re.M)
            if not match or float(match[1])>0:raise ValueError('Server is busy; wait for all requests to finish')
    stamp=time.strftime('%Y%m%d-%H%M%S')
    import uuid
    archive=Path('/home/__DASH_USER__/backups')/('dashboard-policy-'+stamp+'-'+uuid.uuid4().hex[:6])
    archive.mkdir(exist_ok=False)
    import shutil
    shutil.copy2(CONFIG,archive/'server.json')
    if DROPIN.exists():shutil.copy2(DROPIN,archive/DROPIN.name)
    for key in ('autostart','restart_on_failure','sleep_idle_seconds'):config[key]=request[key]
    write_policy(config)
    ctl('daemon-reload')
    ctl('enable' if config['autostart'] else 'disable',UNIT)
    CONFIG.write_text(json.dumps(config,ensure_ascii=False,indent=2));CONFIG.chmod(0o600)
    if changed_sleep:ctl('--no-block','restart',UNIT)
    return {'ok':True,'restart_requested':changed_sleep,'backup':str(archive),'status':status()}

def dispatch(request):
    if os.geteuid()!=0:raise PermissionError('Controller must run as root')
    import llama_dashboard_models as models
    if request.get('action') in ('editor','inventory','model_job'):
        return models.handle(request)
    with open('/run/llama-dashboard-control.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        if request.get('action')=='status':result=status()
        elif request.get('action')=='set_policy':result=set_policy(request)
        elif request.get('action') in ('preview_model','validate_model','apply_model'):
            result=models.handle(request)
        else:raise ValueError('Unsupported action')
        return result

def main():
    if sys.argv[1:] == ['--daemon']:
        import pwd
        account=pwd.getpwnam('__DASH_USER__')
        directory=Path('/run/llama-dashboard-control');directory.mkdir(mode=0o755,exist_ok=True);directory.chmod(0o755)
        address=directory/'control.sock'
        if address.exists():address.unlink()
        class Handler(socketserver.StreamRequestHandler):
            def handle(self):
                self.request.settimeout(30)
                try:
                    _,uid,_=struct.unpack('3i',self.request.getsockopt(socket.SOL_SOCKET,socket.SO_PEERCRED,12))
                    if uid not in (0,account.pw_uid):raise PermissionError('Unauthorized local user')
                    request=json.loads(self.rfile.readline(65537))
                    result=dispatch(request)
                except Exception as error:result={'error':str(error)}
                self.wfile.write(json.dumps(result,ensure_ascii=False).encode()+b'\n')
        class Server(socketserver.ThreadingUnixStreamServer):
            daemon_threads=True
        with Server(str(address),Handler) as server:
            os.chown(address,0,account.pw_gid);address.chmod(0o660)
            server.serve_forever()
    else:
        print(json.dumps(dispatch(json.loads(sys.stdin.buffer.read(65537))),ensure_ascii=False))

if __name__=='__main__':
    try:main()
    except Exception as error:
        print(json.dumps({'error':str(error)},ensure_ascii=False));sys.exit(1)
