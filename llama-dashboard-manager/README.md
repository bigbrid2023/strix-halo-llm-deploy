# llama.cpp Dashboard：监控、模型切换和启动参数管理

版本：2026-09-16。适合 AMD Ryzen AI Max+ 395 / 类似统一内存 Linux 机器；其他 Linux 硬件也可运行，但本版 GPU 传感器主要适配 AMD。应用只使用 Python 标准库，不需要 npm、数据库或云端账号。

## 包含什么

- 运行结果：生成 Token、生成速度、Prompt 处理速度和近期趋势。
- MTP 和提示词缓存：草稿接受率、接受位置、缓存复用率。
- 任务与上下文：处理/排队请求、Slot 状态与上下文容量。
- 硬件：AMD GPU 利用率、温度、功耗、显存/GTT、系统内存、Swap。
- 服务策略：开机启动、异常退出自动重启、空闲休眠。
- 模型管理：扫描 GGUF、检查分片、选择模型、保存每个模型的成功参数、加载失败自动恢复。
- 参数编辑：常用参数中文说明、完整命令行参数编辑、对应 binary 的可搜索帮助。

**不含模型权重、llama-server 二进制、GPU 驱动、原部署机器的配置/口令或 Windows Codex 配置。** 请使用朋友机器上已能工作的 llama-server。Qwen shared MTP 需要相应支持它的编译版本；普通官方 binary 不一定能直接使用此类补丁模型。请先确认自己的 binary 能手动加载目标模型。

兼容层 8081 属于原机器另外部署的 Codex 服务，不在此通用 Dashboard 包中。这里提供原始 llama.cpp 8080 API；仅安装本包不等于已具备 Codex Responses 工具调用兼容。

## 环境要求

- Linux + systemd，Python 3.10 或更新版本；有 sudo 权限的现有普通用户。
- `systemctl`、`loginctl`、`runuser`、`pgrep` 可用。Fedora/Ubuntu 等常见发行版通常已经提供。
- 已安装可用的 GPU 驱动及兼容 llama-server，支持 `--metrics`、`--sleep-idle-seconds`、`--jinja` 和 `/props` 状态接口。
- 模型硬盘已挂载且开机可访问，模型完整分片都在指定根目录下。
- 固定使用 8080（模型）及 8090（Dashboard）。安装器不会自动更改防火墙或 BIOS。

本包不是 Windows 原生服务安装器，也没有将模型程序或 GPU 驱动装入容器。Windows 用户需要在 Linux 主机上安装，再通过浏览器访问。

## 安装：先检查，再部署

解压后进入本目录。把以下示例中的 `youruser` 和所有路径改成你自己的实际值。服务器程序和模型必须已存在。

```bash
sudo python3 install.py \
  --user youruser \
  --models-root /mnt/models-disk \
  --server /opt/llama.cpp/build/bin/llama-server \
  --model /mnt/models-disk/MyModel/model-00001-of-00003.gguf \
  --context 262144 \
  --dry-run
```

`--dry-run` 仅读取参数帮助、检查文件和参数，使用临时目录做校验；不写安装文件，不改变现有服务。出现“现有服务/文件/端口冲突”时，实际安装会拒绝覆盖。请自行确认已有模型是由哪个程序管理，先备份并处理旧部署，再安装。本安装器不会替你停掉 Hermes、其他模型服务或占用端口的进程。

检查通过后删除最后的 `--dry-run` 再执行，安装并启动服务。首次加载可能需数分钟，打开：

`http://你的Linux机器局域网IP:8090/`

默认参数：262144 上下文、1 Slot、4 CPU 线程、尽可能 GPU 卸载、30 分钟空闲休眠。**262K 是示例值，不是所有模型都支持的值；应按模型和内存修改。** 安装器不自动推断 RoPE、KV 精度、MTP 或图像投影搭配。

安装过程验证参数和文件后才写服务文件。遇到 systemd/权限等后续错误会停止并报告；已经写入的安装目录可能保留，请按报错检查，勿反复强制覆盖。安装文件清单见 `~/llama-dashboard/installation.json`。

### Qwen3.8 Flash Next + shared MTP 示例

```bash
sudo python3 install.py \
  --user youruser \
  --models-root /mnt/models-disk \
  --server /opt/llama.cpp/build-vulkan-mtp/bin/llama-server \
  --model /mnt/models-disk/Qwen3.8/UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf \
  --draft-model /mnt/models-disk/Qwen3.8/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --mmproj /mnt/models-disk/Qwen3.8/mmproj-BF16.gguf \
  --context 262144 \
  --extra-args '-ctk q8_0 -ctv q8_0 -b 4096 -ub 1024 --reasoning-budget 8192' \
  --dry-run
```

此例同样需替换路径并去掉 `--dry-run` 才安装。`--draft-model` 会附加 `draft-mtp` 和草稿长度 2。512K 需要自行评估内存、模型原始上下文与 YaRN 配置；不要为了照抄原部署而给不相关模型增加 RoPE 参数。

`--no-start` 可以仅安装文件，不启用开机启动、不启动模型和 Dashboard。实际安装需要操作系统管理员权限。若依赖自定义动态库，请先在系统中正确配置加载路径；本包不复制原机器的 LD_LIBRARY_PATH。

## 管理口令

每次首次安装生成独立随机口令，存放在所选用户的：

`~/.config/llama-dashboard/admin-token`

文件只有该用户可读写。登录对应用户后读取，填入网页管理口令框；浏览器不持久保存口令。修改该文件为单行新口令后运行 `systemctl --user restart llama-dashboard.service` 生效，不会重启模型。

只读监控和模型列表无口令；写入策略和模型加载需要口令。该服务面向可信局域网，未配置 HTTPS 或公网认证网关，请按自己的网络情况控制访问范围。

## 日常使用

1. 打开“模型与参数”，扫描指定根目录中的模型。只有首分片列入选择，缺失/无效分片被标记。
2. 在下拉框选择模型，完整启动参数会自动同步，无需口令，也不会加载模型。手动填写路径后点击“选择此模型并准备参数”。同家族量化切换保留页面中已修改的参数；跨家族会恢复目标配置或清除不兼容项，务必复核。检查文件、保存及加载时填写管理口令。
3. 修改常用参数或完整参数文本。模型名相同家族切换时尽量保留配置，切其他家族时去除 Qwen 专用设置；已成功用过的模型恢复其上次参数。
4. 点击“检查参数与模型文件”，查看最终参数和提醒。检查不运行推理，不能保证内存或架构兼容。
5. 暂停客户端的新请求，点击“保存并加载模型”。有处理/排队任务或其他模型管理冲突时拒绝切换。切换会清空 KV，短暂中断 API。
6. 观察任务状态；关闭网页后后台任务继续。新进程退出或 10 分钟未就绪会尝试恢复原模型。资源/磁盘故障也可能使恢复失败，页面会明确报告。

工具调用、图片输入、思考强度、长上下文质量取决于模型和聊天模板，不能仅由“加载成功”保证。模型重启后累计指标清零。异常重启使用 systemd，不会周期性发送推理请求；空闲休眠因此仍可工作。`/slots` 只在处理任务时读取，避免持续唤醒模型。llama.cpp 指标 20 秒采集、硬件 30 秒采集。

### 哪些参数可编辑

当前编译版的推理、采样、CPU/GPU、批量、KV、缓存、RoPE、草稿模型、图像投影和模板等参数可以在完整编辑器中使用，名称和参数数量由安装时该 binary 的 `--help` 动态获取。

为维持单模型管理、监控连接及现有权限边界，以下选项固定或禁用：模型 API 的 8080 端口/0.0.0.0 监听、metrics、API 认证/TLS和前缀、在线模型下载、多模型路由、server 内置主机工具、MCP 命令执行、切换 binary。它不是通用命令执行页面。

输入文件仅能在指定模型根目录或 `~/llama-dashboard-data` 中。日志和 Slot 导出等输出仅允许 `~/llama-dashboard-data`。此限制减少误写系统文件的可能，仍需保护管理口令。

## 服务与文件

| 内容 | 路径 / 服务 |
|---|---|
| 网页、后端、前端脚本 | 所选用户的 ~/llama-dashboard/ |
| 用户 Dashboard 服务 | ~/.config/systemd/user/llama-dashboard.service |
| 模型 root 用户服务 | /root/.config/systemd/user/llama-server.service |
| 实际模型参数覆盖 | /root/.config/systemd/user/llama-server.service.d/90-dashboard-policy.conf |
| 当前模型配置 | /etc/llama-dashboard/server.json |
| 各模型成功参数 | /etc/llama-dashboard/model-profiles.json |
| 最近加载任务 | /etc/llama-dashboard/model-job.json |
| 本地管理控制器 | /usr/local/libexec/llama_dashboard_control.py 和 llama_dashboard_models.py |
| 控制器系统服务 | /etc/systemd/system/llama-dashboard-control.service |
| 策略/模型切换备份 | 所选用户的 ~/backups/ |

模型服务沿用本项目的 root 用户 systemd 管理方式；Dashboard 以普通用户运行，通过本机 Unix socket 请求受限管理操作，不调用任意 shell。首次正常安装启用 root 和所选用户的 linger，使服务脱离登录会话运行。模型磁盘开机挂载由系统管理员负责。

以所选普通用户查看 Dashboard：

```bash
systemctl --user status llama-dashboard.service
journalctl --user -u llama-dashboard.service -n 50 --no-pager
```

以 root 查看模型与控制器：

```bash
sudo -i
env XDG_RUNTIME_DIR=/run/user/0 systemctl --user status llama-server.service
env XDG_RUNTIME_DIR=/run/user/0 systemctl --user show llama-server.service -p ExecStart
env XDG_RUNTIME_DIR=/run/user/0 journalctl --user -u llama-server.service -n 80 --no-pager
systemctl status llama-dashboard-control.service
```

`--no-start` 后要手动启用服务，先为 root 和所选普通用户启用 linger，启动对应 user@UID.service，再分别 daemon-reload、enable 和 start 对应服务。不要在模型启动失败时同时手动运行另一份 llama-server。

## 备份、升级、回退

每次模型切换在 `~/backups/model-switch-时间-ID/` 保存旧 server.json、覆盖文件和 result.json；策略修改保存到 dashboard-policy 目录。需要手工回退时先确认后台任务已结束，停止 root 模型服务，恢复配套的 server.json 和覆盖文件，然后 daemon-reload 并启动。root base unit 的 ExecStart 是占位符，真实启动参数在覆盖文件中，不要只恢复其中一个。

升级前备份整个安装目录、/etc/llama-dashboard、两个 /usr/local/libexec/llama_dashboard_*.py 文件、帮助快照和相关 unit。当前安装器专用于初次安装，明确拒绝覆盖旧部署；没有自动原地升级选项。卸载时先停用相应服务，再依据 installation.json 检查哪些文件属于本包，保留模型权重、备份和口令副本；本包不提供自动删除脚本。

更换 binary 后需重新生成 /usr/local/libexec/llama-server-help.txt，核对支持的参数并测试，旧编辑器帮助不会自动跟随 binary 变化。Fedora 的 /etc/systemd/system 和 /usr/local/libexec 文件复制后需要正确 SELinux 标签，可用 restorecon 修复。

不声称已验证加载过程中断电、重启系统或杀死临时任务的自动恢复；若状态残留加载中，先检查 `llama-dashboard-apply-<任务ID>.service` 和 root 模型服务，按备份人工恢复。不要在后台任务仍工作时手工改配置。

## 已做的验证

原测试主机为 Fedora 44、Ryzen AI Max+ 395、128 GB 统一内存：

- 23 项监控/控制/模型管理测试通过。
- 桌面和手机页面无横向溢出、无 JavaScript 错误；模型扫描、参数输入同步、帮助搜索通过。
- 无效 KV 格式导致启动失败后，Q2 自动恢复。
- Q2 → IQ4 成功；IQ4 经兼容入口返回测试内容。
- IQ4 → Llama 3.2 1B 成功，完成普通聊天响应；再恢复 Q2 + 512K + MTP。
- Q4 已扫描并检查完整分片，未在本轮进行实际加载/性能验证。

新安装器完成语法、路径模板及原 Linux 主机的 dry-run 检查；尚未在朋友的干净机器上做完整安装或整机重启测试。不同 binary、驱动、模型和磁盘挂载方式仍需首次部署验证。

### 2026.09.16.1 修复

修复模型下拉选择与完整参数不同步、准备按钮空口令无反应的问题；准备结果在选择框旁显示，连续选择以最后一次为准，准备期间暂停参数编辑以避免覆盖。

### 2026.09.16.2 修复

任务结束不再清空 Slot 明细，保留最近采样用量和时间并标注历史快照；Dashboard 重启后仍保留，server 重启清零。空闲和休眠时仍不查询 Slot，避免干扰休眠。快照不是最终任务统计或当前 KV 驻留量，短任务可能被 20 秒采样漏掉。
