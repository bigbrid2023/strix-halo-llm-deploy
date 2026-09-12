# Qwen3.8 Flash Next (UD-IQ4_XS + MTP + Vision) 在 AMD Strix Halo APU 上的部署指南

> **硬件**：AMD Ryzen AI MAX+ 395 (Strix Halo APU, 128GB 统一内存)
> **系统**：Fedora Linux 44 (Kernel 7.1.13)
> **部署时间**：2026-09-08
> **最后更新**：2026-09-12

---

## 目录

- [1. 硬件规格](#1-硬件规格)
- [2. 系统配置](#2-系统配置)
- [3. 模型文件](#3-模型文件)
- [4. llama.cpp 编译](#4-llamacpp-编译)
- [5. 启动参数](#5-启动参数)
- [6. systemd 服务配置](#6-systemd-服务配置)
- [7. 性能测试数据](#7-性能测试数据)
- [8. API 调用示例](#8-api-调用示例)
- [9. 踩坑记录](#9-踩坑记录)

---

## 1. 硬件规格

| 项目 | 规格 |
|------|------|
| **CPU** | AMD Ryzen AI MAX+ 395 (16C/32T) |
| **iGPU** | Radeon 8060S (gfx1151, Strix Halo) |
| **统一内存** | 128GB (实际可用 ~97GB 给 GPU) |
| **操作系统** | Fedora Linux 44 (Kernel 7.1.13-200.fc44.x86_64) |
| **Vulkan** | RADV (开源 AMD Vulkan 驱动) |
| **DPM 档位** | high (性能模式) |

> ⚠️ Strix Halo 是 APU（CPU+GPU 统一内存架构），不是独立显卡，所有 GPU 计算共享统一内存池。

---

## 2. 系统配置

### 2.1 Kernel cmdline 参数

编辑 GRUB 使其生效（`/etc/default/grub` 的 `GRUB_CMDLINE_LINUX`）：

```
amdgpu.gttsize=126976 ttm.pages_limit=32505856
```

| 参数 | 说明 |
|------|------|
| `amdgpu.gttsize=126976` | 分配 126GB GTT 窗口（给 Vulkan RADV 使用） |
| `ttm.pages_limit=32505856` | TTM 内存管理器上限（让 Vulkan 能使用大内存池） |

> **关键**：Strix Halo APU 统一内存在 Linux 下默认不会全部暴露给 GPU，需要这两个参数才能让 Vulkan RADV 使用完整 ~97GB。

**验证生效**：
```bash
cat /proc/cmdline
# 应该看到 amdgpu.gttsize=126976 ttm.pages_limit=32505856
```

### 2.2 GPU 性能档位

设为 `high` 确保 GPU 不降频：
```bash
echo high > /sys/class/drm/card*/device/power_dpm_force_performance_level
```

### 2.3 Vulkan 驱动

```bash
# 验证 Vulkan 可用
vulkaninfo --summary

# 推荐使用 RADV（开源驱动）
ls /dev/dri/
# card1  ← Strix Halo iGPU
# renderD128
```

---

## 3. 模型文件

模型存放在外挂 NTFS 盘（需挂载到 `/path/to/models/`）：

```
/path/to/models/Qwen3.8-Flash-Next-GGUF/
├── UD-IQ4_XS/                          # 主模型 (UD-IQ4_XS 量化, 91GB)
│   ├── Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf  (meta)
│   ├── Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf  (47GB)
│   └── Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf  (41GB)
├── MTP/                                 # MTP 侧模型 (推测解码加速)
│   └── mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf            (2.6GB)
└── mmproj-BF16.gguf                     # 视觉投影器 (866MB)
```

### 下载链接

```bash
# HF Repo: https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF

# 主模型 UD-IQ4_XS (分片下载)
aria2c -x8 -s8 
  "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/main/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00002-of-00003.gguf"
aria2c -x8 -s8 
  "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/main/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00003-of-00003.gguf"

# MTP 侧模型
aria2c -x8 -s8 
  "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/main/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf"

# 视觉 mmproj (FP16)
# (如需代理：加 --all-proxy="http://your-proxy:port")
aria2c -x8 -s8 
  "https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF/resolve/main/mmproj/mmproj-BF16.gguf"
```

---

## 4. llama.cpp 编译

### 4.1 关键：必须打 PR #28243

> ⚠️ **主分支 llama.cpp 不支持 MTP（qwen4exp 架构），必须打 [PR #28243](https://github.com/ggml-org/llama.cpp/pull/28243) patch**

```bash
cd /path/to/llama.cpp
git clone https://github.com/ggml-org/llama.cpp.git
cd llama.cpp

# 下载并应用 MTP patch
curl -L "https://github.com/ggml-org/llama.cpp/pull/28243.diff" -o /tmp/pr-28243.diff
git apply /tmp/pr-28243.diff
```

### 4.2 Vulkan 编译（生产用）

```bash
cmake -B build-vulkan-mtp -S . \
  -DCMAKE_BUILD_TYPE=Release \
  -DGGML_VULKAN=ON \
  -DGGML_NATIVE=OFF \
  -DGGML_OPENMP=ON \
  -DGGML_LTO=ON \
  -DBUILD_SHARED_LIBS=OFF

cmake --build build-vulkan-mtp --config Release --target llama-server -j8
```

输出：`/path/to/llama.cpp/build-vulkan-mtp/bin/llama-server`（72MB 静态二进制）

### 4.3 版本信息

```
commit: 5d806aa
tag: b10859-5-g5d806aa
binary: 72MB (Release 静态链接)
```

---

## 5. 启动参数

```bash
/path/to/llama.cpp/build-vulkan-mtp/bin/llama-server \
  -m /path/to/models/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \
  -md /path/to/models/Qwen3.8-Flash-Next-GGUF/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --spec-type draft-mtp \
  --spec-draft-n-max 2 \
  -ngl 999 \
  -fa on \
  -ctk q8_0 \
  -ctv q8_0 \
  -c 262144 \
  -b 4096 \
  -ub 1024 \
  -t 4 \
  --parallel 1 \
  --jinja \
  --no-webui \
  --host 0.0.0.0 \
  --port 8080 \
  --metrics \
  --mmproj /path/to/models/Qwen3.8-Flash-Next-GGUF/mmproj-BF16.gguf \
  --image-min-tokens 1024
```

### 关键参数说明

| 参数 | 值 | 说明 |
|------|-----|------|
| `-m` | 主模型路径 | UD-IQ4_XS 分片第一片（meta） |
| `-md` | MTP draft 模型 | 推测解码加速 |
| `--spec-type draft-mtp` | | 启用 MTP 推测解码 |
| `--spec-draft-n-max 2` | 2 | draft max 设为 2（最优） |
| `-ngl 999` | 999 | 全部层加载到 GPU |
| `-fa on` | | 内存对齐优化 |
| `-ctk q8_0 -ctv q8_0` | | KV cache 8-bit 量化（让 256K ctx 不 OOM） |
| `-c 262144` | 256K | 最大上下文长度 |
| `-b 4096 -ub 1024` | | batch size |
| `-t 4` | 4 | 线程数 |
| `--mmproj` | | **视觉投影器路径（启用多模态）** |
| `--image-min-tokens 1024` | 1024 | 视觉 token 最小压缩 |

---

## 6. systemd 服务配置

### 6.1 服务文件

`~/.config/systemd/user/llama-server.service`：

```ini
[Unit]
Description=Qwen3.8 Flash Next llama-server (UD-IQ4_XS + MTP draft, Vulkan, 256K ctx)
Documentation=https://github.com/ggml-org/llama.cpp/pull/28243
After=network.target

[Service]
Type=simple
WorkingDirectory=/path/to/models

ExecStartPre=/bin/sh -c 'for i in 1 2 3 4 5 6 7 8 9 10; do
  [ -f /path/to/models/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf ] && exit 0
  sleep 3
done
echo "ERROR: model file not found after 30s"
exit 1'

ExecStart=/path/to/llama.cpp/build-vulkan-mtp/bin/llama-server \
  -m /path/to/models/Qwen3.8-Flash-Next-GGUF/UD-IQ4_XS/Qwen3.8-Flash-Next-UD-IQ4_XS-00001-of-00003.gguf \
  -md /path/to/models/Qwen3.8-Flash-Next-GGUF/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --spec-type draft-mtp --spec-draft-n-max 2 \
  -ngl 999 -fa on -ctk q8_0 -ctv q8_0 \
  -c 262144 -b 4096 -ub 1024 -t 4 \
  --parallel 1 --jinja --no-webui \
  --host 0.0.0.0 --port 8080 --metrics \
  --mmproj /path/to/models/Qwen3.8-Flash-Next-GGUF/mmproj-BF16.gguf \
  --image-min-tokens 1024

Restart=always
RestartSec=5
StartLimitIntervalSec=300
StartLimitBurst=10
MemoryMax=120G
TimeoutStopSec=30
KillSignal=SIGTERM
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
```

### 6.2 GPU 性能档位 drop-in

`~/.config/systemd/user/llama-server.service.d/power.conf`：

```ini
[Service]
ExecStartPost=/bin/sh -c 'echo high > /sys/class/drm/card*/device/power_dpm_force_performance_level'
```

### 6.3 启用服务

```bash
# 允许用户linger（后台服务持久化）
sudo loginctl enable-linger $USER

# 重载并启用
systemctl --user daemon-reload
systemctl --user enable --now llama-server

# 检查状态
systemctl --user status llama-server
curl http://localhost:8080/health
```

---

## 7. 性能测试数据

### 7.1 服务状态

```json
{"status":"ok"}
```

- **端口**：8080
- **API**：OpenAI 兼容 (`http://localhost:8080/v1`)
- **Capabilities**：`completion`, `multimodal`

### 7.2 短对话生成速度

| 指标 | 数值 |
|------|------|
| **生成速度** | 33.38 tok/s |
| **Prefill 速度** | 14.30 tok/s |
| **Draft acceptance** | 54.9% (156/284) |
| **测试条件** | 20 tokens prompt → 300 tokens 生成 |

### 7.3 长期运行数据（metrics 累计）

| 指标 | 累计值 |
|------|--------|
| Prompt tokens processed | 447,119 |
| Prompt tokens cached | 2,211,060 |
| Prompt time | 1,806 秒 |
| Tokens predicted | 35,336 |
| Generation time | 1,240 秒 |
| Avg generation speed | ~28 tok/s |
| Max sequence length | 91,081 tokens |

### 7.4 256K 上下文长期运行数据

> 用户实测：262K ctx 持续对话，context 稳定在 ~182K，运行 5 轮 97 步

| 指标 | 数值 |
|------|------|
| **上下文总量** | 262K |
| **上下文已用** | ~182K（70%） |
| **系统提示词** | ~1.6K |
| **工具** | ~6.5K |
| **对话消息** | ~101K |
| **对话轮数** | 5 轮 |
| **总步数** | 97 步 |
| **LLM 累计耗时** | 63m54s |
| **工具调用耗时** | 19m34s |
| **首 token 平均延迟** | 10.1s |
| **生成速度** | **25 tok/s** |
| **缓存命中** | **99%** |
| **累计输入 token** | 6.3M tokens |
| **KV cache** | Q8_0 量化，支撑 256K 无 OOM |
| **Draft acceptance** | 54-69% |

### 7.5 Vulkan vs ROCm 对比（131K ctx）

| 指标 | Vulkan+MTP | ROCm+MTP | 结论 |
|------|------------|----------|------|
| 生成速度（server 内部） | **32.79 tok/s** | 28.27 tok/s | Vulkan +16% |
| Prompt 处理 | 6.77 tok/s | **40.50 tok/s** | ROCm +498% |
| **256K ctx 支持** | ✅ ~31 tok/s | ❌ OOM | Vulkan 唯一可行 |
| 可用统一内存 | **97.5 GB** | 65 GB | Vulkan 多 50% |
| 冷启动时间 | **6-7 秒** | 24-30 秒 | Vulkan 4x 快 |

> **结论**：Strix Halo APU 上 **Vulkan 是唯一选择**，ROCm XNACK disabled 只能使用 65GB 窗口，跑不了 256K ctx。

---

## 8. API 调用示例

### 8.1 文字对话

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [{"role": "user", "content": "用一句话自我介绍"}],
    "max_tokens": 100
  }'
```

### 8.2 视觉识别（多模态）

```bash
curl http://localhost:8080/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "qwen",
    "messages": [
      {
        "role": "user",
        "content": [
          {"type": "text", "text": "这张图片里有什么？"},
          {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,/9j/4AAQSkZJRg..."}
        ]
      }
    ],
    "max_tokens": 200
  }'
```

### 8.3 Python 调用

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8080/v1",
    api_key="not-needed"
)

# 文字
resp = client.chat.completions.create(
    model="qwen",
    messages=[{"role": "user", "content": "你好"}],
    max_tokens=100
)
print(resp.choices[0].message.content)

# 视觉（base64图片）
import base64
with open("image.jpg", "rb") as f:
    img_b64 = base64.b64encode(f.read()).decode()

resp = client.chat.completions.create(
    model="qwen",
    messages=[{
        "role": "user",
        "content": [
            {"type": "text", "text": "描述这张图片"},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
        ]
    }],
    max_tokens=200
)
```

---

## 9. 踩坑记录

### 坑 1：主分支 llama.cpp 不支持 MTP

- **症状**：启动报错，不识别 `--spec-type draft-mtp`
- **解决**：必须打 [PR #28243](https://github.com/ggml-org/llama.cpp/pull/28243)

### 坑 2：ROCm 在 Strix Halo 上只能看 65GB

- **症状**：Vulkan 可以用 ~97GB，ROCm 只能划 65GB，256K ctx OOM
- **原因**：ROCm 7.1.1 在 APU 上 XNACK disabled
- **解决**：用 Vulkan RADV

### 坑 3：IOMMU 无法关闭

- **症状**：设置 `amd_iommu=off` 后 `dmesg` 显示仍是 `Default domain type: Translated`
- **原因**：BIOS/AGESA 强制开启 IOMMU
- **影响**：NPU (XDNA) 报错但 GPU 推理正常

### 坑 4：NTFS 开机挂载 race condition

- **症状**：llama-server 开机启动时 NTFS 盘还没挂载
- **解决**：ExecStartPre 循环等待模型文件出现，最多 30 秒

### 坑 5：视觉模型 capabilities 未声明

- **症状**：API 显示 `capabilities: ["completion"]` 没有 `multimodal`
- **原因**：注册模型时未传递 `--mmproj` 参数
- **解决**：加上 `--mmproj` 和 `--image-min-tokens` 后 API 返回 `capabilities: ["completion","multimodal"]`

### 坑 6：Vulkan 驱动找不到设备

- **症状**：`vulkaninfo` 显示 `terminator_CreateInstance: Received return code -9`
- **原因**：`libvulkan_dzn.so` (Intel) 被优先加载
- **解决**：设置 `RADV_PERFTUNE=1` 或确保 `amdgpu` 驱动加载

---

## License / 免责

本项目仅供学习参考。模型权重来自 [Unsloth/Qwen3.8-Flash-Next-GGUF](https://huggingface.co/unsloth/Qwen3.8-Flash-Next-GGUF)，请遵守相应许可证。
