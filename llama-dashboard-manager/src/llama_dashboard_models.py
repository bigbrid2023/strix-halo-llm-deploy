#!/usr/bin/python3
"""Local GGUF configuration editor and recoverable, single-model load jobs."""
import copy
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import struct
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import llama_dashboard_control as control

DISK = Path('/MODEL_DISK')
DATA = Path('/home/__DASH_USER__/llama-dashboard-data')
STATE = Path('/etc/llama-dashboard/model-job.json')
PROFILES = Path('/etc/llama-dashboard/model-profiles.json')
HELP = Path(__file__).with_name('llama-server-help.txt')
BACKUPS = Path('/home/__DASH_USER__/backups')
ACTIVE = {'queued', 'stopping', 'loading', 'rolling_back'}
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
LOAD_TIMEOUT = 600

# These are infrastructure/command-execution options, not inference tuning.
# The server is an existing root user service; never expose its host tools.
BLOCKED = {
    '--help', '-h', '--usage', '--version', '--cache-list', '-cl', '--completion-bash',
    '--list-devices', '--tools', '--tools-runtime', '--mcp-servers-config', '--mcp-servers-json',
    '--agent', '-ag', '--ui-mcp-proxy', '--webui-mcp-proxy', '--props',
    '--api-key', '--api-key-file', '--ssl-key-file', '--ssl-cert-file', '--api-prefix',
    '--models-dir', '--models-preset', '--models-max', '--models-autoload', '--no-models-autoload',
    '--reuse-port', '--model-url', '-mu', '--hf-repo', '-hf', '-hfr', '--hf-file', '-hff',
    '--hf-token', '-hft', '--docker-repo', '-dr', '--model-draft-url', '--hf-repo-draft',
    '--video-ffmpeg-dir', '--rpc', '--spec-default', '--path',
    '--spec-draft-hf', '-hfd', '-hfrd', '--mmproj-url', '-mmu',
}
OUTPUT_FLAGS = {'--log-file', '--log-prompts-dir', '--slot-save-path'}
FAMILY_FLAGS = {'-md', '--model-draft', '--spec-type', '--spec-draft-n-max', '--mmproj', '-mm',
                '--image-min-tokens', '--image-max-tokens', '--rope-scaling', '--rope-scale',
                '--yarn-orig-ctx', '--yarn-ext-factor', '--yarn-attn-factor', '--yarn-beta-slow',
                '--yarn-beta-fast', '--rope-freq-base', '--rope-freq-scale', '--chat-template',
                '--chat-template-file', '--chat-template-kwargs', '--reasoning-format'}

def save_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(obj, ensure_ascii=False, indent=2))
    temp.chmod(0o600)
    temp.replace(path)

def revision():
    return hashlib.sha256(control.CONFIG.read_bytes()).hexdigest()

def job():
    return json.loads(STATE.read_text()) if STATE.exists() else None

def public_job():
    value = job()
    if not value: return None
    return {k:v for k,v in value.items() if k not in ('old_config', 'new_config')}

def ensure_no_job():
    value = job()
    if value and value['state'] in ACTIVE:
        raise ValueError('已有模型加载/恢复任务，请等待完成；状态可在页面查看。')

def help_entries():
    entries = []
    for line in HELP.read_text().splitlines():
        if line.startswith('-') and not line.startswith('-----'):
            parts = re.split(r'\s{2,}(?=[^\s-])', line, maxsplit=1)
            header = parts[0].strip()
            names = re.findall(r'(?<!\S)--?[A-Za-z0-9][A-Za-z0-9-]*', header)
            if not names: continue
            tail = header[header.rfind(names[-1])+len(names[-1]):].strip()
            arity = 2 if tail == 'START END' else int(bool(tail))
            entries.append({'names':names, 'argument':tail, 'arity':arity,
                            'description':parts[1] if len(parts)>1 else ''})
        elif entries and line.startswith(' '):
            entries[-1]['description'] += '\n'+line.strip()
    for e in entries:
        e['restricted'] = any(n in BLOCKED or n.endswith('-default') or n.startswith('--fim-') for n in e['names'])
    return entries

def options():
    return {name:e for e in help_entries() for name in e['names']}

def parse_args(text):
    if not isinstance(text, str) or len(text)>48000:
        raise ValueError('参数文本过长或格式不正确。')
    tokens = shlex.split(text, comments=False)
    if any(any(ord(ch)<32 for ch in token) for token in tokens):
        raise ValueError('参数值不能包含控制字符或换行。')
    specs=options(); groups=[]; i=0
    while i<len(tokens):
        flag=tokens[i]; i+=1
        # Accept --option=value as well as --option value.
        inline=None
        if flag.startswith('--') and '=' in flag: flag,inline=flag.split('=',1)
        if flag not in specs: raise ValueError('当前编译版本不支持参数：'+flag)
        spec=specs[flag]
        if flag in BLOCKED or flag.endswith('-default') or flag.startswith('--fim-'):
            raise ValueError(flag+' 属于命令执行、下载或服务入口设置，不在此模型编辑器中开放。')
        values=[] if inline is None else [inline]
        if len(values)>spec['arity']: raise ValueError(flag+' 不接受参数值。')
        needed=spec['arity']-len(values)
        if i+needed>len(tokens): raise ValueError(flag+' 缺少参数值。')
        values+=tokens[i:i+needed];i+=needed
        if any(v in specs for v in values): raise ValueError(flag+' 缺少参数值。')
        groups.append([flag,*values])
    return groups

def gguf(path):
    path=path.resolve(strict=True)
    if not path.is_relative_to(DISK.resolve()) or path.suffix.lower()!='.gguf' or not path.is_file():
        raise ValueError('模型必须是指定硬盘中的 GGUF 文件。')
    with path.open('rb') as f:
        header=f.read(24)
        if len(header)!=24 or header[:4]!=b'GGUF': raise ValueError('文件不是有效的 GGUF：'+path.name)
        _,version,tensors,_=struct.unpack('<4sIQQ',header)
        if version not in (2,3):raise ValueError('GGUF 版本不支持：'+path.name)
    match=re.search(r'-(\d{5})-of-(\d{5})\.gguf$',path.name,re.I)
    files=[path]
    if match:
        if int(match[1])!=1: raise ValueError('请选择分片模型的第 00001 个文件。')
        count=int(match[2])
        if not 1<=count<=1024: raise ValueError('分片数量异常。')
        files=[path.with_name(path.name[:match.start()]+f'-{i:05d}-of-{count:05d}.gguf') for i in range(1,count+1)]
        tensors=0
        for f in files:
            resolved=f.resolve(strict=True)
            if not resolved.is_relative_to(DISK.resolve()):raise ValueError('分片超出模型硬盘。')
            with f.open('rb') as stream:
                header=stream.read(24)
                if len(header)!=24 or header[:4]!=b'GGUF':raise ValueError('无效分片：'+f.name)
                _,v,n,_=struct.unpack('<4sIQQ',header)
                if v not in (2,3):raise ValueError('分片版本不支持：'+f.name)
                tensors+=n
    if tensors==0:raise ValueError('文件没有模型张量（可能只是词表）：'+path.name)
    return path, sum(f.stat().st_size for f in files), len(files)

def inventory():
    found=[];visited=0;truncated=False;errors=[]
    def scan_error(error): errors.append(str(error))
    for root, dirs, files in os.walk(DISK, followlinks=False, onerror=scan_error):
        dirs[:]=[d for d in dirs if not d.startswith('.') and d not in ('$RECYCLE.BIN','System Volume Information')]
        visited+=len(files)
        if visited>150000 or len(found)>=1000: truncated=True;break
        for name in sorted(files):
            if not name.lower().endswith('.gguf'):continue
            if name.startswith('ggml-vocab-'):continue
            split=re.search(r'-(\d{5})-of-\d{5}\.gguf$',name,re.I)
            if split and split[1]!='00001':continue
            path=Path(root)/name
            if path.is_symlink():continue
            kind='projection' if 'mmproj' in name.lower() else 'draft' if ('mtp' in name.lower() or '/MTP/' in str(path)) else 'model'
            item={'path':str(path),'name':name,'kind':kind,'relative':str(path.relative_to(DISK))}
            try:
                _,size,count=gguf(path);item.update(bytes=size,shards=count,valid=True)
            except (OSError, ValueError) as error:item.update(valid=False,error=str(error))
            found.append(item)
    return {'root':str(DISK),'models':found,'truncated':truncated,'errors':errors}

def format_groups(groups):
    return '\n'.join(shlex.join(g) for g in groups)

def current_groups(config):
    # Convert legacy relative main-model path to absolute for the editor.
    argv=list(config['argv'][1:])
    i=argv.index('-m');argv[i+1]=str((Path(config['cwd'])/argv[i+1]).resolve())
    groups=parse_args(shlex.join(argv))
    groups=[g for g in groups if g[0]!='--sleep-idle-seconds']
    groups.append(['--sleep-idle-seconds',str(config['sleep_idle_seconds'])])
    return groups

def remember(config):
    profiles=json.loads(PROFILES.read_text()) if PROFILES.exists() else {}
    path=str((Path(config['cwd'])/config['argv'][config['argv'].index('-m')+1]).resolve())
    profiles[path]=config
    save_json(PROFILES,profiles)

def editor():
    config=json.loads(control.CONFIG.read_text())
    return {'revision':revision(),'binary':config['argv'][0], 'arguments':format_groups(current_groups(config)),
            'model_path':str((Path(config['cwd'])/config['argv'][config['argv'].index('-m')+1]).resolve()),
            'help':help_entries(), 'job':public_job(), 'data_directory':str(DATA), 'disk':str(DISK)}

def preview_model(request):
    path,_,_=gguf(Path(request['model_path']))
    config=json.loads(control.CONFIG.read_text())
    profiles=json.loads(PROFILES.read_text()) if PROFILES.exists() else {}
    draft = parse_args(request['arguments']) if 'arguments' in request else None
    if draft is not None:
        main = [g for g in draft if g[0] in ('-m','--model')]
        if len(main) != 1:raise ValueError('必须且只能指定一个 -m 主模型。')
        old = Path(main[0][1])
        same = path == old or ('Qwen3.8-Flash-Next' in path.name and 'Qwen3.8-Flash-Next' in old.name)
        if same:
            groups = [['-m',str(path)] if g[0] in ('-m','--model') else g for g in draft]
            return {'arguments':format_groups(groups), 'same_family':True,
                    'notice':'模型路径已同步；保留页面中已编辑的参数，尚未保存或加载。'}
    if str(path) in profiles:
        saved=copy.deepcopy(profiles[str(path)])
        saved['sleep_idle_seconds']=config['sleep_idle_seconds']
        return {'arguments':format_groups(current_groups(saved)), 'same_family':True,
                'notice':'已恢复此模型上次成功加载的参数；沿用当前空闲休眠策略。'}
    if 'Qwen3.8-Flash-Next' in path.name:
        for saved_path,saved in profiles.items():
            if 'Qwen3.8-Flash-Next' in Path(saved_path).name:
                saved=copy.deepcopy(saved);saved['sleep_idle_seconds']=config['sleep_idle_seconds'];config=saved;break
    groups=draft if draft is not None else current_groups(config)
    old=Path(config['cwd'])/config['argv'][config['argv'].index('-m')+1]
    if draft is not None:old=Path(next(g[1] for g in draft if g[0] in ('-m','--model')))
    # Keep exact parameters for another quantization of this known family.
    same_family='Qwen3.8-Flash-Next' in path.name and 'Qwen3.8-Flash-Next' in old.name
    if not same_family:
        groups=[g for g in groups if g[0] not in FAMILY_FLAGS and not any(x in options()[g[0]]['names'] for x in FAMILY_FLAGS)
                and not g[0].startswith('--spec-')]
        groups=[g for g in groups if g[0] not in ('-c','--ctx-size')]
        groups.append(['-c','262144'])
    groups=[['-m',str(path)] if g[0] in ('-m','--model') else g for g in groups]
    return {'arguments':format_groups(groups),'same_family':same_family,
            'notice':'同家族量化，保留当前参数；显存和速度仍需实测。' if same_family else
            '已清除原模型专用 MTP、mmproj、模板和 RoPE 设置，上下文暂设 262144。请按新模型能力调整；不支持时会加载失败并恢复原配置。'}

def check_paths(groups, cwd):
    specs=options()
    for group in groups:
        flag=group[0];spec=specs[flag]
        if len(group)<2:continue
        if flag in OUTPUT_FLAGS:
            p=Path(group[1])
            if not p.is_absolute() or not p.resolve().is_relative_to(DATA.resolve()):
                raise ValueError(flag+' 的输出路径须在 '+str(DATA))
            continue
        # Includes all input files described by this binary (LoRA/templates/grammar/etc.).
        if re.search(r'FNAME|FILE|PATH|DIR',spec['argument']):
            values=group[1].split(',') if 'lora' in flag or 'control-vector' in flag else [group[1]]
            for value in values:
                if 'scaled' in flag:value=value.rsplit(':',1)[0]
                p=(cwd/Path(value)).resolve(strict=True)
                if not p.is_relative_to(DISK.resolve()) and not p.is_relative_to(DATA.resolve()):
                    raise ValueError(flag+' 的输入文件须在模型硬盘或 '+str(DATA))

def validate(request):
    if request.get('revision')!=revision(): raise ValueError('配置已被更新，请重新读取当前配置后再编辑。')
    groups=parse_args(request.get('arguments'))
    model_groups=[g for g in groups if g[0] in ('-m','--model')]
    if len(model_groups)!=1:raise ValueError('必须且只能指定一个 -m 主模型。')
    path,size,count=gguf(Path(model_groups[0][1]))
    if 'mmproj' in path.name.lower() or 'mtp' in path.name.lower():raise ValueError('MTP/mmproj 不是主模型，请在对应参数中指定。')
    for group in groups:
        if group[0] in ('--host','--port') and group[1]!= {'--host':'0.0.0.0','--port':'8080'}[group[0]]:
            raise ValueError('当前 Dashboard/Codex 使用固定入口 0.0.0.0:8080。')
    sleep=[g for g in groups if g[0]=='--sleep-idle-seconds']
    if len(sleep)>1:raise ValueError('休眠参数重复。')
    seconds=int(sleep[0][1]) if sleep else 1800
    if seconds!=-1 and not 30<=seconds<=86400:raise ValueError('休眠秒数须为 -1（关闭）或 30–86400。')
    # Preserve the integration endpoints and metrics even in a minimal edited command.
    groups=[g for g in groups if g[0] not in ('--sleep-idle-seconds','--host','--port','--metrics')]
    for g in groups:
        if g[0] in ('-m','--model'):g[:]=['-m',str(path)]
    groups += [['--host','0.0.0.0'],['--port','8080'],['--metrics']]
    check_paths(groups,path.parent)
    for group in groups:
        if group[0] in ('-md','--model-draft','--spec-draft-model','--mmproj','-mm'):gguf((path.parent/Path(group[1])))
    old=json.loads(control.CONFIG.read_text());candidate=copy.deepcopy(old)
    candidate.update(argv=[old['argv'][0]]+[x for g in groups for x in g],cwd=str(path.parent),sleep_idle_seconds=seconds)
    warnings=['启动成功只代表当前编译版能够加载；模型的工具调用、思考强度和长上下文质量需另行验证。']
    if size>70*1024**3:warnings.append('模型权重较大，加上 KV、MTP 后可能超出可用内存；失败会尝试恢复原配置。')
    return candidate, {'model':path.name,'bytes':size,'shards':count,'sleep_idle_seconds':seconds,
                       'arguments':format_groups(groups+[['--sleep-idle-seconds',str(seconds)]]),'warnings':warnings}

def require_idle():
    state=control.info('-p','ActiveState','-p','MainPID')
    if state.get('MainPID')=='0' and state.get('ActiveState') in ('inactive','failed'):return
    try:
        with OPENER.open('http://127.0.0.1:8080/metrics',timeout=5) as response:metrics=response.read().decode()
        for key in ('requests_processing','requests_deferred'):
            match=re.search(r'^llamacpp:'+key+r'\s+(\S+)',metrics,re.M)
            if not match or float(match[1])!=0:raise ValueError('有处理或排队任务，请等待完成后再切换。')
    except ValueError:raise
    except Exception as error:raise ValueError('无法确认模型空闲，暂不执行切换：'+str(error))

def start_job(request):
    ensure_no_job()
    if request.get('confirm_restart') is not True:raise ValueError('加载配置需要确认重启。')
    candidate,report=validate(request)
    if control.status()['conflicts']:raise ValueError('存在其他模型进程或保活任务，不能切换。')
    require_idle()
    job_id=uuid.uuid4().hex
    backup=BACKUPS/('model-switch-'+time.strftime('%Y%m%d-%H%M%S')+'-'+job_id[:6]);backup.mkdir()
    shutil.copy2(control.CONFIG,backup/'server.json')
    if control.DROPIN.exists():shutil.copy2(control.DROPIN,backup/control.DROPIN.name)
    record={'id':job_id,'state':'queued','message':'已备份，准备加载。','created_at':time.time(),
            'backup':str(backup),'target':report['model'],'old_config':json.loads(control.CONFIG.read_text()),'new_config':candidate}
    remember(record['old_config'])
    save_json(STATE,record)
    try:
        control.run(['systemd-run','--quiet','--collect','--unit=llama-dashboard-apply-'+job_id,
                     '--property=TimeoutStartSec=25min','--property=Type=exec',
                     '/usr/bin/python3',str(Path(__file__).resolve()),'--worker',job_id])
    except Exception as error:
        record.update(state='failed',message='未启动加载任务：'+str(error));save_json(STATE,record);raise
    return {'ok':True,'job':public_job(),'preview':report}

def update_job(record,state,message):
    record.update(state=state,message=message,updated_at=time.time())
    save_json(STATE,record)

def stop_model():
    control.ctl('--no-block','stop',control.UNIT)
    deadline=time.monotonic()+150
    while time.monotonic()<deadline:
        s=control.info('-p','MainPID','-p','ActiveState')
        if s.get('MainPID')=='0' and s.get('ActiveState') in ('inactive','failed'):return
        time.sleep(1)
    raise RuntimeError('旧模型未能完全退出，拒绝同时启动第二个模型。')

def await_ready(config):
    deadline=time.monotonic()+LOAD_TIMEOUT
    while time.monotonic()<deadline:
        s=control.info('-p','ActiveState','-p','MainPID')
        if s.get('ActiveState') in ('failed','inactive'):raise RuntimeError('模型进程退出，请查看启动日志。')
        try:
            with OPENER.open('http://127.0.0.1:8080/health',timeout=3) as response:
                ready=json.load(response).get('status')=='ok'
            if ready and int(s.get('MainPID','0'))>0:
                with OPENER.open('http://127.0.0.1:8080/props',timeout=3) as response:props=json.load(response)
                expected=Path(config['argv'][config['argv'].index('-m')+1]).name
                if Path(props.get('model_path','')).name==expected and props.get('endpoint_metrics') is True:return
        except Exception:pass
        time.sleep(2)
    raise RuntimeError('10 分钟内未能完成模型加载。')

def activate(config):
    temporary=copy.deepcopy(config);temporary['restart_on_failure']=False
    control.write_policy(temporary)
    control.ctl('daemon-reload')
    control.ctl('reset-failed',control.UNIT,check=False)
    control.ctl('--no-block','start',control.UNIT)
    await_ready(config)

def log_excerpt():
    return control.run(['journalctl','_SYSTEMD_USER_UNIT='+control.UNIT,'-n','35','--no-pager','-o','cat'],check=False)[-10000:]

def worker(job_id):
    record=job()
    if not record or record['id']!=job_id or record['state']!='queued':return
    mutated=False
    try:
        if control.status()['conflicts']:raise ValueError('执行前检测到新出现的管理冲突，已取消。')
        require_idle()
        update_job(record,'stopping','正在释放旧模型；期间推理服务暂不可用。')
        # A request arriving after the idle check may still race with stop. systemd SIGTERM
        # is used; no SIGKILL is sent by this worker.
        mutated=True
        stop_model()
        if control.processes():raise RuntimeError('旧模型退出后出现其他模型进程，拒绝同时加载。')
        update_job(record,'loading','正在加载 '+record['target']+'，最多等待 10 分钟。')
        activate(record['new_config'])
        save_json(control.CONFIG,record['new_config'])
        remember(record['new_config'])
        control.write_policy(record['new_config']);control.ctl('daemon-reload')
        update_job(record,'succeeded','新模型已就绪，启动参数已保存。')
    except Exception as error:
        record['load_error']=str(error)
        try:record['load_log']=log_excerpt()
        except Exception as log_error:record['load_log']='无法读取启动日志：'+str(log_error)
        if not mutated:
            update_job(record,'failed','检查未通过，原模型未更改：'+str(error));return
        update_job(record,'rolling_back','加载失败，正在恢复原模型：'+str(error))
        try:
            stop_model()
            if control.processes():raise RuntimeError('检测到其他模型进程，请先解决冲突再恢复。')
            activate(record['old_config'])
            save_json(control.CONFIG,record['old_config'])
            control.write_policy(record['old_config']);control.ctl('daemon-reload')
            update_job(record,'rolled_back','新配置加载失败；原模型已恢复。')
        except Exception as restore_error:
            # Keep the original persistent configuration even if hardware/resources block recovery.
            save_json(control.CONFIG,record['old_config'])
            control.write_policy(record['old_config']);control.ctl('daemon-reload')
            update_job(record,'failed','自动恢复未就绪：'+str(restore_error)+'；原配置已还原，请查看服务日志。')
    finally:
        save_json(Path(record['backup'])/'result.json',record)

def handle(request):
    action=request.get('action')
    if action=='editor':return editor()
    if action=='inventory':return inventory()
    if action=='model_job':return {'job':public_job()}
    if action=='preview_model':return preview_model(request)
    if action=='validate_model':
        _,report=validate(request);return {'ok':True,'preview':report}
    if action=='apply_model':return start_job(request)
    raise ValueError('不支持的模型操作。')

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--worker' and re.fullmatch('[0-9a-f]{32}',sys.argv[2]):worker(sys.argv[2])
    else:raise SystemExit('Only internal model worker invocation is supported')
