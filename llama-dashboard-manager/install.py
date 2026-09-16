#!/usr/bin/env python3
"""Explicit, non-overwriting installation on Linux + systemd. No downloads."""
import argparse
import json
import os
import pwd
import re
import secrets
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

HERE=Path(__file__).resolve().parent

def command(args, check=True, env=None):
    return subprocess.run(args,check=check,env=env,text=True,capture_output=True,timeout=30)

def quoted(value):
    return '"'+str(value).replace('\\','\\\\').replace('"','\\"').replace('%','%%').replace('$','$$')+'"'

def render(text, account, disk):
    # Source templates do not contain the original owner's account, disk ID, or credentials.
    return text.replace('/home/__DASH_USER__',account.pw_dir).replace('__DASH_USER__',account.pw_name).replace('/MODEL_DISK',str(disk))

def main():
    parser=argparse.ArgumentParser(description='Install the shareable llama.cpp Dashboard; --dry-run makes no changes.')
    parser.add_argument('--user',required=True,help='Existing ordinary Linux account')
    parser.add_argument('--models-root',required=True,help='Existing mounted model disk/root directory')
    parser.add_argument('--server',required=True,help='Existing compatible llama-server binary')
    parser.add_argument('--model',required=True,help='Main GGUF first shard, inside models-root')
    parser.add_argument('--context',type=int,default=262144)
    parser.add_argument('--gpu-layers',default='999')
    parser.add_argument('--threads',type=int,default=4)
    parser.add_argument('--sleep-seconds',type=int,default=1800)
    parser.add_argument('--draft-model',help='Optional matching shared MTP draft GGUF')
    parser.add_argument('--mmproj',help='Optional matching image projector GGUF')
    parser.add_argument('--extra-args',default='',help='Additional inference arguments, quoted as one value')
    parser.add_argument('--dry-run',action='store_true')
    parser.add_argument('--no-start',action='store_true',help='Install without enabling or starting any service')
    args=parser.parse_args()
    if sys.platform!='linux' or sys.version_info<(3,10):raise ValueError('需要 Linux 和 Python 3.10+。')
    if not shutil.which('systemctl'):raise ValueError('需要 systemd。')
    account=pwd.getpwnam(args.user)
    if account.pw_uid==0 or not re.fullmatch('[a-zA-Z0-9_-]+',args.user):raise ValueError('--user 必须为现有普通用户。')
    disk=Path(args.models_root).resolve(strict=True);server=Path(args.server).resolve(strict=True);model=Path(args.model).resolve(strict=True)
    home=Path(account.pw_dir);deploy=home/'llama-dashboard'
    for p in (disk,server,model,home):
        if any(c in str(p) for c in "\n\r\x00'\"\\"):
            raise ValueError('安装路径不支持换行、引号或反斜杠。')
    if not disk.is_dir() or not server.is_file() or not os.access(server,os.X_OK):raise ValueError('模型目录或可执行 server 不正确。')
    if not model.is_relative_to(disk) or model.suffix.lower()!='.gguf':raise ValueError('主模型须为 models-root 中的 GGUF。')
    if args.context<1 or args.threads<1:raise ValueError('上下文和线程数须为正数。')
    if args.sleep_seconds!=-1 and not 30<=args.sleep_seconds<=86400:raise ValueError('sleep-seconds 须为 -1 或 30–86400。')
    help_result=command([str(server),'--help']);help_text=help_result.stdout+help_result.stderr
    for flag in ('--metrics','--sleep-idle-seconds','--jinja'):
        if flag not in help_text:raise ValueError('此 server 不支持必要参数：'+flag)
    argv=[str(server),'-m',str(model),'-c',str(args.context),'-ngl',args.gpu_layers,'-t',str(args.threads),'--parallel','1','--jinja','--no-webui','--metrics','--host','0.0.0.0','--port','8080']
    for flag,value in [('-md',args.draft_model),('--mmproj',args.mmproj)]:
        if value:
            path=Path(value).resolve(strict=True)
            if not path.is_relative_to(disk):raise ValueError('辅助模型须位于 models-root。')
            argv += [flag,str(path)]
    if args.draft_model:argv += ['--spec-type','draft-mtp','--spec-draft-n-max','2']
    extras=shlex.split(args.extra_args)
    if any(x in ('-m','--model','--host','--port','--sleep-idle-seconds') for x in extras):raise ValueError('主模型、入口及休眠请使用安装器对应选项。')
    argv += extras
    config={'argv':argv,'cwd':str(model.parent),'sleep_idle_seconds':args.sleep_seconds,
            'restart_on_failure':True,'autostart':not args.no_start,'environment':{}}
    unit=Path('/root/.config/systemd/user/llama-server.service')
    paths=[Path('/etc/llama-dashboard'),unit,unit.with_name(unit.name+'.d'),deploy,
           Path('/etc/systemd/system/llama-dashboard-control.service'),home/'.config/systemd/user/llama-dashboard.service',
           home/'.config/llama-dashboard/admin-token',Path('/usr/local/libexec/llama_dashboard_control.py'),Path('/usr/local/libexec/llama_dashboard_models.py')]
    conflicts=[str(p) for p in paths if p.exists()]
    if command(['pgrep','-x','llama-server'],check=False).returncode==0:conflicts.append('已有 llama-server 进程运行')
    for port in (8080,8090):
        with socket.socket() as probe:
            try:probe.bind(('0.0.0.0',port))
            except OSError:conflicts.append(f'端口 {port} 已占用')
    # Validate in a temporary process before writing any installation files.
    with tempfile.TemporaryDirectory(prefix='llama-dashboard-check-') as temporary:
        stage=Path(temporary)
        for name in ('llama_dashboard_control.py','llama_dashboard_models.py'):
            (stage/name).write_text(render((HERE/'src'/name).read_text(),account,disk))
        (stage/'llama-server-help.txt').write_text(help_text)
        (stage/'server.json').write_text(json.dumps(config))
        code="import sys,json,shlex;from pathlib import Path;sys.path.insert(0,sys.argv[1]);import llama_dashboard_control as c;import llama_dashboard_models as m;c.CONFIG=Path(sys.argv[1])/'server.json';cfg=json.loads(c.CONFIG.read_text());candidate,_=m.validate({'revision':m.revision(),'arguments':shlex.join(cfg['argv'][1:]+['--sleep-idle-seconds',str(cfg['sleep_idle_seconds'])])});print(json.dumps({'config':candidate,'conflicts':[u for u in c.audit_units() if u['conflict']]}))"
        check=command([sys.executable,'-c',code,str(stage)],check=False)
        if check.returncode:raise ValueError('启动参数检查未通过：'+check.stderr[-2500:])
        checked=json.loads(check.stdout);config=checked['config'];conflicts.extend(u['path'] for u in checked['conflicts'])
    print(json.dumps({'install_dir':str(deploy),'model':str(model),'binary':str(server),'context':args.context,
                      'autostart':not args.no_start,'conflicts':conflicts,'dry_run':args.dry_run},ensure_ascii=False,indent=2))
    if args.dry_run:
        print('仅检查，未写入文件或改变服务。' if not conflicts else '仅检查：发现现有服务/文件，实际安装会拒绝覆盖。')
        return
    if os.geteuid()!=0:raise ValueError('实际安装需要 sudo；可先普通权限 --dry-run。')
    if conflicts:raise ValueError('拒绝覆盖现有模型/Dashboard。请在确认用途后手工处理冲突，或使用干净系统。')
    def write(path,text,mode=0o644,owner=False):
        path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text);path.chmod(mode)
        if owner:os.chown(path,account.pw_uid,account.pw_gid)
        if shutil.which('restorecon'):command(['restorecon',str(path)],check=False)
    deploy.mkdir();os.chown(deploy,account.pw_uid,account.pw_gid)
    for name in ('llama_dashboard.py','llama_dashboard.html','model-manager.js'):
        write(deploy/name,render((HERE/'src'/name).read_text(),account,disk),owner=True)
    for name in ('llama_dashboard_control.py','llama_dashboard_models.py'):
        write(Path('/usr/local/libexec')/name,render((HERE/'src'/name).read_text(),account,disk),0o755)
    write(Path('/usr/local/libexec/llama-server-help.txt'),help_text)
    write(Path('/etc/llama-dashboard/server.json'),json.dumps(config,ensure_ascii=False,indent=2),0o600)
    token=home/'.config/llama-dashboard/admin-token'
    write(token,secrets.token_urlsafe(24)+'\n',0o600,True)
    os.chown(token.parent,account.pw_uid,account.pw_gid);token.parent.chmod(0o700)
    data=home/'llama-dashboard-data';data.mkdir(exist_ok=True);os.chown(data,account.pw_uid,account.pw_gid)
    backups=home/'backups';backups.mkdir(exist_ok=True);os.chown(backups,account.pw_uid,account.pw_gid)
    write(unit,'[Unit]\nDescription=llama.cpp managed by Dashboard\n[Service]\nType=simple\nExecStart=/usr/bin/false\n[Install]\nWantedBy=default.target\n')
    sys.path.insert(0,'/usr/local/libexec')
    import llama_dashboard_control as controller
    controller.write_policy(config)
    write(Path('/etc/systemd/system/llama-dashboard-control.service'),(HERE/'src/llama-dashboard-control.service').read_text())
    dash_unit=home/'.config/systemd/user/llama-dashboard.service'
    write(dash_unit,'[Unit]\nDescription=llama.cpp Dashboard\nAfter=network.target\n[Service]\nType=simple\nExecStart=/usr/bin/python3 '+quoted(deploy/'llama_dashboard.py')+'\nRestart=on-failure\nRestartSec=3\nNoNewPrivileges=true\n[Install]\nWantedBy=default.target\n',owner=True)
    receipt={'installed_paths':[str(p) for p in paths],'user':account.pw_name,'model':str(model),'autostart':not args.no_start}
    write(deploy/'installation.json',json.dumps(receipt,ensure_ascii=False,indent=2),owner=True)
    write(deploy/'README.md',(HERE/'README.md').read_text(),owner=True)
    command(['systemctl','daemon-reload'])
    if not args.no_start:
        for user,uid in [('root',0),(args.user,account.pw_uid)]:
            command(['loginctl','enable-linger',user]);command(['systemctl','start',f'user@{uid}.service'])
        controller.ctl('daemon-reload');controller.ctl('enable',controller.UNIT);controller.ctl('--no-block','start',controller.UNIT)
        command(['systemctl','enable','--now','llama-dashboard-control.service'])
        prefix=['runuser','-u',args.user,'--','env',f'XDG_RUNTIME_DIR=/run/user/{account.pw_uid}','systemctl','--user']
        command(prefix+['daemon-reload']);command(prefix+['enable','--now','llama-dashboard.service'])
    print('安装文件已写入。'+('未启用或启动服务。' if args.no_start else '已提交模型启动，首次加载可能需数分钟，请在 Dashboard 查看状态。'))
    print('Dashboard: http://本机局域网IP:8090/\n管理口令文件：'+str(token))
    print('未修改防火墙、BIOS 或 GPU 驱动；请按 README 检查模型健康和开机挂载。')

if __name__=='__main__':
    try:main()
    except (ValueError,OSError,KeyError,subprocess.SubprocessError) as error:
        print('安装未完成：'+str(error),file=sys.stderr);sys.exit(1)
