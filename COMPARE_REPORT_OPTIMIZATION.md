# Compare 报告合同升级与运行验收

本轮只调整报告整理、交互和工程计量，不改变 ToxNet v2、B16 或 TxGemma 的预测值、训练数据划分、权重和历史评价。v3 继续 `formalAvailability=false`。软件测试、中文报告生成成功或报告变快，都不能证明模型毒性预测更准确。

历史本机部署证据保留在 [OLLAMA_DEPLOYMENT_ACCEPTANCE.md](OLLAMA_DEPLOYMENT_ACCEPTANCE.md)。本轮性能方法和实测结论以 [COMPARE_PERFORMANCE_BENCHMARK.md](COMPARE_PERFORMANCE_BENCHMARK.md) 为准；新聚合回执使用独立路径 `artifacts/compare/report-optimization-20261014/receipt.json`。没有实际回执时，Token、速度、显存和提速幅度均为待测，不能引用旧单次结果冒充本轮基准。

## 数据流程与兼容性

旧流程：RDKit → B16 / TxGemma 真实结果 → 不可变事实快照 → 语言模型重新复制每项 `value/sourceIds` 和事实顺序 → 严格比对 → 后端渲染。

新流程：RDKit → 同一真实结果与不可变事实快照 → 语言模型只安排事实 ID、章节顺序及有界解释 → 后端从完整快照重建所有科学字段 → 严格比对和渲染。Qwen 与已逐次授权的 DeepSeek 分别收到相同内容的独立副本，不读取对方报告。DeepSeek 仍需单独授权，不会因为本地报告失败而自动调用。

| 项目 | `aidd.compare.report.v1` | `aidd.compare.report.v2` |
|---|---|---|
| 生成字段 | 快照 SHA、章节/事实排列、逐项 `factChecks`、可选解释 | 快照 SHA、章节/事实排列、可选解释、不确定性、拒绝说明 |
| 科学值 | 生成器复制，后端要求完全一致 | 生成器没有可写字段，全部由后端快照重建 |
| 来源与版本 | 事实和来源必须与快照一致 | 结构、来源、权重/规则/任务版本由同一快照 SHA 绑定并重建 |
| 结论排序 | 每条事实恰好一次且不能跨章节 | 保留相同限制，不能遗漏事实以缩短输出 |
| 新增文字 | 一律 `unverified` | `commentary/uncertainty/refusal` 一律 `unverified` |
| 兼容入口 | API 省略新增字段时仍默认 `v1/deep` | 新前端显式提交 `v2/fast`；也可选择 `v2/deep` |

`reportContract` 严格接受 `v1/v2`，`reportMode` 严格接受 `fast/deep`，额外字段和强制类型转换拒绝。旧实验报告继续使用其原始合同查看；没有模式字段的历史 v1 显示为历史合同，而不是新生成的 v2。切换模式、模型、任务或分子后，旧结果失效，不能作为新配置的结果导出。

两种模式的报告输入都只使用现有最小事实快照：证据为审计状态和数量等摘要，归因为可用状态和方法。Deep不会自动把原始实验行、受限数据库、相似分子标签、文献原文、私有路径、密钥或前一个报告加入语言模型输入；资料不足时只能说明证据不足。已明确授权的云端调用也不扩大允许外发的信息类别。

v2 的模型原始输出只能具有以下结构；尖括号内容是格式说明，不是可执行回执或真实实验值：

```json
{
  "schema": "aidd.compare.report.v2",
  "snapshotSha256": "<服务器提供的64位小写SHA256>",
  "sectionOrder": ["prediction", "specialist", "evidence", "limits"],
  "claimOrder": {
    "prediction": ["<该章节全部原事实ID>"],
    "specialist": ["<该章节全部原事实ID>"],
    "evidence": ["<该章节全部原事实ID>"],
    "limits": ["<该章节全部原事实ID>"]
  },
  "commentary": [{"sectionId": "limits", "text": "尚无充分证据支持毒性因果机制。"}],
  "uncertainty": "研究预测不能代替实验验证。",
  "refusal": null
}
```

模型不能返回 `score/scores/sourceIds/sources/modelVersion/structureId/factChecks/tools` 等额外字段。验证器检查完整快照校验和、规范结构与 `structureId`、来源关联、模型/任务/规则版本、章节归属、所有事实 ID 的完整性和唯一性。生成器提供的错误或未知 schema、重复/遗漏结论、跨章节移动、错误快照 SHA 或越界解释均拒绝；失败时保留已获得的真实 Token 与来源元数据，不发布未通过验证的正文。

通过核验的响应仍提供兼容的 `sections[].text/claimIds/commentary`、`verifiedFactIds`、`sourceVerification`。v2 还提供 `verifiedFacts/verifiedSources/structure/chemistryVersion/factReconstruction`；这些内容来自服务器快照，不是模型自述。工作台从 `run.snapshot.facts` 渲染事实，单独标记生成解释。`verified-structured-report` 仅证明合同与既有数据字段一致，不能证明自由文字科学正确或机制成立。

Ollama 的 v2 请求实际使用服务器绑定的严格 JSON Schema 作为 `/api/chat` 的 `format`，`provenance.outputFormat=server-bound-json-schema`；不是只在提示词里要求 JSON。Schema 将快照 SHA 固定为常量，将 `claimOrder` 限定为各章节键组成的对象，并约束章节/事实合法 ID、固定数量、模式段数及字符上限。提示版本更新为 `aidd.compare.independent-report.v2.1`，提供完整 `responseTemplate`，移除重复章节事实清单，明确每章最多一段解释。v1 仍使用原 `format="json"` 和 v1 提示版本；Fast只缩短解释，不能删减完整 `factChecks`。结构化生成不是科学验证，后验哈希、身份、来源、事实完整性和文本总预算门禁均未放宽，不自动修补非法输出。

## Fast 与 Deep

两种模式保留同一完整事实、四毒性端点、所请求的十二通路、专项任务类别、来源状态与科研限制。它们不是不同预测模型，不会改变快照或预测值。

| 约束 | Fast | Deep（v2） |
|---|---|---|
| 整理目标 | 四端点摘要、通路提示、专项任务、证据状态、主要局限 | 详细端点、来源版本、校准/适用域、规则/归因、证据与专项差异；研究假设和后续实验建议仍未验证 |
| `commentary` 段数 | 最多 4 段 | 最多 8 段 |
| 每段字符数 | 最多 240 | 最多 800 |
| 解释合计字符数 | 最多 600 | 最多 4000 |
| `uncertainty` 字符数 | 最多 180 | 最多 600 |
| `refusal` 字符数 | 最多 180 | 最多 400 |

字符数是程序校验的 Unicode 文本长度，不是模型 Token 数；限制在生成后再次执行，不仅是提示词建议。解释允许为空。`v1/fast` 使用相同 Fast 解释限制；默认 `v1/deep` 保留原有 v1 行为以兼容旧客户端，不能假称其解释预算与新 v2 完全相同。

原 Qwen `num_ctx/num_predict` 仍由服务器环境配置，模式并不自动扩大显存或上下文容量。`generationOptions` 记录实际发出的预算，真实 `promptTokens/completionTokens` 来自 Ollama；二者不能混为一谈。减少输入中的重复文字和输出中的 `factChecks` 是工程设计，只有实际同条件基准才能说明是否减少 Token 或耗时。

## 中文问题理解

自由问题只在本机由 `backend/compare_question.py` 作确定性映射，不把原文发送给报告模型，不让语言模型定义标签。

| 词语示例 | 已有研究主题 | 必须保留的边界 |
|---|---|---|
| 心脏毒性 / hERG | `herg` | 只定位到现有 hERG 来源标签，不能代表全部心脏毒性 |
| 肝毒性 / 肝损伤 / DILI | `dili` | 保持原 DILI 正类合同 |
| 致突变性 / Ames | `ames` | 不扩大为所有遗传毒性或致癌任务 |
| 临床毒性 / ClinTox | `clintox` | 不是人体临床风险概率 |
| 血脑屏障 / BBB | `bbb` | 仅显式选择已审核 `tdc-bbb-martins` 时成为活动主题，不自动启用任务 |
| 通路活性 / Tox21 | `tox21` | 必须开启通路展示；十二个独立实验通路不是四毒性标签 |

多主题问题保留各已识别主题。无法识别、致癌/生殖毒性/肾毒性/安全剂量等未支持任务，或请求 BBB 但未选任务时，`snapshot.questionInterpretation` 给出 `requires-selection`、有限主题 ID 和中文警告；不会生成替代预测或推测实验阈值。前端事实来源区可展开查看识别结果。用户仍需显式选择归因端点、实验检索任务和专项任务。

## 服务版本、历史与资源状态

`GET /api/research/compare/runtime` 不调用模型，返回 `runtimeProvenance`、`resourceState`、`persistence`、`acceptingRuns/inFlightRuns`。启动时源码回执记录 Git SHA、相关源码逐文件 SHA、整体源码 SHA、进程 ID 和源码是否偏离提交；磁盘后续编辑不能把旧进程变成新部署。实际运行也保留当次源码回执。它是源码一致性证据，不是科学验证或硬件容量证明。

运行查询中的 `resourceState` 是 Compare 调度阶段和隔离状态，不是整卡 GPU 占用。`queueWaitMs`、阶段 `metrics.queueWaitMs/durationMs` 与 `generationSeconds` 分别显示，不将生成耗时当作加载、排队及卸载总耗时。

历史列表支持 `GET /api/research/compare/runs?limit=10&offset=0&q=fast`。`q` 最多 120 字符，只搜索 runId、状态、创建时间、结构 ID、已记录报告合同/模式、专项任务和 Ollama 模型等允许的元数据；不搜索 SMILES、自由问题、报告全文或实验记录。搜索仍按分页和原 TTL 过滤，不执行模型。

SQLite 继续使用 schema 2 和原 `aidd.compare.persisted-run.v1` 投影。新增模式、运行源码和计量为新记录的可选 JSON 字段，本轮不进行 schema2→新版本迁移，不补写或重写旧 payload/历史行。默认只落盘脱敏摘要；全文恢复仍要求原运行显式 `persistSensitiveContent=true`。普通重启不会解除既有 quarantine，不能同时启动第二个同库 owner，也不自动续跑或重复云端请求。

## Windows 11 / RTX 4060 8GB 完整启动

前提：已有通过 SHA 校验的 B16/MoLFormer、本机安装且授权的 TxGemma/Qwen 和可用 CUDA 环境。新克隆不包含权重或数据库；以下命令不会下载模型。已有模型无需再次 `pull`。先在当前服务原终端正常停止，并确认无运行中实验、清理已完成；如存在 quarantine，按 [工作台维护流程](AIDD_COMPARE_WORKBENCH.md#解除已确认的本机资源隔离) 核验，不能直接清空数据库。

```powershell
Set-Location 'D:\安装包\OneDrive\เอกสาร\ChatGPT\项目实践  二'
$comparePython = (Resolve-Path '.\.venv-v3-cuda\Scripts\python.exe').Path
& $comparePython -m pip check
& $comparePython -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"
Invoke-RestMethod -Uri 'http://127.0.0.1:11434/api/tags'

# 端口仍占用时先检查并正常停止原 Compare，不启动第二个数据库 owner。
if (Get-NetTCPConnection -LocalPort 4185 -State Listen -ErrorAction SilentlyContinue) {
    throw '4185 仍有服务监听。请先核对 runtime、活动实验和资源清理，并在原终端正常停止。'
}
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path '.\artifacts\integration\multitask-cuda-20261013\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path '.\models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
$env:TOXNET_COMPARE_PYTHON = $comparePython
$env:TOXNET_COMPARE_MAX_RUNNING = '1'
$env:WEB_CONCURRENCY = '1'
$env:TOXNET_COMPARE_GPU_SAMPLING = 'true'
$env:TOXNET_OLLAMA_URL = 'http://127.0.0.1:11434'
$env:TOXNET_OLLAMA_MODEL = 'qwen3.5:4b'
$env:TOXNET_OLLAMA_CONTEXT_TOKENS = '8192'
$env:TOXNET_OLLAMA_OUTPUT_TOKENS = '4096'
$env:AIDD_TXGEMMA_RUNTIME = 'ollama'
$env:AIDD_TXGEMMA_ENABLE = 'true'
$env:AIDD_TXGEMMA_LICENSE_ACCEPTED = 'true' # 仅在本人已完成必要授权后设置
$env:AIDD_TXGEMMA_OLLAMA_MODEL = 'txgemma-2b:latest'
$env:AIDD_TXGEMMA_OLLAMA_DIGEST = 'b6578da0f91e0974123bf1b9ba924349ef901c897143784fd746faaa1d1ba094'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $comparePython -m uvicorn backend.main:app --host 127.0.0.1 --port 4185 --workers 1
```

模型名和 TxGemma digest 是此机已审核包的配置，其他安装必须核对实际值，不允许复制 digest 伪装来源。第三方量化仓库没有公开 Google 上游 commit 的限制继续保留。`licenseAccepted=true` 是操作者声明，不是独立许可认证。

另开 PowerShell 查询，不打印 `.env` 或密钥：

```powershell
Set-Location 'D:\安装包\OneDrive\เอกสาร\ChatGPT\项目实践  二'
$compareRuntime = Invoke-RestMethod -Uri 'http://127.0.0.1:4185/api/research/compare/runtime'
$compareRuntime | ConvertTo-Json -Depth 8
Invoke-RestMethod -Uri 'http://127.0.0.1:4185/api/research/models'
Invoke-RestMethod -Uri 'http://127.0.0.1:4185/api/research/compare/runs?limit=10&offset=0&q=fast'
```

在 `/compare` 选择 Fast 或 Deep、`BBB_Martins` 和真实 Qwen ID，保持 DeepSeek 关闭即可做不产生外部收费 API 的三模型验收。正式基准先核对服务源码 SHA 与干净提交，再按性能文档运行；不能把 Mock 或旧进程的记录写成新代码验收。

`TOXNET_COMPARE_GPU_SAMPLING=true` 启用每个本机阶段的有界 `nvidia-smi` 采样，写入 `metrics.boardMemory`；默认 `false`。它记录包括桌面和其他程序的整卡采样最大占用，可能漏掉短峰值。`gpu.peakAllocatedBytes/peakReservedBytes` 是 PyTorch 进程内计量，二者不能相互代替。精确显存峰值、模型归属峰值和不可取得的值保持 `null`；WDDM 的 N/A 不当作零。不同时常驻所有本机模型，保持 Compare 本机阶段串行和卸载核验。报告为非流式，首 Token 延迟不可测，保持 `null`。

## 验收与仍需独立完成的工作

首次实际四臂测量来自源码 `04eb143b0838f52ebe06d81b48851621ee350670`：首组4次尝试中1次报告通过、3次报告失败。失败证据保留在 `artifacts/compare/report-optimization-20261014/benchmark.json`；该文件还保留下一组已取消运行的单独状态，不能将其当作第五次成功完成的四臂测量。已单独复现的 v2 Deep 问题为 `claimOrder` 被生成成数组，而合同要求对象，以及 `commentary` 产生12段、超过8段上限；不能推断另外两次失败也是相同原因。

修复采用上文明确模板与服务器绑定 JSON Schema，并保持失败结果原样。修复后的基准必须写到另一个独立输出路径，不能覆盖第一次回执、删除失败分母或把初版计量冒充新提示版本的成功证据。在修复后尚无实际成功回执时，新Token、耗时、提速幅度和报告通过率均待测；第一次不完整四臂组不能据此发布优化结论。

单元/API/浏览器软件验证应覆盖 v1 历史、新合同、事实 ID/源版本、哈希与结构身份、文本越界、中文问题、模式失效、JSON 导出、SQLite 恢复及无模型错误；软件通过数量和 CI 结论由最终验收追加，本文不预填。真实模型基准只使用真实 Ollama 返回的计量，区分冷启动、OS 文件缓存下的重载和真正模型常驻热运行；不能为制造热运行绕过卸载或占用锁。

科学限制始终完整：研究原型、非临床风险；B16 未独立校准和适用域未验证；ClinTox 少样本及可能负迁移；旧 test/Karim 已曝光；Tox21 不等同四毒性标签；BBB 类别不转概率；相似度不等于置信度；归因不等于因果；自然语言解释不等于实验机制验证。新独立标签评估、概率校准、适用域和真实 DeepSeek 收费调用验收不因本轮工程优化自动完成。

## 本轮修改文件索引

以下索引根据相对 `f10226046936121775df726c73a4fce5b3e360c8` 的实际 Git 差异与本轮新增文件整理；文件存在不表示真实基准已经完成或通过，不在此预填最终性能指标。

| 分组 | 实际修改或新增文件 | 主要用途 |
|---|---|---|
| 后端报告与适配 | `backend/compare_adapters.py`、`backend/compare_reporting.py`、`backend/compare_question.py`、`backend/compare_schemas.py` | v1/v2 合同、受控结构化生成、Fast/Deep 与中文问题识别 |
| 后端运行与持久化 | `backend/compare_orchestrator.py`、`backend/compare_persistence.py`、`backend/compare_routes.py`、`backend/compare_runtime.py`、`backend/compare_gpu_metrics.py` | 运行源码回执、历史兼容、搜索、资源状态及有界整卡采样 |
| 前端 | `compare-app.js`、`compare-core.js`、`compare.html`、`compare.css` | 报告模式、来源与未验证解释区分、真实计量、历史搜索和交互稳定性 |
| 基准脚本 | `scripts/benchmark_compare_reports.py` | 同条件报告对照、失败和中断保留、独立聚合输出 |
| JavaScript 测试 | `test/compare-core.test.js`、`test/compare-ui-state.test.js` | 模式切换、旧历史、JSON 导出、搜索分页与真实响应字段形状 |
| Python 测试与夹具 | `test/compare_fixture.py`、`test/test_compare_adapters.py`、`test/test_compare_benchmark.py`、`test/test_compare_gpu_metrics.py`、`test/test_compare_persistence.py`、`test/test_compare_report_integration.py`、`test/test_compare_report_v2.py` | 合同与事实重建、安全门禁、计量错误处理、SQLite 兼容及运行源码记录 |
| 配置与提交边界 | `.env.example`、`.gitignore` | 可审查的配置示例与聚合回执白名单；不提交真实密钥、权重或研究缓存 |
| 文档 | `AIDD_COMPARE_WORKBENCH.md`、`MODEL_API_CONTRACT.md`、`README.md`、`OLLAMA_DEPLOYMENT_ACCEPTANCE.md`、`COMPARE_REPORT_OPTIMIZATION.md`、`COMPARE_PERFORMANCE_BENCHMARK.md` | 使用步骤、接口合同、优化设计、兼容性与真实验收边界 |
| 新聚合回执 | `artifacts/compare/report-optimization-20261014/benchmark.json`、`artifacts/compare/report-optimization-20261014/benchmark-final.json`、`artifacts/compare/report-optimization-20261014/receipt.json` | 分别保留初版尝试与修复后独立运行记录；不能覆盖失败记录或混用其源码版本 |

修复后本轮运行源码固定为 `9ff4a42d9ee397801eaacc76425102ff63064e2a`。最终文档、测试及聚合回执收尾提交可以产生新的 Git 提交 SHA，但这些文件不在 `backend/compare_runtime.py` 的后端源码哈希集合中，不改变本轮已固定的后端 `runtimeProvenance.sourceSha256` 或逐文件 `sourceFiles` 校验值。验收应同时保留运行时记录的启动提交与源码哈希，不能仅用后续仓库 HEAD 代替实际运行版本；任何后端源码再变动都需要另行重启和验收。


## 本轮最终实测

相对于同条件 v1/Deep，中位数口径下 v2/Fast 的 Prompt Token 减少 23.86%，Completion Token 减少 75.29%，Qwen 阶段耗时下降 67.38%。这是有限样本工程对照，不能解释为预测准确率提高，也不能外推为长期 SLA。

完整四臂表、显存口径、失败记录、软件测试与浏览器结果见[性能文档](COMPARE_PERFORMANCE_BENCHMARK.md)及[验收回执](artifacts/compare/report-optimization-20261014/receipt.json)。使用命令见前文Windows启动部分；若PowerShell系统代理误使loopback返回502，可在PowerShell7使用Invoke-RestMethod -NoProxy，或用本项目Python的httpx.Client(trust_env=False)查询，不修改系统代理或TLS校验。
