# 蓝研 AIDD：四模型 Compare 工作台

Compare 是独立研究对照，**不是四模型投票、分数平均或准确率排名**。正式 v2 的四端点 API 与工件不变；全部 v3 注册项仍为 `formalAvailability=false`。分支 `feat/aidd-four-model-compare` 基于 main `3e0cbe432360b5c9b22aa0c3d538c5b0e23e8a51`，不会自动合并或部署。

## 架构与实际能力

| 注册 ID | 作用 / 真实实现 | 本轮真实执行情况 |
|---|---|---|
| `toxnet-v3-b16` | 已有 `MultitaskModelClient.predict_all`，固定 MoLFormer + B16 权重，独立 PyTorch 子进程、工件 SHA256 | 真实 RTX 4060 CUDA HTTP 对照已通过；16 任务，等价输入一致 |
| `txgemma-2b-predict` | Google 固定版本内置 Gemma2，经过审核的 TDC 提示，离线核验、独立子进程 | 适配器/门禁已测试；未接受许可、未下载权重、没有真实预测 |
| `ollama-qwen-local` | 本机 `/api/tags` → `/api/chat` → digest 复核 → `/api/ps` 卸载确认 | 实际服务不可达；没有生成真实 Qwen 报告；不能推断磁盘是否存有模型 |
| `deepseek-api` | 现有服务端账户配置，独立 Chat Completions 报告适配器 | 未执行收费 API；配置完整不等于型号或账户权限已验证 |

`compare_registry.py` 只描述能力；状态由实际检查给出。已有千问 API 供应商仍是远程 API，与 Ollama 独立。DeepSeek Compare 不调用旧 `ModelClient.predict`，不生成可信毒性分数。

数据流程：RDKit 验证、canonical 结构与 `structureId` → B16 16 任务 → 可选本机证据/同权重归因 → TxGemma 审核任务 → 不可变事实快照 → 分别独立生成 Qwen / 已授权 DeepSeek 报告 → 确定性核验 → 对照工作台 / JSON 导出。

每个模型保留独立状态和错误。一个模型失败不会清除已经通过核验的输出。没有 B16 结果时只产生“证据不足”事实，不生成替代毒性分数。报告不接收前一报告的内容。

## 可比性与事实验证

- B16 的四端点保持 hERG / DILI / Ames / ClinTox 来源标签；十二 Tox21 通路是独立 assay 活性，没有临床综合概率。
- TxGemma 仅 `tdc-bbb-martins` 的官方模板及 A/B 输出通过审核：A 不跨 BBB，B 跨 BBB。`probability=null`，与四毒性端点不直接可比。hERG / DILI / AMES / ClinTox 候选全部 `unsupported`。来源、固定 revision、许可门禁及合法用户下载见 [TXGEMMA_COMPARE_REVIEW.md](docs/TXGEMMA_COMPARE_REVIEW.md)。
- `aidd.compare.facts.v1` 的 canonical JSON SHA256 固定所有事实、分数、来源、任务语义和章节归属。两个报告器收到逐字一致的服务器提示和事实副本。
- `aidd.compare.report.v1` 要求完整章节、结论 ID、精确 `value` / `sourceIds`；重复、缺失、跨章节移动、改分数、改来源、错误 SHA 或额外工具字段均拒绝。事实文字由程序从快照渲染，报告器只可在受控章节内排序。
- 所有生成自由文字标为 **unverified**，不升级成实验事实或机制证据。模型归因不是因果机制；人工 SMARTS 警示不是神经归因；相似度不是置信度。
- 快照只包含查询结构、真实模型输出、任务合同、规则/归因状态、证据数量等审计摘要及限制；不含实验数据行、检索样本结构/标签/文献、受限数据库、密钥、本机完整路径或自由问题。研究问题只在本机提取端点主题。
- ClinTox 少样本与 B16 负迁移风险保留。旧 test / 历史 hERG_Karim 已曝光，不能用于本轮调参或宣称新盲测。没有新合法独立标签，不报告 AUROC/AP 等新准确率或科学排名。

## Windows 11 + RTX 4060 8GB：启动

在已有整合项目使用已验收 CUDA 环境。全新克隆不包含 B16/MoLFormer 缓存：必须先按 [CUDA_VALIDATION.md](CUDA_VALIDATION.md) 和 [TOXNET_V3_MULTITASK.md](TOXNET_V3_MULTITASK.md) 合法准备与训练，不把空目录视为已安装。

```powershell
Set-Location 'D:\安装包\OneDrive\เอกสาร\ChatGPT\项目实践  二'
git switch feat/aidd-four-model-compare
$comparePython = '.\.venv-v3-cuda\Scripts\python.exe'
& $comparePython -m pip install -r requirements.txt -r requirements-model.txt -r requirements-v3.txt
& $comparePython -m pip check
& $comparePython -c "import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"

$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path '.\artifacts\integration\multitask-cuda-20261013\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path '.\models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
$env:TOXNET_COMPARE_PYTHON = (Resolve-Path $comparePython).Path
$env:TOXNET_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:TOXNET_OLLAMA_MODEL = 'qwen3.5:4b' # 必须匹配真实 /api/tags ID
$env:TOXNET_COMPARE_MAX_RUNNING = '1'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $comparePython -m uvicorn backend.main:app --host 127.0.0.1 --port 4185
```

访问 `http://127.0.0.1:4185/compare`；原工作台在 `/`。示例选择新端口，避免覆盖已有 4173/4183 服务。CPU 模式显式设置 `TOXNET_MULTITASK_DEVICE=cpu`，并使用具有完整 v3 依赖的 CPU Python；CUDA 不可用不会自动改用云端。

### 本地 Qwen：用户安装与验收

本轮没有擅自安装 Ollama 或下载模型。用户自行从 [Ollama 官方](https://ollama.com/download/windows) 安装并审阅模型许可后：

```powershell
ollama --version
ollama serve # 已在托盘运行服务时无需重复启动
# 在另一个 PowerShell 窗口查询已安装的真实 ID：
ollama list
Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags'
# 仅在本人决定下载、确认官方标签可用及许可后执行：
ollama pull qwen3.5:4b
```

若官方标签不存在，选择官方可取得的 Qwen 标签，更新 `TOXNET_OLLAMA_MODEL` 后重启服务。不能用其它模型假冒 Qwen3.5-4B。工作台刷新实际清单，记录 digest；不自动 `pull`。国内网络下载失败应保留官方缓存重试、使用官方允许的网络，不关闭 SHA/许可验证、不使用来源不明权重。Ollama 模型与显存尚未实际验收，本机报告可能因上下文、时限或严格 JSON 校验失败；先关闭 Tox21 报告展示缩短输入，再按错误实测，不降低事实核验。

Ollama URL 只允许 loopback HTTP，禁远程主机、代理、认证参数和重定向。请求使用 `keep_alive=0`；异常/取消尝试卸载并通过 `/api/ps` 确认。清理无法确认时隔离后续本地 GPU 阶段，需要检查 daemon 后重启 Compare 服务。

### DeepSeek：独立报告与外传

通过原工作台“配置 API”保存服务端 DeepSeek 密钥、账户实际模型名称及版本；浏览器 Compare 不接收或发送密钥。仅接受官方 `https://api.deepseek.com[/v1]/chat/completions`，禁重定向及环境代理，不允许自定义出站地址。

每次运行必须同时启用 DeepSeek 和勾选本次外传授权，可能产生费用。预设不保存授权；接受运行后 UI 消耗授权，重新运行需重新选择。两个报告使用同一最小科学事实；DeepSeek 不读取 Qwen 的报告。`requestAttempted` 仅证明本次 HTTP 请求曾发起，不保证被接收/计费；`executed` 表示已取得通过确定性核验的报告。费用只有供应商真实返回时才能记录；当前字段为 `null`，不是零费用。

## API 与运行生命周期

独立契约 `aidd.multimodel.compare.v1`，严格 Pydantic，拒绝未知字段、错误类型、非法 SMILES / 未注册任务。接口清单及字段见 [MODEL_API_CONTRACT.md](MODEL_API_CONTRACT.md)。

默认最大执行 1 个运行、最多 32 条内存记录；本机模型阶段额外共用串行锁。阶段上限 240 秒、整个运行（含等待）900 秒。终态记录在 1800 秒后由请求触发清理；服务重启丢失运行，导出 JSON 保存。可用环境变量调整，均有范围限制。预设最多 20 条，原子保存到忽略的 `data/compare-presets.json`，不保存分子/问题/密钥/授权。

`DELETE` 请求取消并等待子进程退出或 Ollama 清理确认；已完成任务不删除，保留到 TTL。取消、超时保留已确认 B16 结果。模型/输入修改使前端旧结果失效，迟到的创建响应自动取消，迟到轮询不能恢复旧结果；导出再次核对当前配置。

本机锁只管理本服务 Compare 阶段，不管理其它应用、其它服务进程或原研究端点。不要同时从旧服务或外部应用加载其它大型 GPU 模型。TxGemma 与 Qwen 的容量必须单独实测；不能把 MoLFormer 显存结论套用。CUDA/OOM/模型缺失明确失败；没有隐式 CPU 卸载、量化或收费回退。

```powershell
$compareRequest = @{
  smiles = 'CCO'; question = '关注 hERG'; mode = 'compare'
  primaryPredictor = 'toxnet-v3-b16'; specialistPredictor = 'txgemma-2b-predict'
  specialistTask = $null; primaryReporter = 'ollama-qwen-local'
  comparisonReporter = $null; ollamaModel = $null
  includeTox21 = $true; includeEvidence = $false; explainEndpoint = $null
  allowCloudTransfer = $false
} | ConvertTo-Json
$compareRun = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:4185/api/research/compare/runs' -ContentType 'application/json' -Body $compareRequest
Invoke-RestMethod -Uri ('http://127.0.0.1:4185' + $compareRun.pollUrl)
# 如需取消：
Invoke-RestMethod -Method Delete -Uri ('http://127.0.0.1:4185' + $compareRun.pollUrl)
```

## 已执行验证与重现

真实 HTTP 验收摘要：[runtime-verification.json](artifacts/compare/runtime-verification.json)。两组等价输入、展示开/关均使用真实 CUDA B16；只验证软件/身份一致性，不是准确率或吞吐基准。子进程预测约 0.20–0.21 秒，含加载约 7.3 秒；PyTorch 进程峰值 allocated 187,969,024 bytes / reserved 213,909,504 bytes，**不是整板显存**。无归因/证据选项的该探针不能代表更大 batch、长分子或片段扰动峰值。

```powershell
& '.\.venv-v3\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -q
& '.\.venv-v3-cuda\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -q
node --test test/*.test.js
& '.\.venv-v3-cuda\Scripts\python.exe' scripts/verify_compare_runtime.py `
  --model-dir artifacts/integration/multitask-cuda-20261013/B16-seed-42 `
  --encoder-dir models/pretrained/molformer --device cuda
# 明确 MOCK，不调用真实权重/Ollama/云端：
& '.\.venv-v3\Scripts\python.exe' -m test.compare_fixture
# 访问 http://127.0.0.1:4184/compare，页面醒目标注 Mock。
```

CPU 与 CUDA 环境各实际通过 **417 项 Python 测试**，全仓 **70 项 JavaScript 测试**通过；两套 pip check 和新增 JS 语法检查通过。Mock 测试覆盖四模型完整流程、共同快照、分数/来源篡改拒绝、授权、未完成/缺权重/未知 revision、取消/创建竞态、队列/过期、资源隔离、预设与旧 v2 HTTP 合同。真实浏览器检查全 Mock 四卡/双栏报告、专项类别、输入和配置失效、取消/清空、单报告失败保留结果及实际 JSON 下载，也在独立真实工作台验证 B16 的 4+12 表格、实测显存、其余模型不可用/跳过与真实 JSON 导出。50 个已跟踪历史工件与基准 main SHA256 一致。实际测试数和工件核验见 [validation.json](artifacts/compare/validation.json)、[browser-verification.json](artifacts/compare/browser-verification.json)、[artifact-preservation.json](artifacts/compare/artifact-preservation.json)。

尚未执行：真实 Qwen 报告（daemon 不可达）、真实 TxGemma（许可和权重缺失）、真实 DeepSeek（不自动执行收费调用），因此这些模型的 GPU 峰值、吞吐、费用及真实报告通过率均 **未测量**。没有本轮新独立标注数据，也没有科学性能排名。Tx/Qwen 授权安装后，三类本地阶段排队，云报告单独按本次授权执行；全部未准备时仍能保留可用 B16 与规则审计。

源码变更分为：注册/Adapter/worker；合同/事实核验/资源/编排/路由；独立 Compare 前端；CPU/HTTP/JS/浏览器 Mock 测试；离线 TxGemma 审计及真实 B16 复验脚本；文档、依赖和忽略规则。没有提交大型新权重、模型缓存、实验数据行、用户密钥或预设。
