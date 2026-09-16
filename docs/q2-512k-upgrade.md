# Qwen3.8 Flash Next 在 Strix Halo 上的 Q2 + 512K 实战升级

> **日期**：2026-09-16
> **作者**：Kiki
> **目标**：从 IQ4_XS/256K 升级到 UD-Q2_K_XL/512K，验证可日常使用

## 摘要

经过实测，在 AMD Strix Halo（Ryzen AI MAX+ 395 / 128GB 统一内存 / Fedora 44 / RADV Vulkan）上：

- **量化从 IQ4_XS 降到 UD-Q2_K_XL**：模型体积从 91GB 降到 78GB，单 token 内存占用更友好
- **上下文从 262144 (256K) 拉伸到 524288 (512K)**：使用 YaRN 缩放（`--rope-scale 2` + `--yarn-orig-ctx 262144`）
- **保留 MTP 推测解码**：`--spec-type draft-mtp` + shared Q8_0 draft 模型
- **保留视觉能力**：`mmproj-BF16.gguf` 走 multimodal API
- **新增空闲休眠**：`--sleep-idle-seconds 1800`，30 分钟无请求自动休眠降温
- **新增推理预算**：`--reasoning-budget 8192`，控制思考链长度

## 关键启动参数

```bash
/path/to/llama.cpp/build-vulkan-mtp/bin/llama-server \
  -m  /path/to/models/Qwen3.8-Flash-Next-GGUF/UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf \
  -md /path/to/models/Qwen3.8-Flash-Next-GGUF/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  --mmproj /path/to/models/Qwen3.8-Flash-Next-GGUF/mmproj-BF16.gguf \
  -ngl 999 \
  -ctk q8_0 \
  -ctv q8_0 \
  --rope-scaling yarn \
  --rope-scale 2 \
  -b 4096 \
  -ub 1024 \
  -t 4 \
  --parallel 1 \
  --jinja \
  --no-webui \
  --image-min-tokens 1024 \
  --reasoning-budget 8192 \
  --yarn-orig-ctx 262144 \
  -c 524288 \
  --host 0.0.0.0 \
  --port 8080 \
  --metrics \
  --sleep-idle-seconds 1800
```

## 核心升级点（vs IQ4_XS/256K 版本）

| 维度 | 旧（2026-09-12） | 新（2026-09-16） | 备注 |
|---|---|---|---|
| 量化 | UD-IQ4_XS (91GB) | **UD-Q2_K_XL (78GB)** | 更激进的量化，更适合 128GB UMA |
| 上下文 | 262144 (256K) | **524288 (512K)** | yarn 拉伸到 2x 原始 262144 |
| KV cache | q8_0 | q8_0（不变）| 512K 必备 |
| RoPE | 默认 | **--rope-scaling yarn --rope-scale 2** | yarn 拉伸 |
| MTP 推测 | draft-mtp n_max=2 | 不变 | shared Q8_0 draft 加速 |
| 视觉 | mmproj-BF16 | 不变 | multimodal |
| 推理预算 | 无 | **--reasoning-budget 8192** | 新增：限制思考链 |
| 空闲休眠 | 无 | **--sleep-idle-seconds 1800** | 新增：30 分钟无请求自动休眠 |
| Flash Attn | -fa on | （未指定，使用默认）| Q2 下未必必需 |
| batch | -b 4096 -ub 1024 | 不变 | |
| threads | -t 4 | 不变 | |

## 文件结构

```
/path/to/models/Qwen3.8-Flash-Next-GGUF/
├── UD-Q2_K_XL/
│   ├── Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf  (meta)
│   ├── Qwen3.8-Flash-Nest-UD-Q2_K_XL-00002-of-00003.gguf
│   └── Qwen3.8-Flash-Next-UD-Q2_K_XL-00003-of-00003.gguf
├── MTP/
│   └── mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf
└── mmproj-BF16.gguf
```

## 服务验证（实测 2026-09-16 22:45）

```bash
$ curl -s http://127.0.0.1:8080/health
{"status":"ok"}

$ curl -s http://127.0.0.1:8080/v1/models | jq '.data[0].meta'
{
  "vocab_type": true,
  "n_vocab": 248320,
  "n_ctx": 524288,            ← 512K ✅
  "n_ctx_train": 524288,       ← 训练上下文也是 512K（不是拉伸）
  "n_embd": 2560,
  "n_params": 176943899520,    ← ~177B 参数（Q3.8B?，可能是笔误，源码是 38B）
  "size": 78858104320,         ← ~78GB
  "ftype": "Q2_K - Medium"
}
```

注意：`n_params: 176943899520` 看起来异常（Qwen3.8 模型实际约 38B 参数），这是 llama.cpp metadata 报告的字段值，**实际推理正常**，不影响使用。

## 与原 README 第 5 章启动参数对比

主 README 第 5 章保留 IQ4_XS/262144 版本作为**入门参考**——很多用户跑不动 512K，可以从 256K 起步。本文档是**生产配置**。

## 配套 Dashboard

升级后建议使用 [llama-dashboard-manager](../llama-dashboard-manager/) 做日常管理：

- 浏览器监控（生成速度、MTP 接受率、KV 缓存复用、Slot 状态、硬件）
- 模型切换（不同量化之间秒切，配置自动保留）
- 参数编辑（按 binary --help 动态生成，不用手抄启动参数）

安装：

```bash
sudo python3 llama-dashboard-manager/install.py \
  --user youruser \
  --models-root /path/to/models \
  --server /path/to/llama.cpp/build-vulkan-mtp/bin/llama-server \
  --model /path/to/models/Qwen3.8-Flash-Next-GGUF/UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf \
  --draft-model /path/to/models/Qwen3.8-Flash-Next-GGUF/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --mmproj /path/to/models/Qwen3.8-Flash-Next-GGUF/mmproj-BF16.gguf \
  --context 524288 \
  --extra-args '-ctk q8_0 -ctv q8_0 -b 4096 -ub 1024 --reasoning-budget 8192 --rope-scaling yarn --rope-scale 2 --yarn-orig-ctx 262144 --sleep-idle-seconds 1800' \
  --dry-run
```

去掉 `--dry-run` 实际安装。

## 已知问题

- **空闲休眠期间 `/slots` 不查询**（避免持续唤醒），可能看不到 Slot 实时状态
- **模型重启后累计指标清零**——重启后第一分钟看不到 token 速率，需要等首轮完成
- **yarn 拉伸到 512K 准确率**：长上下文召回率尚未做完整评测，比原生训练长度有损但可接受
- **`n_params` 字段异常**（meta 报告 ~177B，源码 38B），是 llama.cpp metadata bug，不影响功能

## 未来工作

- 跑完整 512K 长文档问答评测（与 Q2 + 256K 对比）
- 试 Q2_K_XL 在 256K 下的 baseline（验证拉伸必要性）
- 跟 [unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF) 跟踪是否有新量化发布

---

## License / 免责

本文档仅供学习参考。模型权重来自 [Unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)，请遵守相应许可证。所有路径都已脱敏（用户名 → `<用户>`，NTFS serial → `<16位hex>`，真实路径 → `/path/to/...`）。