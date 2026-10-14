# Compare 报告性能基准

本页记录本轮的测量协议和复现命令。历史 `OLLAMA_DEPLOYMENT_ACCEPTANCE.md` 中约 54 秒的 Qwen 记录是历史单次运行，不能代替本轮最新后端的重复测量。未实测的指标保持待测或 `null`，工程速度不代表毒性预测准确率。

## 四个配置组成 2 × 2 对照

报告合同与报告深度是两个独立维度，不是四个不同的预测模型。

| 配置 ID | 请求字段 `reportContract` | 请求字段 `reportMode` | 用途 |
| --- | --- | --- | --- |
| `v1-deep` | `v1` | `deep` | 原始完整事实回写合同及深度整理 |
| `v2-deep` | `v2` | `deep` | 轻量合同及深度整理，考察合同变化 |
| `v1-fast` | `v1` | `fast` | 原始合同及快速整理，考察深度变化 |
| `v2-fast` | `v2` | `fast` | 轻量合同及快速整理，工作台推荐组合 |

每次执行都真实运行 B16 → TxGemma `BBB_Martins` → Qwen；不把缓存预测或 Mock 输出作为测量。两种报告合同都基于完整不可变事实快照，Fast 不删除后端重建的科学事实和必要局限。DeepSeek 设为 `null`，`allowCloudTransfer=false`，脚本不付费调用、不下载模型、不变更服务端配置。

`scripts/benchmark_compare_reports.py` 固定串行顺序，支持多个合法输入及重复实验。每个同结构、同重复编号的四配置组必须具有完全相同的 `structureId`、事实快照 SHA256 和三个实际模型的身份／修订／权重校验信息，才能进入可比统计。若任何记录中模型身份改变，整个聚合禁止混合修订后给出可比结论。请求合同、服务端返回合同、来源核验、报告状态与清理确认也必须一致合法。

固定顺序可能引入时间、温度和文件缓存的顺序效应；这是本机探索性工程对照，不是随机化性能试验。若要用于部署容量规划，应进一步增加样本和交叉顺序设计。

## Windows 11 / RTX 4060 8GB 运行

先按工作台启动文档启动最新代码的单 worker 后端，核验运行源码版本，并确认没有其他活动的 Compare 任务。不要再打开第二个同 SQLite 数据库 owner。以下命令只测现有服务，不负责重启、解除 quarantine 或安装模型。

```powershell
Set-Location 'D:\安装包\OneDrive\เอกสาร\ChatGPT\项目实践  二'
& '.\.venv-v3-cuda\Scripts\python.exe' -c "import torch; print(torch.__version__); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"
Invoke-RestMethod 'http://127.0.0.1:11434/api/tags'
Invoke-RestMethod 'http://127.0.0.1:11434/api/ps'
Invoke-RestMethod 'http://127.0.0.1:4185/api/research/models'
Invoke-RestMethod 'http://127.0.0.1:4185/api/research/compare/runtime'
git rev-parse HEAD
```

确认 Ollama 的实际 Qwen ID、digest、TxGemma 来源审核及 BBB 任务都通过；不能仅凭本机 checkout 的 HEAD 推定现有进程已加载该提交。脚本读取 `/api/research/compare/runtime` 的启动源码凭据，拒绝源码与提交不符、服务未接单、存在活动运行或资源隔离。`--source-revision` 若提供则必须与实际服务的 commit 一致；各运行的 commit／源码集合 SHA／启动时间也须与预检一致，服务中途重启会停止基准。

下面使用乙醇和乙酸两个公开基础结构，每个结构重复两次，共 16 个真实 Compare 运行。预计需要多分钟，具体时间由实测决定。使用新的输出路径；已有文件会被拒绝覆盖，避免改写历史证据。

```powershell
$auditedServiceCommit = '<已确认运行服务的40位提交SHA>'
& '.\.venv-v3-cuda\Scripts\python.exe' '.\scripts\benchmark_compare_reports.py' `
  --base-url 'http://127.0.0.1:4185' `
  --smiles 'CCO' --smiles 'CC(=O)O' `
  --repeat 2 --max-runs 16 `
  --ollama-model 'qwen3.5:4b' `
  --gpu-sample --gpu-sample-seconds 0.25 `
  --source-revision $auditedServiceCommit `
  --output '.\artifacts\compare\report-optimization-20261014\benchmark.json'
```

若服务端已启用阶段显存采样，可以去掉 `--gpu-sample`，避免重复采样增加测量开销。GPU 不可用或没有 `nvidia-smi` 时，显存信息明确为 `unavailable`／`null`；不得把 CPU 或 Mock 测试当成 RTX 实测。

服务端阶段采样通过启动前设置 `$env:TOXNET_COMPARE_GPU_SAMPLING='true'` 启用，各本机模型的 `metrics.boardMemory` 在适配器清理完成后结束采样。所有对照臂须使用相同设置；该阶段总时长包含采样停止所需的监控开销。基准聚合保留该字段的安全白名单；它与脚本可选的整次运行采样分别标注窗口。

默认最多 12 次，16 次计划需要显式 `--max-runs 16`。脚本上限为 10 个输入、10 次重复、120 次总运行；不会悄悄只保留部分对照臂。任一任务被外部干扰、报告核验失败、来源改变或清理不确定时，不得从该组计算优化结论。超时和 Ctrl+C 会尝试取消本脚本拥有的运行，已有完整结果继续留在聚合 JSON；清理未确认时必须检查隔离状态后再开始新运行。

## 冷启动、热运行与 Token

当前适配器使用 `keep_alive=0` 并验证卸载，因此后续请求按模型卸载后的加载条件执行。第一个请求之前的 Ollama 模型常驻状态并非由基准脚本管理；若要标注第一轮也为冷模型加载，应在运行前保存 `/api/ps` 为空的检查证据。模型卸载不等于清空操作系统文件缓存，文件页缓存是否预热没有被控制。

本轮不实现为计时而常驻全部模型，也不把后续卸载后请求误称为热模型运行。热模型速度及冷／热差值未测；显存与清理约束优先。后续热运行实验需要独立、受控的驻留策略及 GPU 容量验证，不能沿用此协议的数据。

记录的 `promptTokens`、`completionTokens`、`generationSeconds` 来自 Ollama 实际响应，`tokensPerSecond` 为实际输出 Token 数／实际解码时间。它不包含模型加载、提示预填充、排队或清理时间。模型阶段 `durationMs` 和整次运行 `durationMs` 另行记录。非流式 `/api/chat` 无法观测首 Token 延迟，所以 `ttftSeconds=null`，不能用 HTTP 响应总时长代替 TTFT。

报告核验失败时保留已返回的实际 Token 计量；失败记录不冒充成功报告。`sourceErrorCount=0` 仅用于通过精确来源核验的报告。拒绝输出的具体来源错误数量不可推断，保留 `null`。`unverifiedAdditionCount` 是未核验自由文本段数，包含解释、不确定性和拒绝说明；它不是虚假科学结论数量。自动语义审查尚不能可靠计数新增科学断言，`unsupportedScientificClaimCount=null`。

## GPU 测量口径

`backend/compare_gpu_metrics.py` 的 `NvidiaSmiSampler` 不加载 CUDA 或模型，只在有界窗口内通过 NVIDIA 驱动读取整卡显存。输出包含 UTC 测量起止、采样次数、请求间隔、每卡首／末观测与采样最大显存。实际间隔还包含驱动查询耗时。

- 整卡显存包括桌面、图形应用及其他计算进程，不是模型独占显存。
- 采样最大值可能遗漏短峰值，因此 `exactPeakBytes=null`，`modelAttributedPeakBytes=null`。
- 计算进程数量用于背景提示，不证明所有权；Windows WDDM 可能不提供进程显存，也不能完整列出图形进程。
- 不记录进程名、PID、完整本机路径；没有采样时不填写零。
- B16 的 PyTorch `peakAllocatedBytes`／`peakReservedBytes` 只表示隔离进程的分配器，两者与整卡显存分别保存，不混成一个峰值。
- GGUF 文件大小、量化位数或 MoLFormer 既往 CUDA 成功都不能替代 Qwen／TxGemma 的显存实测。

脚本的可选 GPU 数据覆盖整个 Compare 运行窗口，不能依据该窗口的单个最大值断定是 Qwen 或 TxGemma 的模型峰值。若要分析模型切换，使用服务端阶段采样、真实 `loadSeconds` 和清理记录；阶段总时长之差不直接等同于纯模型切换开销。

## 统计和证据输出

聚合只持久化结构 ID、运行 ID、配置、已审核模型身份、快照哈希、验证状态及工程计量，不保存 SMILES、完整报告、原始实验行、私有目录、密钥或 Hugging Face Token。工作台继续使用本地最小摘要 SQLite 策略；脚本不删除历史数据。提交 GitHub 前仍需审核聚合工件，不能提交本机缓存、模型权重或数据库。

每臂统计同时给出 attempted／verified／comparable 数，失败不能从分母悄悄消失。中位数与 P95 仅从完整、同快照、同模型、成功清理的四配置组计算；P95 使用排序后线性插值。一条记录不估计 P95；少于 20 条明确标记小样本探索性估计，不用于长期平均速度或 SLA 宣传。

### 本轮实测记录

2026-10-14 修复源码 `9ff4a42d9ee397801eaacc76425102ff63064e2a` 已实际完成 16 次本机 Compare；16 份报告通过核验，4 个完整四臂组满足同结构/快照/权重/模型版本/源码一致条件。证据：[最终聚合](artifacts/compare/report-optimization-20261014/benchmark-final.json)、[验收回执](artifacts/compare/report-optimization-20261014/receipt.json)。每臂仅4次，小样本P95仅为探索性插值估计。

| 配置 | 可比运行数 | Prompt Token 中位数 | Completion Token 中位数 | Qwen 阶段中位数 / P95（秒） | 解码 Token/s 中位数 | 报告通过 / 尝试 | 整卡采样最大 MiB（Qwen阶段） |
|---|---:|---:|---:|---:|---:|---:|---:|
| v1-deep | 4 | 4381.5 | 3067 | 68.00 / 68.56 | 52.02 | 4 / 4 | 5919 |
| v2-deep | 4 | 3338 | 958 | 26.47 / 28.20 | 52.98 | 4 / 4 | 5671 |
| v1-fast | 4 | 4487.5 | 3432.5 | 74.46 / 75.52 | 52.05 | 4 / 4 | 5677 |
| v2-fast | 4 | 3336 | 758 | 22.18 / 22.65 | 53.17 | 4 / 4 | 5672 |

相对于同条件 v1/Deep，中位数口径下 v2/Fast 的 Prompt Token 减少 23.86%，Completion Token 减少 75.29%，Qwen 阶段耗时下降 67.38%。这是有限样本工程对照，不能解释为预测准确率提高，也不能外推为长期 SLA。

各次报告保留27条事实与6条限制，核验通过的来源错误计数为0；自由解释均未验证，新增无依据科学断言的语义计数仍为null。v1/Fast仍需复制全部factChecks，实测不能据名称推定其比v1/Deep更快。

初版 `04eb143` 的1通过、3报告失败及后续1取消保留于 [初次回执](artifacts/compare/report-optimization-20261014/benchmark.json)，没有覆盖或纳入修复后的性能分母。已复现的v2/Deep失败为claimOrder数组和过多解释段；修复增加模板及受快照约束的JSON Schema，后验验证未放宽。

CPU 解析、聚合、同一性保护和无云流程可单独验证；这仅为软件测试：

```powershell
& '.\.venv\Scripts\python.exe' -m unittest test.test_compare_gpu_metrics test.test_compare_benchmark -v
```

本轮毒性科学准确率、独立概率校准、适用域验证和全新盲测均不由报告性能实验完成；`formalAvailability=false`。TxGemma 仍只审查 BBB 类别，`probability=null`，不能与四毒性标签求平均或作为临床风险概率。第三方 TxGemma GGUF 未公开上游 Google commit 与转换版本的来源局限仍然保留。
