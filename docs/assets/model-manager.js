'use strict';
// No persistence of the management password in browser storage.
const managerFields=[
 ['-c','上下文容量','输入和输出共享的 token 总容量。当前 Qwen 512K 使用 YaRN 扩展；其他模型默认准备 262K。'],
 ['--parallel','并行 Slot 数','并行处理任务的槽位数量，不是连接数；提高后需重新评估每槽上下文与内存。'],
 ['-ngl','GPU 层数','999 表示尽可能交给 GPU；也可填写此版本支持的 auto 或 all。'],
 ['-t','生成 CPU 线程','主要影响 CPU 计算部分；过多线程不一定更快。'],
 ['-b','逻辑批量','Prompt 处理批量大小；较大可能更快，也可能增加内存。'],
 ['-ub','物理微批量','GPU 实际分块处理大小，一般不超过逻辑批量。'],
 ['-ctk','K 缓存格式','如 q8_0、q4_0、f16；降低精度可省内存，但效果和兼容性需实测。'],
 ['-ctv','V 缓存格式','通常与 K 一起设置；部分格式要求 Flash Attention。'],
 ['--flash-attn','Flash Attention','on / off / auto；是否采用高效注意力实现，取决于模型与后端。'],
 ['--cache-ram','额外 RAM 提示缓存','单位 MiB；0 关闭这一层缓存，-1 无上限。不是整个模型的内存上限。'],
 ['--sleep-idle-seconds','空闲休眠秒数','1800 为 30 分钟，-1 关闭；空闲后卸载权重与 KV，下次请求重新加载。'],
 ['--reasoning-budget','默认思考预算','-1 不限制，0 不思考；Codex 兼容层按请求选择的强度覆盖。'],
 ['--rope-scaling','RoPE 扩展方式','none / linear / yarn；只对匹配的模型设置，不能保证扩展后的质量。'],
 ['--rope-scale','RoPE 扩展倍数','当前 Qwen 为 2；其他模型不要直接照搬。'],
 ['--yarn-orig-ctx','YaRN 原始上下文','当前 Qwen 为 262144，与 RoPE 扩展设置配套。'],
 ['--spec-type','推测解码方式','当前 Qwen 使用 draft-mtp。清空草稿模型时请同时清空此项和草稿长度。'],
 ['--spec-draft-n-max','每步最大草稿 Token','当前值 2；增加后是否更快取决于接受率。'],
 ['-md','MTP / 草稿模型路径','必须与主模型架构兼容。普通模型不应继承 Qwen shared MTP。'],
 ['--mmproj','图像投影模型路径','用于图片输入，必须匹配主模型；纯文本模型可清空。'],
];
let modelEditor=null,modelDirty=false,managerBusy=false,jobTimer=null,lastJobState=null,prepareSerial=0,preparing=false,lastArgumentModel=null;
function shellWords(text){
 const words=[];let word='',quote=null,started=false;
 for(let i=0;i<text.length;i++){const c=text[i];
  if(quote==="'"){if(c==="'")quote=null;else word+=c;started=true;continue;}
  if(quote==='"'){if(c==='"')quote=null;else if(c==='\\'&&['"','\\','$','`','\n'].includes(text[i+1]))word+=text[++i];else word+=c;started=true;continue;}
  if(c==='"'||c==="'"){quote=c;started=true;}else if(c==='\\'){if(++i>=text.length)throw Error('参数末尾的反斜杠不完整');word+=text[i];started=true;}
  else if(/\s/.test(c)){if(started){words.push(word);word='';started=false;}}else{word+=c;started=true;}
 }
 if(quote)throw Error('参数中的引号没有闭合');if(started)words.push(word);return words;
}
function shellQuote(value){return /^[a-zA-Z0-9_/.=,:+@%-]+$/.test(value)?value:"'"+value.replaceAll("'","'\\''")+"'";}
function argumentGroups(){
 const specs=new Map(modelEditor.help.flatMap(e=>e.names.map(n=>[n,e]))),words=shellWords($('launch-args').value),groups=[];
 for(let i=0;i<words.length;){let flag=words[i++],values=[];
  if(flag.startsWith('--')&&flag.includes('=')){const k=flag.indexOf('=');values=[flag.slice(k+1)];flag=flag.slice(0,k);}
  const spec=specs.get(flag);if(!spec)throw Error('未知参数：'+flag);
  while(values.length<spec.arity){if(i>=words.length)throw Error(flag+' 缺少值');values.push(words[i++]);}
  groups.push([flag,...values]);
 }return groups;
}
function aliases(flag){return modelEditor.help.find(e=>e.names.includes(flag))?.names||[flag];}
function syncFields(forceModel=false){
 try{const groups=argumentGroups();for(const [flag]of managerFields){const names=aliases(flag),value=groups.filter(g=>names.includes(g[0])).at(-1)?.[1]||'';document.querySelector(`[data-flag="${flag}"]`).value=value;}
  const path=groups.find(g=>['-m','--model'].includes(g[0]))?.[1];
  if(path&&(forceModel||path!==lastArgumentModel)){$('model-path').value=path;$('model-inventory').value=path;}
  lastArgumentModel=path;
 }catch(error){$('editor-result').textContent=error.message;}
}
function updateField(flag,value){
 try{const names=aliases(flag),groups=argumentGroups().filter(g=>!names.includes(g[0]));if(value.trim())groups.push([flag,value.trim()]);$('launch-args').value=groups.map(g=>g.map(shellQuote).join(' ')).join('\n');modelDirty=true;$('preview-box').hidden=true;}
 catch(error){$('editor-result').textContent=error.message;syncFields();}
}
function renderHelp(){
 if(!modelEditor)return;const search=$('help-search').value.toLowerCase();
 $('parameter-help').innerHTML=modelEditor.help.map(e=>({...e,zh:managerFields.find(f=>e.names.includes(f[0]))?.slice(1).join('：')||''})).filter(e=>(e.names.join(' ')+' '+e.description+' '+e.zh).toLowerCase().includes(search)).map(e=>`<details><summary>${esc(e.names.join(', '))} ${esc(e.argument)}${e.restricted?' · 此编辑器禁用':''}</summary>${e.zh?`<p>${esc(e.zh)}</p>`:''}<pre>${esc(e.description)}</pre></details>`).join('');
}
async function managerFetch(path,body){
 const options={cache:'no-store',signal:AbortSignal.timeout(60000)};
 if(body){const token=$('model-token').value;if(!token&&body.action!=='preview_model')throw Error('请填写管理口令');Object.assign(options,{method:'POST',headers:{'Content-Type':'application/json','X-Dashboard-Request':'1','Authorization':'Bearer '+token},body:JSON.stringify(body)});}
 const r=await fetch(path,options),data=await r.json();if(!r.ok||data.error)throw Error(data.error||`HTTP ${r.status}`);return data;
}
async function readEditor(force=false){
 if(modelDirty&&!force&&!confirm('重新读取会覆盖尚未保存的参数，继续吗？'))return;
 try{modelEditor=await managerFetch('/api/editor');$('launch-args').value=modelEditor.arguments;$('model-path').value=modelEditor.model_path;modelDirty=false;
 $('editor-status').textContent='当前保存的模型：'+modelEditor.model_path+' · 程序：'+modelEditor.binary;
 $('common-fields').innerHTML=managerFields.map(([flag,label,help])=>`<label class="${['-md','--mmproj'].includes(flag)?'field-wide':''}">${esc(label)} <small>${esc(flag)}</small><input data-flag="${flag}" ${['-md','--mmproj'].includes(flag)?'list="aux-models"':''} placeholder="未设置（使用 server 默认值）"><small>${esc(help)}</small></label>`).join('');
 for(const input of document.querySelectorAll('[data-flag]'))input.oninput=()=>updateField(input.dataset.flag,input.value);
 syncFields(true);renderHelp();renderJob(modelEditor.job);
 }catch(error){$('editor-status').textContent='读取失败：'+error.message;}
}
async function scanModels(){
 $('scan-models').disabled=true;$('scan-status').textContent='正在扫描 GGUF 与分片完整性…';
 try{const result=await managerFetch('/api/inventory');const main=result.models.filter(m=>m.kind==='model');
 $('model-inventory').innerHTML='<option value="">选择硬盘上的主模型</option>'+main.map(m=>`<option value="${esc(m.path)}" ${m.valid?'':'disabled'}>${esc(m.relative)} · ${m.valid?f(m.bytes/1073741824,1)+' GiB / '+m.shards+' 分片':'文件不完整'}</option>`).join('');
 $('aux-models').innerHTML=result.models.filter(m=>m.kind!=='model'&&m.valid).map(m=>`<option value="${esc(m.path)}">${esc(m.kind)}</option>`).join('');
 $('scan-status').textContent=`发现 ${main.length} 个主模型、${result.models.length-main.length} 个辅助模型${result.truncated?'（扫描达到上限，可手填其他路径）':''}${result.errors.length?'；部分目录未能读取':''}`;
 }catch(error){$('scan-status').textContent='扫描失败：'+error.message;}finally{$('scan-models').disabled=false;}
}
function preparationState(active){
 preparing=active;
 for(const el of document.querySelectorAll('[data-flag],#launch-args,#editor-refresh'))el.disabled=active;
 $('validate-model').disabled=active||managerBusy;
 $('apply-model').disabled=active||managerBusy||!!jobTimer;
}
async function prepareModel(){
 if(!modelEditor||managerBusy)return;
 const serial=++prepareSerial,path=$('model-path').value.trim();
 preparationState(true);$('model-summary').textContent='正在检查模型文件并同步参数…';$('preview-box').hidden=true;
 try{const result=await managerFetch('/api/control',{action:'preview_model',model_path:path,arguments:$('launch-args').value});
  if(serial!==prepareSerial)return;
  $('launch-args').value=result.arguments;modelDirty=true;syncFields(true);
  $('model-summary').textContent=result.notice;$('editor-result').textContent='模型与参数已同步，尚未保存或加载。';
 }catch(error){if(serial===prepareSerial){$('model-summary').textContent='准备失败：'+error.message;$('editor-result').textContent='准备失败：'+error.message;}}
 finally{if(serial===prepareSerial)preparationState(false);}
}
async function checkOrApply(apply){
 if(!modelEditor||managerBusy||preparing)return;
 try{const selected=argumentGroups().find(g=>['-m','--model'].includes(g[0]))?.[1];if(selected!==$('model-path').value.trim())throw Error('所选路径与参数中的主模型不同，请先点击“选择此模型并准备参数”。');}
 catch(error){$('editor-result').textContent=error.message;return;}
 if(apply&&!confirm('确认保存并加载此模型？当前 KV 缓存将清空，加载期间暂不可用；失败会尝试恢复原模型。请先暂停客户端发送新请求。'))return;
 managerBusy=true;$('apply-model').disabled=true;$('validate-model').disabled=true;$('editor-result').textContent=apply?'正在检查并提交加载任务…':'正在检查参数和文件…';
 try{const result=await managerFetch('/api/control',{action:apply?'apply_model':'validate_model',revision:modelEditor.revision,arguments:$('launch-args').value,confirm_restart:apply});
 $('preview-box').hidden=false;$('preview-content').textContent=`模型：${result.preview.model}\n权重：${f(result.preview.bytes/1073741824,1)} GiB · ${result.preview.shards} 分片\n${result.preview.warnings.join('\n')}\n\n${result.preview.arguments}`;
 $('editor-result').textContent=apply?'加载任务已提交。页面关闭后任务仍会继续。':'文件和参数格式检查通过；实际运行兼容性将在加载时验证。';
 if(apply){modelDirty=false;$('model-token').value='';renderJob(result.job);}
 }catch(error){$('editor-result').textContent='未完成：'+error.message;}finally{managerBusy=false;$('validate-model').disabled=false;if(!jobTimer)$('apply-model').disabled=false;}
}
function renderJob(job){
 if(!job){$('model-job').hidden=true;return;}
 const active=['queued','stopping','loading','rolling_back'].includes(job.state);
 $('model-job').hidden=false;$('model-job').textContent=`${job.message} · ${job.target} · 备份：${job.backup}`;
 $('job-log-box').hidden=!job.load_error;$('job-log').textContent=(job.load_error||'')+'\n'+(job.load_log||'');
 $('apply-model').disabled=active||preparing||managerBusy;$('policy-save').disabled=active||!!policyState?.conflicts?.length;
 if(active&&!jobTimer)jobTimer=setInterval(pollModelJob,3000);
 if(!active&&jobTimer){clearInterval(jobTimer);jobTimer=null;loadPolicy();readEditor(true);refresh();}
 lastJobState=job.state;
}
async function pollModelJob(){try{renderJob((await managerFetch('/api/model-job')).job);}catch(error){$('model-job').textContent='任务状态读取失败，可刷新页面继续查看：'+error.message;}}
$('editor-refresh').onclick=()=>readEditor();$('scan-models').onclick=scanModels;
$('model-inventory').onchange=()=>{if($('model-inventory').value){$('model-path').value=$('model-inventory').value;prepareModel();}};
$('model-path').oninput=()=>{++prepareSerial;preparationState(false);$('preview-box').hidden=true;$('model-summary').textContent='路径已修改，请准备参数。';};
$('use-model').onclick=prepareModel;$('help-search').oninput=renderHelp;
$('launch-args').oninput=()=>{modelDirty=true;$('preview-box').hidden=true;};$('launch-args').onchange=()=>syncFields();
$('validate-model').onclick=()=>checkOrApply(false);$('apply-model').onclick=()=>checkOrApply(true);
document.querySelector('nav').insertAdjacentHTML('beforeend','<a href="#model-manager">模型与参数</a>');
readEditor();
