# 蓝研 AIDD：四模型 Compare 工作台

Compare 是独立研究对照，**不是四模型投票、分数平均或准确率排名**。正式 v2 的四端点 API 与工件不变；全部 v3 注册项仍为 `formalAvailability=false`。分支 `feat/aidd-four-model-compare` 基于 main `3e0cbe432360b5c9b22aa0c3d538c5b0e23e8a51`，不会自动合并或部署。

## 架构与实际能力

| 注册 ID | 作用 / 真实实现 | 初版 Compare 的真实执行记录 |
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

Ollama URL 只允许 loopback HTTP，禁远程主机、代理、认证参数和重定向。请求使用 `keep_alive=0`；每次资源占用建立单个被追踪且有界的清理任务，正常完成、重复取消或shutdown等待它结束，并通过 `/api/ps` 确认卸载。清理不能确认时隔离后续本机阶段；已落盘的隔离状态不会由普通重启解除，安全恢复流程见下文。

### DeepSeek：独立报告与外传

通过原工作台“配置 API”保存服务端 DeepSeek 密钥、账户实际模型名称及版本；浏览器 Compare 不接收或发送密钥。仅接受官方 `https://api.deepseek.com[/v1]/chat/completions`，禁重定向及环境代理，不允许自定义出站地址。

每次运行必须同时启用 DeepSeek 和勾选本次外传授权，可能产生费用。预设不保存授权，历史恢复不复用它；提交时 UI 立即消耗授权，重试也需重新选择，迟到响应不清除新输入的新授权。历史可以保留“原运行是否授权/是否尝试请求”的审计标记，但已保存请求的 `allowCloudTransfer` 写为false，不能作为下一次执行许可。两个报告使用同一最小科学事实；DeepSeek 不读取 Qwen 的报告。`requestAttempted` 仅证明本次 HTTP 请求曾发起，不保证被接收/计费；`executed` 表示已取得通过确定性核验的报告。合法usage在内容解析前记录：报告被拒绝或JSON核验失败，不代表供应商没有生成Token或计费。实际返回的计量可保留，无效值保持null。费用当前为unknown/null，不是零费用。

## API 与运行生命周期

独立契约 `aidd.multimodel.compare.v1`，严格 Pydantic，拒绝未知字段、错误类型、非法 SMILES / 未注册任务。接口清单及字段见 [MODEL_API_CONTRACT.md](MODEL_API_CONTRACT.md)。

默认最大执行1个运行、最多32条内存记录；本机模型阶段额外共用串行锁。阶段上限240秒、整个运行（含等待）900秒。终态默认1800秒有效，启动及后续请求清理过期记录。SQLite保存允许持久化的历史投影，重启不会自动续跑或重复云端调用；全文能否恢复取决于本次保存授权。可用环境变量调整时限与容量，均有范围限制。

`DELETE`请求取消并等待子进程退出或Ollama清理确认；重复取消、HTTP断开及shutdown不会提前释放本机资源锁。已终态任务不删除，保留到TTL，取消/超时保留已确认B16结果。输入或配置修改使旧结果失效，迟到创建响应自动取消，迟到轮询不能恢复旧结果；导出再次核对当前配置。普通页面跳转仅停止浏览器轮询，后台已授权的运行可继续；回到页面通过runId重新GET，不重复POST。

当前仅支持单worker（启动时 `WEB_CONCURRENCY` 必须为1），同一SQLite文件通过OS owner锁拒绝第二个服务进程。启动命令不要增加 `--workers`，本机使用1个服务监听loopback。不同数据库、不同工作目录、旧研究API与其他应用不共享Compare GPU锁；这不是整机或多租户调度。TxGemma与Qwen容量仍须单独实测，不能套用MoLFormer显存结论。CUDA/OOM/模型缺失明确失败；没有隐式CPU卸载、量化或收费回退。

```powershell
$compareRequest = @{
  smiles = 'CCO'; question = '关注 hERG'; mode = 'compare'
  primaryPredictor = 'toxnet-v3-b16'; specialistPredictor = 'txgemma-2b-predict'
  specialistTask = $null; primaryReporter = 'ollama-qwen-local'
  comparisonReporter = $null; ollamaModel = $null
  includeTox21 = $true
  includeEvidence = $true; evidenceEndpoint = 'herg'
  includeAttribution = $false; explainEndpoint = $null
  allowCloudTransfer = $false; persistSensitiveContent = $false
} | ConvertTo-Json
$compareRun = Invoke-RestMethod -Method Post -Uri 'http://127.0.0.1:4185/api/research/compare/runs' -ContentType 'application/json' -Body $compareRequest
Invoke-RestMethod -Uri ('http://127.0.0.1:4185' + $compareRun.pollUrl)
# 如需取消：
Invoke-RestMethod -Method Delete -Uri ('http://127.0.0.1:4185' + $compareRun.pollUrl)
```

## P0/P1：本机历史、稳定交互与维护

实验检索与归因使用两个独立开关：只开启 `includeEvidence=true` 并选择 `evidenceEndpoint=herg` 不会进行片段删除；只开启 `includeAttribution=true` 并指定 `explainEndpoint` 也不会自动检索实验库。新界面始终显式提交开关。旧客户端省略 `includeAttribution` 时仍根据非空 `explainEndpoint` 推导，以保留原合同；旧请求省略 `evidenceEndpoint` 时沿用旧端点过滤。

报告、证据、状态卡、端点和性能表分别按内容更新。未变化的区域保留原DOM；变化时保存已有details展开、键盘焦点及阅读锚点，JSON对象键顺序变化不会触发重绘。真实Ollama列表决定当前所选模型的ID/digest状态，安装不等于已经完成生成；DeepSeek完整配置显示“访问待核验”。Mock页面和带Mock来源的历史记录始终明确标注软件夹具，不称真实部署。

本机历史数据库使用SQLite schema version 2，事务迁移schema1的运行表并新增持久资源状态。库与owner-lock使用Windows当前用户/SYSTEM私有ACL或POSIX私有权限；拒绝符号链接路径和不支持的未来schema。它提供本机访问控制，没有数据库加密或多用户认证，文件仍需按研究数据权限管理。

| 保存/恢复字段 | 实际行为 |
|---|---|
| `persistSensitiveContent=false`（默认） | 保存运行状态、结构ID、实际预测和usage摘要、脱敏模型来源及报告摘要；不保存查询SMILES、自由问题、实验行/图片、完整事实快照或报告正文 |
| 本次显式 `persistSensitiveContent=true` | 允许完整研究内容在本机保存；仍剔除密钥、token、认证信息和本机路径，不授予公开或云端传输许可 |
| `contentSaved` | 表示完整内容是否持久化，界面保存授权提交时立即消费，预设不包含它 |
| `recovery.contentAvailable` | 表示当前GET是否还能提供全文；同进程内存可能有全文但未持久化，重启后只能恢复摘要 |
| 中断恢复 | 保留成功结果，把未完成阶段标为 `INTERRUPTED`，整个运行转为 `failed/partial`；不恢复模型执行或云端授权 |

Windows工作目录含OneDrive目录名时，默认库放在 `%LOCALAPPDATA%/AIDD-Blue-ToxNet/compare/<workspace-hash>/runs.sqlite3`，避免研究全文自动同步到云盘；其他目录默认 `data/compare/runs.sqlite3`。新默认不会自动搬移旧数据库，切换前需要保留所需导出并确认实际库位置。不要将库、SQLite日志、owner-lock、预设或研究导出加入Git。

“实验历史”使用 `GET /api/research/compare/runs?limit=10&offset=0` 分页，只展示摘要；点击记录再GET指定runId。浏览器localStorage仅记住最后一个runId，不保存SMILES、正文或两类授权。刷新和浏览器后退只恢复GET轮询，不重新提交。原结构/全文未保存时界面明确说明，允许导出已保存摘要，不通过重算伪装成原结果。

预设最多20条，原子保存到忽略的 `data/compare-presets.json`。创建、更新/重命名、删除分别使用POST、PATCH、DELETE，更新和删除需用户确认；名称去空白后按不区分大小写的规则唯一，同名冲突409。预设保留创建/更新时间，不包含分子、自由问题、密钥、云端或敏感内容保存授权。

### 解除已确认的本机资源隔离

清理不确定时本机阶段进入quarantine。数据库schema2的服务状态独立于历史TTL，普通重启不会自动清掉已保存的隔离。没有浏览器“强制解锁”按钮；先正常停止Compare并等待其关闭，确认本次B16/TxGemma子进程退出，处理仍占用模型的本机Ollama。维护脚本不会杀进程、下载/加载模型或发起收费生成，只查询进程与指定loopback `/api/ps`；查询失败不等于已经卸载。

在前文已配置 `$comparePython` 的PowerShell中执行：

```powershell
# 只计算并在本机查看默认数据库路径；不会创建/迁移数据库。
$compareHistoryDatabase = & $comparePython -c "from backend.compare_persistence import ROOT, default_history_path; print(default_history_path(ROOT))"
if (Get-Command ollama -ErrorAction SilentlyContinue) { ollama ps }
# 如有模型，先通过Ollama正常停止本人本次运行的实际模型；未安装时无需下载。

# 第一步只核验，不修改quarantine。Compare服务必须先停止。
& $comparePython scripts/verify_compare_cleanup.py --database $compareHistoryDatabase
if ($LASTEXITCODE -ne 0) { throw '清理未通过核验，停止后续解除操作。' }

# 仅在上一步status=verified且本人确认解除隔离时执行。
& $comparePython scripts/verify_compare_cleanup.py --database $compareHistoryDatabase --clear-quarantine

# 正常重启，同一项目只启动一个worker。
& $comparePython -m uvicorn backend.main:app --host 127.0.0.1 --port 4185 --workers 1
```

若使用自定义数据库，必须将 `--database` 指向该服务的实际库。若更改过Ollama loopback地址，两个维护命令同样追加 `--ollama-url 'http://127.0.0.1:实际端口'`。仍有服务owner、无法读取相关进程或Ollama卸载不明时返回unavailable，不清除隔离。解除记录只证明本服务worker和指定Ollama在核验时满足条件，`globalGpuAvailabilityCertified=false`；不证明其他软件没有占用显存。存储故障与清理故障应保留脱敏诊断，不能把丢失隔离证明当作安全释放。

P0诊断日志仅允许关联requestId/runId/modelId、阶段、errorCode、合法duration、清理状态和HTTP尝试标志；不记录请求/响应全文、SMILES、实验数据、完整本机路径或密钥。HTTP成功后仍可能发生报告校验失败，已返回的真实usage可以保留，费用保持unknown；失败结果不发出未经核验的报告文本，也不自动重试或切换供应商。

### 无大型权重的CPU工作流

`.github/workflows/compare-cpu.yml` 设计为Ubuntu/Python3.13/Node22的软件回归：显式安装CPU Torch，设置HF离线及关闭研究/TxGemma授权，不下载MoLFormer、TxGemma或Ollama权重，不访问收费API。仓库原有v2小权重可用于相应真实加载合同；大编码器与外部模型场景使用显式软件夹具和临时测试工件。Windows ACL/OneDrive专属测试在Linux跳过，POSIX权限检查适用Linux；CPU CI不能证明RTX4060、真实Ollama、TxGemma或云模型已部署。

本次P0/P1最终测试数量、实际通过的Linux CI与真实浏览器/模型复验见 [AIDD_COMPARE_V1_ACCEPTANCE.md](AIDD_COMPARE_V1_ACCEPTANCE.md)。初版历史证明与本轮新增证明分别保留；安装状态不等于真实生成验收。

## 初版 Compare 已执行验证与重现（历史记录）

以下是初版Compare的既有验收证据，保留原数值，不代表本次P0/P1源码的最终回归结果。

真实 HTTP 验收摘要：[runtime-verification.json](artifacts/compare/runtime-verification.json)。两组等价输入、展示开/关均使用真实 CUDA B16；只验证软件/身份一致性，不是准确率或吞吐基准。子进程预测约 0.20–0.21 秒，含加载约 7.3 秒；PyTorch 进程峰值 allocated 187,969,024 bytes / reserved 213,909,504 bytes，**不是整板显存**。无归因/证据选项的该探针不能代表更大 batch、长分子或片段扰动峰值。

```powershell
& '.\.venv-v3\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -q
& '.\.venv-v3-cuda\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -q
node --test test/*.test.js
& '.\.venv-v3-cuda\Scripts\python.exe' scripts/verify_compare_runtime.py `
  --model-dir artifacts/integration/multitask-cuda-20261013/B16-seed-42 `
  --encoder-dir models/pretrained/molformer --device cuda `
  --output artifacts/compare/local-runtime-new.json
# 明确 MOCK，不调用真实权重/Ollama/云端：
& '.\.venv-v3\Scripts\python.exe' -m test.compare_fixture
# 访问 http://127.0.0.1:4184/compare，页面醒目标注 Mock。
```

CPU 与 CUDA 环境各实际通过 **417 项 Python 测试**，全仓 **70 项 JavaScript 测试**通过；两套 pip check 和新增 JS 语法检查通过。Mock 测试覆盖四模型完整流程、共同快照、分数/来源篡改拒绝、授权、未完成/缺权重/未知 revision、取消/创建竞态、队列/过期、资源隔离、预设与旧 v2 HTTP 合同。真实浏览器检查全 Mock 四卡/双栏报告、专项类别、输入和配置失效、取消/清空、单报告失败保留结果及实际 JSON 下载，也在独立真实工作台验证 B16 的 4+12 表格、实测显存、其余模型不可用/跳过与真实 JSON 导出。50 个已跟踪历史工件与基准 main SHA256 一致。实际测试数和工件核验见 [validation.json](artifacts/compare/validation.json)、[browser-verification.json](artifacts/compare/browser-verification.json)、[artifact-preservation.json](artifacts/compare/artifact-preservation.json)。

尚未执行：真实 Qwen 报告（daemon 不可达）、真实 TxGemma（许可和权重缺失）、真实 DeepSeek（不自动执行收费调用），因此这些模型的 GPU 峰值、吞吐、费用及真实报告通过率均 **未测量**。没有本轮新独立标注数据，也没有科学性能排名。Tx/Qwen 授权安装后，三类本地阶段排队，云报告单独按本次授权执行；全部未准备时仍能保留可用 B16 与规则审计。

源码变更分为：注册/Adapter/worker；合同/事实核验/资源/编排/路由；独立 Compare 前端；CPU/HTTP/JS/浏览器 Mock 测试；离线 TxGemma 审计及真实 B16 复验脚本；文档、依赖和忽略规则。没有提交大型新权重、模型缓存、实验数据行、用户密钥或预设。
