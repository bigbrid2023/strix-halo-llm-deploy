# Dashboard 功率限制 + 调用来源 + llama.cpp 9-24 重编译（2026-09-25）

> **日期**：2026-09-25
> **作者**：Kiki
> **范围**：本机（AMD Ryzen AI MAX+ 395 / Fedora 44）的 `llama-server` + 自研 Dashboard 更新
> **对应 commit**：本仓库 main HEAD 之后

## 摘要

在 2026-09-24 ~ 09-25 之间，本仓库的运行机做了三件事：

1. **Dashboard 升级**：把 `llama_dashboard.py` / `llama_dashboard.html` 改成新版，接入 **功率限制 UI** + **调用来源显示** + **模型来源显示**。
2. **功率实测**：用 `ryzenadj` 在 4 档（140/120W original、100W、90W、70W）做了完整的吞吐 + 温度测试，确认 **90W 是日常使用的甜点档**（温度 ≤ 75℃，吞吐 = 性能档的 96%）。
3. **llama.cpp 重编译**：基于上游 `6b790a9` (2026-09-24) 的 vulkan 修复重新跑了一次 build，验证 vulkan 后端仍然能加载 Qwen3.8-Flash-Next + shared MTP draft。生产 build 仍是 `build-vulkan-mtp/`（2026-09-08 编译、MTP PR #28243 已 merge）。

---

## 1. Dashboard 新版变更点

旧版（`llama-dashboard-manager/install.py` v2026.09.16.2）只读 metrics + 切换模型。新版（2026-09-25）新增三块 UI + 三组 API：

| 新增模块 | UI 位置 | 后端数据来源 | API 端点 |
|---|---|---|---|
| **功率限制** | 顶部 "功率切换" 区块 | `ryzenadj --info` 直读 SMU + `/run/llama-dashboard-control/control.sock` 写回 | `GET /api/power`、`POST /api/control` (action=`set_power`) |
| **调用来源** | 顶部 "调用来源" 表格 | `/run/llama-dashboard-callers/state.json`（uvicorn/反代写入的最近 5 分钟连接快照） | `GET /api/state` 内嵌 |
| **模型来源** | 服务管理区块 | `model-manager.js` 从 GGUF `general.architecture` + 文件路径推断 | `POST /api/control` (action=`preview_model`/`validate_model`) |

配套前端脚本：

- `llama_dashboard.html`：单文件 SPA 模板，所有区块都在这一份 HTML 里
- `power-manager.js`：每 15 秒轮询 `/api/power`，表单提交时调 `/api/control`，带 `X-Dashboard-Request: 1` 头验证
- `model-manager.js`：模型扫描/校验 UI，按文件路径自动猜测 `draft-model` / `mmproj`

部署后访问 `http://<本机 IP>:8090/` 即可看到全部新区块。功率 UI 需要 admin token（路径 `<user>/.config/llama-dashboard/admin-token`，与原模型管理共用；`<user>` 替换为运行 Dashboard 的本地用户名）。

> **完整源码见本仓库 `docs/assets/`**：`llama_dashboard.py` (22KB) + `llama_dashboard.html` (33KB) + `power-manager.js` (2.8KB) + `model-manager.js` (12.6KB)。

### 1.1 功率切换 UX 关键点

- 三个 profile（`90` / `100` / `140`），单位瓦特。**`140` 即原厂档（`fast-limit=140 / slow-limit=120`）**，`70` / `100` 留给手动选择，UI 默认选中当前生效档。
- 每次切换后立刻调 `ryzenadj --info` 读回实际值，**读不到 0.15W 偏差以内则视为失败**，前端会显示 "未能确认切换完成" + 错误码。
- `slow-limit` 受 `stapm-limit=176` 兜底；不会突破 STAPM 上限。
- 切档时 UI 不会禁用 llama-server —— 后台 service 自带 `ExecStartPre` 等待 model 文件，可以安全切档。

---

## 2. 功率测试结果（2026-09-25 实测）

### 2.1 测试方法

- 工具：`ryzenadj` v0.19.0 + 自研 `bench.py` (本地跑的 2500 个 token prompt，tokenize 后长度 512 / 2048 / 8192 / 32768)
- 主机：AMD Ryzen AI MAX+ 395 / 128GB UMA / Fedora 44 / Kernel 7.1.13
- 模型：Qwen3.8-Flash-Next UD-Q2_K_XL + shared MTP (Q8_0 draft) + mmproj-BF16
- 启动参数：见 [README §5](./README.md#5-启动参数) 生产配置
- 每档 `n_repetition=2`，prompt 长度横跨 2K/8K/32K

### 2.2 关键结论

| 档位（fast/slow W） | 2K 吞吐 | 8K 吞吐 | 32K 吞吐 | 32K 温度峰值 | 推荐场景 |
|---|---|---|---|---|---|
| **140 / 120**（原厂） | 56.2 tok/s | 53.4 tok/s | 46.6 tok/s | **92 ℃** ⚠️ | benchmark / 临时跑完 |
| **100 / 100** | 54.4 tok/s (-3%) | 52.2 tok/s (-2%) | 45.7 tok/s (-2%) | 82 ℃ | 短时高负载 |
| **90 / 100** | 53.9 tok/s (-4%) | 51.5 tok/s (-4%) | 45.1 tok/s (-3%) | 76 ℃ | **日常生产 ⭐** |
| **70 / 70** | 51.2 tok/s (-9%) | 49.2 tok/s (-8%) | 42.8 tok/s (-8%) | 69 ℃ | 静音 / 散热受限 |

> **结论**：**90W 是日常甜点**。温度从 92℃ 降到 76℃（更安全）、吞吐只损失 3-4%。原厂 140W 跑 32K context 时 GPU 温度逼近 junction limit，**不建议长时间跑**。

实测命令（本机上）：

```bash
cd ~/power-bench/20260925 && python3 run.py --label 90
# 完成后自动 restore 回 original limits（176/140/120）
```

---

## 3. llama.cpp 上游同步（2026-09-24）

### 3.1 做了什么

- 在 `~/llama-candidates/20260924-vulkan-mtp/` clone 上游 [ggml-org/llama.cpp](https://github.com/ggml-org/llama.cpp) 到 `6b790a9` (2026-09-24 14:08 +0200)
- 跑 `cmake -B build-vulkan-mtp-0924 -DGGML_VULKAN=ON -DGGML_HIPBLAS=OFF ...`，确认 vulkan 后端可编译
- 用 `--model Qwen3.8-Flash-Next-UD-Q2_K_XL + --spec-type draft-mtp --mmproj mmproj-BF16.gguf` 跑 smoke test（10 个 prompt），全部成功
- 工作树有未提交的 diff（主要是 Qwen4-Exp / conversion 类的实验性补丁），**未应用到生产 build**

### 3.2 生产 build 仍是 9-8 那次

```
~/llama.cpp/build-vulkan-mtp/bin/llama-server   # 9-8 编译，含 PR #28243 MTP patch
~/llama.cpp/build-rocm-mtp/bin/llama-server     # 9-8 编译，ROCm 后端备用
~/llama.cpp/build-vulkan-bak-no-mtp/bin/llama-server  # 9-8 编译，无 MTP 的基线
```

> **为什么不切 9-24 新 build**：上游 6b790a9 主要是 vulkan conv_2d/conv_3d 的对齐修复，与 Qwen3.8 + MTP 完全无关；且我们的生产 binary 已经验证能稳跑 32K context + MTP。换 binary 风险大于收益。

### 3.3 未来动作

- 等上游 merge 我们需要的 patch（如 `--image-min-tokens` 上游对齐、Qwen4-Exp 模型支持稳定）再统一 rebase。
- `~/llama-candidates/` 目录保留为候选分支池，每次上游有新 commit 就跑一次 smoke test。

---

## 4. 部署参考（how to reproduce）

### 4.1 Dashboard 部署

```bash
# 本机上
sudo cp docs/assets/llama_dashboard.py  /opt/llama-dashboard/
sudo cp docs/assets/llama_dashboard.html /opt/llama-dashboard/
sudo cp docs/assets/power-manager.js     /opt/llama-dashboard/
sudo cp docs/assets/model-manager.js     /opt/llama-dashboard/
sudo systemctl restart llama-dashboard.service
```

Dashboard 在 8090 端口；llama-server 在 8080；两者通过 unix socket `/run/llama-dashboard-control/control.sock` 通信（systemd `RuntimeDirectory=llama-dashboard-control`）。

### 4.2 功率切换

```bash
# 切到 90W 甜点档
sudo ryzenadj --stapm-limit=176000 --fast-limit=90000 --slow-limit=90000
# 验证
ryzenadj --info | grep -E "STAPM|PPT LIMIT"
```

或在 Dashboard UI 里选 `90` profile + 输 admin token + 应用。

### 4.3 llama.cpp smoke test（升级前必跑）

```bash
cd ~/llama-candidates/<新候选分支>
cmake -B build -DGGML_VULKAN=ON -DGGML_HIPBLAS=OFF -DCMAKE_BUILD_TYPE=Release
cmake --build build -j8 --target llama-server
./build/bin/llama-server \
  --model ~/models/Qwen3.8/UD-Q2_K_XL/Qwen3.8-Flash-Next-UD-Q2_K_XL-00001-of-00003.gguf \
  --draft-model ~/models/Qwen3.8/MTP/mtp-Qwen3.8-Flash-Next-shared-Q8_0.gguf \
  --spec-type draft-mtp --spec-draft-n-max 2 --mmproj ~/models/Qwen3.8/mmproj-BF16.gguf \
  --no-webui -ngl 999 -c 524288 --host 127.0.0.1 --port 18080
# 然后 curl 试 1 个 chat completion + 1 个 vision 请求
```

---

## 5. 踩坑

### 坑 1：Dashboard 轮询导致 metrics 字段缺失

- **症状**：`callers` 表格报 "调用来源监测暂不可用"
- **原因**：本机用 nginx 反代 8090 时，默认 `proxy_buffering on` 会缓存 SSE
- **解决**：在 nginx 站点配置加 `proxy_buffering off; proxy_cache off;` for `/api/state` 与 `/api/callers`

### 坑 2：ryzenadj 切档后 readback 偏差 0.5W

- **症状**：UI 报 "切换失败"
- **原因**：SMU 内部 `slow-limit` 是慢热生效，0.5 秒内 readback 不准
- **解决**：`run.py` 已经 `time.sleep(0.5)` 后再校验；UI 上手动切档建议等 2 秒再看

### 坑 3：功率测试中 32K context 撞 junction

- **症状**：原厂档（140W）跑 32K prefill 时 GPU 温度 92℃
- **原因**：Strix Halo junction limit 100℃，长时 92℃ 留 8℃ 余量
- **解决**：默认切到 90W，余量 ~24℃

---

## License / 免责

本文档与配套代码仅供学习参考。功率档位是基于本机（AMD Ryzen AI MAX+ 395 / Fedora 44 / 默认散热）的实测；其他机器（笔记本 OEM 散热、商户机水冷 R1）建议自行重跑 `bench.py`。