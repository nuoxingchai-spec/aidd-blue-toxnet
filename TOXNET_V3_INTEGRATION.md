# 蓝研 ToxNet 前三轮整合与运行验收

本轮在 `codex/toxnet-integration-cuda` 分支完成代码审查、必要修复、CPU/CUDA 回归、历史工件验收及两组独立冻结训练复现。该分支继承 PR #3 的全部成果；`main` 保持不变，三个原 PR 尚未合并。v3 继续使用研究接口，所有研究工件保持 `formalAvailability=false`。新训练输出到独立路径，没有重新拟合历史模型或改写旧评价结果。

真实 RTX 4060 CUDA 验收已通过，包括 MoLFormer 前向、真实 B16 副本前向/反向/更新及 batch 1/2/4/8 显存测试。原加载模型与磁盘权重没有被验收步骤修改。设备、峰值显存和操作步骤见 [CUDA_VALIDATION.md](CUDA_VALIDATION.md)；完整真实开发指标见第 8 节。

## 1. 实际 PR 状态和依赖

2026-10-13 通过 GitHub CLI 重新读取到的状态如下。`CLEAN / MERGEABLE` 只描述当前各自目标分支上的合并状态，不代表已合并，也不替代本地科学和软件验收。

| PR | 状态 | Head 分支和提交 | Base 分支 | 合并状态 |
|---|---|---|---|---|
| [#1](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/1) | OPEN、Draft | `feat/toxnet-v3-pretraining` / `ebfa7e44f5c7c737f51b907f7f1af5ea766d1613` | `main` | CLEAN / MERGEABLE |
| [#2](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/2) | OPEN、Draft | `codex/toxnet-v3-multitask` / `bb35137fe805ac69aa1343c119386f3a9567ea43` | `feat/toxnet-v3-pretraining` | CLEAN / MERGEABLE |
| [#3](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/3) | OPEN、Draft | `codex/toxnet-explainable-reports` / `c997ae774d40190002f3ec20b807d1c981f1fb4b` | `codex/toxnet-v3-multitask` | CLEAN / MERGEABLE |

审查时 `origin/main` 为 `51f683e8aba729a2af6221a6b92f8fb5ab4d4ef8`。提交祖先依次为 main → PR #1 → PR #2 → PR #3 → 本轮集成分支，未发现需要重写 v2 的接口冲突或循环引用。

共同编辑的文件包括三轮的 `.gitignore`、`.gitattributes`、`README.md`，以及 PR #2/#3 的 `backend/main.py` 和 `MODEL_API_CONTRACT.md`。它们在当前历史中是累积修改；后两轮已经包含前轮源码。不能把每个堆叠分支都当作独立补丁完整复制到 main。

## 2. 审查结果与本轮修复

已审查数据协议、分子身份、特征顺序及缓存、train-only 预处理、缺失标签 mask、模型加载、来源哈希、API 路由、安全边界和工作台解释状态。

| 问题 | 修复及验证 |
|---|---|
| 四任务 v3 只信任派生禁止重叠索引；同时重写索引和 manifest 哈希可以放宽历史隔离 | `backend/v3_data.py` 从固定旧 validation/test 和已曝光 Karim 来源重新构建 canonical、母体、立体/同位素近等价、互变异构及骨架禁止索引，要求与派生文件一致。增加连贯重算哈希的篡改回归测试。 |
| Morgan-only v3 加载路径未核对训练时化学引擎、RDKit 版本 | `backend/versioned_local_model.py` 对所有分支执行相同版本合同校验，防止改变特征语义却仍引用旧模型版本。 |
| v3 可以加载来自不同数据/划分/种子的评价，或将双类但全空指标视为完成 | 绑定评价 schema、完成状态、开发角色、数据版本、split SHA、seed 和四端点合同；检查类别计数和指标是否可定义。单类 AUROC/AP 保留 null，无观测任务所有指标保留 null，不构造指标。 |
| 更新验收代码后，旧训练源码 SHA 与当前源码不同 | 新增 `backend/training_provenance.py`；四任务和多任务验收脚本的 `--training-source-ref` 按指定完整 Git 提交读取原训练 blob 字节，核对记录 SHA，另列当前运行源码 SHA 和差异。历史 Python 只读取，不执行；旧 manifest 不修改。 |
| CUDA 实验中的 sklearn 逻辑回归也被记录为 CUDA 训练 | 两组实验分别记录实际预测头与冻结编码器设备；B0/B1/C_Morgan 拟合为 CPU，MLP 为 CUDA。最终追加分支级 `featureExtractionDevice`、`encoderLoadDevice`、`featureExtractionMode`：Morgan 不继承共享编码器设备，校验缓存命中不声称本轮重算。已完成工件保持原字节，记录限制见下文。 |
| 四任务验收只把重算指标与汇总比较，未同时绑定实际评价文件和模型/种子 | `scripts/verify_toxnet_v3_run.py` 同时核对真实评价与汇总指标，绑定实际 model/seed/清单/权重/版本；新增 8 项防错配测试，新旧 9 个工件分别重新通过严格验收。 |
| 缺少统一真实运行验收及 GPU 验证入口 | 新增 `scripts/verify_toxnet_integration.py`、`scripts/verify_v3_cuda.py` 及相应单元测试。输出采用新路径，失败、缺设备、权重缺失和 SHA 错误均保留明确状态。 |

预训练编码器仍固定为 `ibm-research/MoLFormer-XL-both-10pct` 的修订 `7b12d946c181a37f6012b9dc3b002275de070314`，权重 SHA256 为 `0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。运行时仅执行核验过的本地 vendored 代码；不使用 `trust_remote_code=True`、浮动 main、动态远程 Python 或自动权重下载。PyTorch 权重采用 `weights_only=True`，NumPy 缓存采用 `allow_pickle=False`。

缓存继续绑定输入顺序、完整规范结构、输入数据 SHA、编码器修订/权重/维度、化学引擎、依赖和缓存文件哈希。标准化、类别权重和参数优化只用 train。Tox21 仍是十二个独立通路任务，不替换原四端点；辅助任务消融不让辅助专属样本参与 scaler 拟合或优化。

源码未硬编码本机用户名或绝对工作区路径。局部实验检索索引会记录经过审核的本地来源根目录，因此移动原始数据目录后需要在新路径重新准备索引。该本地索引和逐条实验记录不随代码再分发。API 密钥继续留在服务端配置；本轮真实验收未调用外部供应商。

## 3. 已实际完成的验收

| 项目 | 实际结果 | 证据 |
|---|---|---|
| Python 单元、API、版本/哈希及回归测试 | **CPU 和 CUDA 环境各 326 项通过** | [validation-tests-20261013.json](artifacts/integration/validation-tests-20261013.json) |
| JavaScript 单元及工作台状态测试 | **52 项通过** | 本轮 `npm test` 运行记录 |
| Python 依赖检查 | 两套 `pip check` 通过 | 独立 v3 CPU 与 CUDA 环境 |
| JavaScript 语法检查 | `app.js`、`toxicity-core.js` 通过 | `node --check` |
| 四任务历史真实工件 | B0/B1/B2 × seeds 42/43/44，**9 次已训练运行重新验收通过** | [historical-fourtask-verification-bound.json](artifacts/integration/historical-fourtask-verification-bound.json) |
| 多任务历史真实工件 | A4/B16/C_Morgan/B16_noTox21 × seeds 42/43/44，**12 次已训练/消融运行重新验收通过** | [historical-multitask-verification.json](artifacts/integration/historical-multitask-verification.json) |
| 真实 CPU 工作台后端链路 | v2 四端点、模型信息、IG、中文报告、实验检索、B16 十二通路、错误场景和分数对应关系通过 | [runtime-cpu-20261013.json](artifacts/integration/runtime-cpu-20261013.json) |
| GPU 验收 | **真实 CUDA、冻结 MoLFormer、B16 副本更新、batch 1/2/4/8 全部通过** | [cuda-validation-20261013.json](artifacts/integration/cuda-validation-20261013.json) |
| 两组完整训练复现 | **9 个四任务 + 12 个多任务/消融工件已训练并核验** | 第 8 节两组完整指标和独立核验 JSON |
| 新 B16 CUDA 后端链路 | 真实 16 任务、独立十二通路、原 v2 和拒绝路径通过 | [runtime-cuda-20261013.json](artifacts/integration/runtime-cuda-20261013.json) |
| 真实浏览器交互 | v2 预测/归因/中文报告、JSON 导出、编辑失效和重置通过 | [browser-verification-20261013.json](artifacts/integration/browser-verification-20261013.json) |

历史工件验收重新计算原开发 validation 的真实指标，核对观察成员、模型权重与加载推理；没有把这些旧开发集合变成新盲测。B16_noTox21 保持 `trained-ablation`，其禁用通路不能通过 Tox21 服务返回学习分数。

本轮 CPU 环境实测为 Python 3.13.2、PyTorch `2.14.1+cpu`、RDKit 2026.9.1、Transformers 4.57.6。`nvidia-smi` 已识别 RTX 4060 Laptop GPU，但 CPU wheel 的 `torch.cuda.is_available()` 为 false；相应旧准备记录是 `not-tested-no-cuda-device`，原因是 CPU-only PyTorch，不能解释为物理显卡不存在。

## 4. v2 保持原样的证明

正式版本仍为 `aidd-toxnet-v2-5c22f6cc5dd3`，真实参数量 2,374,390。原 `/api/analyze` 和 `/api/local-model/info` 使用原 v2 权重进行真实推理；没有提升或替换为 v3。CPU 运行验收还确认解释前后分数一致，模型参数及参数梯度未改变。

| 受保护正式文件 | 原值与本轮实测 SHA256 |
|---|---|
| `models/aidd-toxnet/weights.pt` | `5c22f6cc5dd3e388086f6be75f559277b1dc6a3e155f9067a29b4ac0517c1398` |
| `models/aidd-toxnet/manifest.json` | `6225acc5414233756159bb1015489badb5f4bc5ba484c72367da5bac678af850` |
| `models/aidd-toxnet/evaluation.json` | `e6f60ceb3ca2d2f125e7df74ccdfabc709bf72d6f46ecb9ff49c6f3696f0e6c1` |
| `models/aidd-toxnet/split.json` | `39be266ac00d9af84f858ccdc2471c9fa13972fba326dc1838913ab942440193` |

[CPU 验收工件](artifacts/integration/runtime-cpu-20261013.json) 的 `protectedV2AndHistory` 列出全部 **10 个**受保护文件。本机旧快照全部匹配；其中仓库分发的 9 个文件与 origin/main Git blob 字节一致，未分发的 `training-progress.json` 单独与旧保护快照匹配。历史 test 预测、训练记录和外部 hERG 评价均未改写。

## 5. Windows PowerShell 复现

以下命令在项目根目录执行。已有 `.venv-v3` 和已审核本地数据的电脑可以直接从验收命令开始。首次安装使用新环境名，不覆盖 `.venv`、`.venv-v3` 或正式模型目录。所有输出采用新名称；如果名称已存在，换新名称，不删除原记录。

### 新建独立 CPU 环境

```powershell
py -3.13 -m venv .venv-integration-cpu
$toxnetIntegrationPython = '.\.venv-integration-cpu\Scripts\python.exe'
& $toxnetIntegrationPython -m pip install --upgrade pip
& $toxnetIntegrationPython -m pip install torch==2.14.1 --index-url https://download.pytorch.org/whl/cpu
& $toxnetIntegrationPython -m pip install -r requirements.txt -r requirements-model.txt -r requirements-v3.txt
& $toxnetIntegrationPython -m pip check
```

CUDA 环境、官方 wheel 与驱动匹配检查按 [CUDA_VALIDATION.md](CUDA_VALIDATION.md) 执行；不要在以上 CPU 环境中直接替换 wheel。先用 `nvidia-smi` 和实际 CUDA 张量计算确认设备，不能只检查安装包名称。

### 固定预训练权重和数据准备

显式下载固定 MoLFormer 修订，或导入手动下载的相同文件。两条方式均保留强制 SHA256 校验；国内网络失败不能切换到不明修订或随机模型。

```powershell
$toxnetIntegrationPython = '.\.venv-v3\Scripts\python.exe'
& $toxnetIntegrationPython scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
# 完全离线导入时改用这一条，并将路径替换为真实下载位置：
# & $toxnetIntegrationPython scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer --offline --local-weight 'D:\verified-downloads\model.safetensors'

& $toxnetIntegrationPython scripts\prepare_v3_data.py --data-dir data --candidate-dir data\candidates --output data\v3-development-integration-new --seed 42 --validation-fraction 0.2 --encoder-compatible molformer
$toxnetAbsentPriorDevelopment = 'data\historical-v3-development-not-distributed'
if (Test-Path -LiteralPath $toxnetAbsentPriorDevelopment) { throw '请使用不存在的历史参考路径，或提供 SHA 匹配的真实历史缓存。' }
& $toxnetIntegrationPython scripts\prepare_multitask_data.py --data-dir data --candidate-dir data\candidates --previous-development-dir $toxnetAbsentPriorDevelopment --output data\v3-multitask-integration-new --seed 42 --validation-fraction 0.2 --encoder-compatible molformer
```

准备脚本是离线审核流程，依赖原四端点真实 raw/processed 数据、审核过的 Tox21/Karim 来源和固定历史分割。缺少这些文件会明确拒绝，无假数据回退。合法获取原始来源的步骤见 [TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md) 与 [TOXNET_V3_MULTITASK.md](TOXNET_V3_MULTITASK.md)。本轮未分发未经许可的实验数据库。

`--previous-development-dir` 指固定的已曝光 v3 历史参考，不是刚生成的四任务目录。新 clone 没有该历史缓存时使用上述不存在的路径，协议记录 `unknownStructureReuse=true`、历史交集不可评估；不能据此宣称零交集或新盲测。v2 heldout 和 Karim 的严格禁入审核仍执行。已有本机固定缓存时可提供匹配历史 SHA 的实际目录，不改写历史清单。

### 测试和历史工件验收

```powershell
& $toxnetIntegrationPython -m unittest discover -s test -p 'test_*.py' -v
npm test
node --check app.js
node --check toxicity-core.js
& $toxnetIntegrationPython -m pip check

& $toxnetIntegrationPython scripts\verify_toxnet_v3_run.py --experiment artifacts\v3\baselines-20261013 --data-dir data\v3-development --snapshot models\pretrained\molformer --training-source-ref ebfa7e44f5c7c737f51b907f7f1af5ea766d1613 --output artifacts\integration\historical-fourtask-local-new.json
& $toxnetIntegrationPython scripts\verify_multitask_run.py --experiment artifacts\v3-multitask\baselines-20261013 --data-dir data\v3-multitask-audited --snapshot models\pretrained\molformer --training-source-ref bb35137fe805ac69aa1343c119386f3a9567ea43 --output artifacts\integration\historical-multitask-local-new.json
& $toxnetIntegrationPython scripts\verify_toxnet_integration.py --model-dir artifacts\v3-multitask\baselines-20261013\B16-seed-42 --encoder-dir models\pretrained\molformer --data-dir data\v3-multitask-audited --index-dir data\evidence-index --device cpu --output artifacts\integration\runtime-local-new.json
```

`--training-source-ref` 只适用于该历史实验对应的实际训练提交。新实验使用自己的源码记录；不要为解决不匹配随意指定另一个提交。若完整克隆中没有历史 commit，先显式获取该提交；严格来源核验不应被禁用。

### 冻结特征训练复现

以下 CPU 复现命令保留固定 seeds 42/43/44，只读取已有合法开发分割。本轮实际新训练采用独立 CUDA 环境；其命令见 [CUDA_VALIDATION.md](CUDA_VALIDATION.md)，结果和工件证据见第 8 节。历史 CPU 权重另外重新验收，没有被覆盖。

```powershell
& $toxnetIntegrationPython scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache artifacts\integration\fourtask-embeddings-new.npz --models B0 B1 B2 --seeds 42 43 44 --device cpu --threads 6 --epochs 40 --patience 8 --batch-size 64 --encoder-batch-size 8 --output artifacts\v3\integration-reproduction-new
& $toxnetIntegrationPython scripts\train_multitask.py --data-dir data\v3-multitask-audited --encoder-dir models\pretrained\molformer --embedding-cache artifacts\integration\multitask-embeddings-new.npz --models A4 B16 C_Morgan B16_noTox21 --seeds 42 43 44 --device cpu --threads 6 --epochs 40 --patience 8 --batch-size 64 --encoder-batch-size 8 --minimum-selection-per-class 10 --tox21-weight 0.25 --output artifacts\v3-multitask\integration-reproduction-new
```

在已通过验收的新 GPU 环境中，可将 `--device cpu` 改为 `--device cuda`，并使用新的缓存和输出路径，不能混用不同 PyTorch provenance 的旧特征缓存。完整编码器微调未实现；`--fine-tune-encoder` 会明确拒绝启动。

### B16 推理、研究接口和工作台

```powershell
& $toxnetIntegrationPython scripts\predict_multitask.py --model artifacts\v3-multitask\baselines-20261013\B16-seed-42 --encoder models\pretrained\molformer --smiles 'CCO' --tasks all --device cpu --allow-research

$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts\v3-multitask\baselines-20261013\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cpu'
$env:OMP_NUM_THREADS = '6'
$env:MKL_NUM_THREADS = '6'
& $toxnetIntegrationPython -m uvicorn backend.main:app --host 127.0.0.1 --port 4173
```

已有端口 4173 服务时，先正常停止旧服务再启动；不要同时占用该端口。浏览器打开 `http://127.0.0.1:4173/`，正式本机选择仍是 v2；完成预测后使用解释入口。B16 的研究 API 单独启用，未配置时状态接口明确不可用。

在另一个 PowerShell 窗口验证 API：

```powershell
Invoke-RestMethod http://127.0.0.1:4173/api/local-model/info
Invoke-RestMethod http://127.0.0.1:4173/api/research/tox21/status
Invoke-RestMethod http://127.0.0.1:4173/api/analyze -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO","mode":"model","provider":"local"}'
Invoke-RestMethod http://127.0.0.1:4173/api/research/tox21 -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO"}'
Invoke-RestMethod http://127.0.0.1:4173/api/research/explain -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO","model":"v2","endpoint":"herg","includeLanguage":false}'
```

接口结构和失败状态见 [MODEL_API_CONTRACT.md](MODEL_API_CONTRACT.md)。本地证据索引缺失时保留“证据不足”，语言模型未配置时保留本机中文报告，无演示推理回退。

## 6. 安全合并顺序

以下是待用户明确批准后的人工操作说明；**本轮没有执行合并、重写远端分支或 force push**。建议使用普通 merge commit，依次保留原提交祖先：

1. 审阅 PR #1，确认 head 未变化并完成所需审批，普通 merge 到 main。暂时保留其 head 分支。
2. 将 PR #2 的 base 改为 main，重新检查差异只包含多任务新增内容，确认测试后普通 merge。
3. 将 PR #3 的 base 改为 main，重新检查差异只包含解释系统新增内容，确认测试后普通 merge。
4. 将本轮集成验证 PR 的 base 从 PR #3 分支改为 main，检查只剩本轮修复与验收工具，批准后单独合并。

GitHub CLI 的具体操作示例如下，仅在各步得到授权且 head SHA 仍匹配时执行；如 head 或 main 已变化，重新审查。

```powershell
gh pr ready 1 --repo nuoxingchai-spec/aidd-blue-toxnet
gh pr merge 1 --repo nuoxingchai-spec/aidd-blue-toxnet --merge --match-head-commit ebfa7e44f5c7c737f51b907f7f1af5ea766d1613
gh pr edit 2 --repo nuoxingchai-spec/aidd-blue-toxnet --base main
gh pr diff 2 --repo nuoxingchai-spec/aidd-blue-toxnet
# 此处重新审查及验证，通过后才执行：
gh pr ready 2 --repo nuoxingchai-spec/aidd-blue-toxnet
gh pr merge 2 --repo nuoxingchai-spec/aidd-blue-toxnet --merge --match-head-commit bb35137fe805ac69aa1343c119386f3a9567ea43
gh pr edit 3 --repo nuoxingchai-spec/aidd-blue-toxnet --base main
gh pr diff 3 --repo nuoxingchai-spec/aidd-blue-toxnet
# 此处重新审查及验证，通过后才执行：
gh pr ready 3 --repo nuoxingchai-spec/aidd-blue-toxnet
gh pr merge 3 --repo nuoxingchai-spec/aidd-blue-toxnet --merge --match-head-commit c997ae774d40190002f3ec20b807d1c981f1fb4b
```

### 如果明确选择 squash

squash 后的新 main 提交不会保留原 head 祖先。仅改变 PR base 不能消除后续分支中已经压缩合并的原提交，需要把“旧 base 之后的新增提交”移植到新 main。下面只展示经过用户批准的重写步骤，不能在存在未提交修改、远端已被别人更新或未复核差异时执行。

PR #1 squash 完成后，先备份原分支，再对 PR #2 只搬迁 `ebfa7e4` 之后的提交：

```powershell
git fetch origin
git branch backup/toxnet-multitask-before-squash bb35137fe805ac69aa1343c119386f3a9567ea43
git branch backup/toxnet-explanation-before-squash c997ae774d40190002f3ec20b807d1c981f1fb4b
git rebase --onto origin/main ebfa7e44f5c7c737f51b907f7f1af5ea766d1613 codex/toxnet-v3-multitask
git diff --stat origin/main...codex/toxnet-v3-multitask
# 复核差异并重新运行测试后，且远端仍是记录的旧 head，才执行：
git push --force-with-lease=refs/heads/codex/toxnet-v3-multitask:bb35137fe805ac69aa1343c119386f3a9567ea43 origin codex/toxnet-v3-multitask
gh pr edit 2 --repo nuoxingchai-spec/aidd-blue-toxnet --base main
```

PR #2 经过重新审批 squash 完成后，再移植 PR #3 中原 `bb35137` 之后的提交。这里使用原旧 base SHA，而不是随意换成改写后的 PR #2 head：

```powershell
git fetch origin
git rebase --onto origin/main bb35137fe805ac69aa1343c119386f3a9567ea43 codex/toxnet-explainable-reports
git diff --stat origin/main...codex/toxnet-explainable-reports
# 复核差异并重新运行测试后，且远端仍是记录的旧 head，才执行：
git push --force-with-lease=refs/heads/codex/toxnet-explainable-reports:c997ae774d40190002f3ec20b807d1c981f1fb4b origin codex/toxnet-explainable-reports
gh pr edit 3 --repo nuoxingchai-spec/aidd-blue-toxnet --base main
```

PR #3 也 squash 完成后，集成分支还需要以原 `c997ae7` 为旧 base，使用 `git rebase --onto origin/main c997ae774d40190002f3ec20b807d1c981f1fb4b codex/toxnet-integration-cuda` 移植本轮新增提交，再复核和审批。若租约不满足，停止并读取新远端历史，不使用无条件 `--force`。备份分支及历史 Git blob 保留原训练来源审计所需字节；不能为方便合并改写旧模型工件内的源码 SHA。

## 7. 研究结果边界与第四轮条件

当前能够运行真实正式 v2、独立 v3 研究加载、Tox21 通路、归因、审计检索和来源分开的中文报告。CPU/CUDA 验收及浏览器交互支持前三轮代码与本轮修复进入人工合并审查；是否合并仍需用户审批。RTX 4060 已通过本文关联的真实模型测试，结论限定于已测冻结编码器和预测头工作负载。

四任务和十六任务已有真实开发 validation；完整指标和样本数分别见 [TOXNET_V3_EVALUATION.md](TOXNET_V3_EVALUATION.md)、[TOXNET_V3_MULTITASK.md](TOXNET_V3_MULTITASK.md)。它们的开发划分不同，不能直接当作相同样本比较。共同十六任务划分上，历史 B16 对 ClinTox 的 AUROC、AP 下降，Brier/ECE 上升；validation 仅有 4 个阳性，描述性负迁移应保留，小样本不确定性没有因软件验收而消失。Morgan 基线不是必然劣于预训练模型。

旧 test 和 hERG_Karim 9,978 分子的回顾性评价已曝光，只能作为 `previously exposed / retrospective` 参考，不能用于新模型优化、选择或校准。尚无新的独立盲测、前瞻评价、独立校准或已验证适用域。结构相似度不是预测置信度，IG 不是毒性因果机制；CUDA 或训练通过均不构成科学有效性、临床或生产发布认证。

第四轮可以开始**受控研究工程原型**：复用任务合同、版本化工件、合法分割、事实/证据报告结构和独立研究路由，探索生成式 AIDD 的模型适配和证据约束。完整 TxGemma 垂类模型训练及正式发布尚需另外满足：

- 固定具体 TxGemma 模型/修订，核对其模型卡、许可证、访问条件、商用及再分发条款；本轮没有核实或接入 TxGemma 权重，不能沿用 MoLFormer Apache-2.0 推定其许可。
- 审计生成任务所用化学、实验及文献资料的训练权利，明确输入/输出与真实证据合同；外部模型生成标签不变成真实实验标签。
- 实测目标模型的显存和推理/训练方法。RTX 4060 8GB 的冻结 MoLFormer 验收不能直接外推完整生成模型微调能力；量化/LoRA/服务化方案需要自己的实测。
- 建立生成内容事实性、引用真实性、任务一致性、分子合法性和新独立泛化评估。机制结论仍需真实实验及经过核验的文献，不由语言模型自行认证。

这些工程和科学条件应分别记录；v3 或后续原型继续保持 `formalAvailability=false`，未经独立验证和明确批准不自动部署为正式模型。

## 8. 完整冻结实验的真实复现结果

本轮重新完成第一组 9 个与第二组 12 个工件，其中 3 个零通路损失工件为 `trained-ablation`，不是完整十二通路模型。没有覆盖历史 CPU 工件。冻结 MoLFormer 特征提取分别记录 15.041 秒和 21.103 秒；两组 MLP 实际使用 CUDA，B0/B1/C_Morgan 的 sklearn 优化实际使用 CPU，训练清单已单独记录 `device` 和 `featureExtractionDevice`。这些时长不是稳定性能基准。

各模型均保留三个固定种子，特征变换/类别权重只拟合 train，checkpoint 只用合法开发 validation。完整逐种子、全部端点、标准差、阳性数和参数/文件版本见两组 `experiment.json`；下表只列原四端点的三种子均值。两组开发划分不同，禁止跨表直接当作同一验证集比较。

本轮实际训练源码已保存在完整提交 `998afbf0a9cea32cb11b7f33377b7d40cff59032`，其 Git blob 与 21 个新工件中的 141 条训练源码哈希一致。最终审查之后只修正后续设备记录和验收绑定，没有重写已训练工件。已生成清单中 `training.device` 准确记录预测头设备；其旧 `featureExtractionDevice` 是整组共享编码器提取设备，Morgan-only 清单也可能保留该值，不能据此声称 Morgan 在 GPU 提取或拟合。后续版本改为分支级字段，并区分校验缓存读取和本轮计算。全部 42 个历史/新工件的当前字节及不可变训练来源另见 [training-source-audit-20261013.json](artifacts/integration/training-source-audit-20261013.json)。

### 第一组 B0/B1/B2

数据版本 `aidd-toxnet-v3-development-c1d448002df99aa3`；split SHA256 `7701f6c3d8b69a7b1ed918e1365d39e79726414ce2898dca157f7a42134cbc91`。
[完整指标](artifacts/integration/fourtask-cuda-20261013/experiment.json)，[独立工件/指标核验](artifacts/integration/fourtask-cuda-verification-bound-20261013.json)。

| 模型 | 端点 | n / 阳性 | AUROC 均值 | AP 均值 | Brier 均值 | ECE10 均值 |
|---|---|---:|---:|---:|---:|---:|
| B0 | herg | 97 / 67 | 0.748756 | 0.841513 | 0.198686 | 0.202320 |
| B0 | dili | 55 / 26 | 0.842175 | 0.862051 | 0.186422 | 0.160103 |
| B0 | ames | 772 / 475 | 0.764905 | 0.826583 | 0.206911 | 0.125184 |
| B0 | clintox | 176 / 12 | 0.713415 | 0.132158 | 0.066361 | 0.059724 |
| B1 | herg | 97 / 67 | 0.784080 | 0.886792 | 0.255421 | 0.273700 |
| B1 | dili | 55 / 26 | 0.754642 | 0.775224 | 0.317043 | 0.321863 |
| B1 | ames | 772 / 475 | 0.703909 | 0.790231 | 0.258751 | 0.203862 |
| B1 | clintox | 176 / 12 | 0.715447 | 0.288773 | 0.059471 | 0.058842 |
| B2 | herg | 97 / 67 | 0.795522 | 0.892150 | 0.173353 | 0.098952 |
| B2 | dili | 55 / 26 | 0.816092 | 0.822947 | 0.223075 | 0.225915 |
| B2 | ames | 772 / 475 | 0.775888 | 0.833236 | 0.193596 | 0.093689 |
| B2 | clintox | 176 / 12 | 0.732215 | 0.305182 | 0.058749 | 0.042837 |
### 第二组 A4/B16/Morgan/零通路消融

数据版本 `aidd-toxnet-16task-development-aab05727cde8e47b`；split SHA256 `df0dfa7195f714df81e93f3795d1623c81d1d5a32433a59388421cf7d2b082f8`。
[完整指标](artifacts/integration/multitask-cuda-20261013/experiment.json)，[独立工件/指标核验](artifacts/integration/multitask-cuda-verification-20261013.json)。

| 模型 | 端点 | n / 阳性 | AUROC 均值 | AP 均值 | Brier 均值 | ECE10 均值 |
|---|---|---:|---:|---:|---:|---:|
| A4 | herg | 111 / 89 | 0.771876 | 0.905918 | 0.181957 | 0.203102 |
| A4 | dili | 26 / 16 | 0.927083 | 0.961769 | 0.119333 | 0.140383 |
| A4 | ames | 634 / 444 | 0.790442 | 0.887265 | 0.167258 | 0.083759 |
| A4 | clintox | 81 / 4 | 0.863636 | 0.471599 | 0.049170 | 0.073914 |
| B16 | herg | 111 / 89 | 0.779877 | 0.909528 | 0.160381 | 0.162585 |
| B16 | dili | 26 / 16 | 0.929167 | 0.962231 | 0.115835 | 0.147057 |
| B16 | ames | 634 / 444 | 0.791738 | 0.888882 | 0.166815 | 0.083801 |
| B16 | clintox | 81 / 4 | 0.863636 | 0.309444 | 0.059918 | 0.082023 |
| C_Morgan | herg | 111 / 89 | 0.763534 | 0.895367 | 0.144179 | 0.149220 |
| C_Morgan | dili | 26 / 16 | 0.868750 | 0.928061 | 0.147111 | 0.077189 |
| C_Morgan | ames | 634 / 444 | 0.803497 | 0.898918 | 0.182049 | 0.120580 |
| C_Morgan | clintox | 81 / 4 | 0.844156 | 0.221983 | 0.046082 | 0.034848 |
| B16_noTox21 | herg | 111 / 89 | 0.771876 | 0.905918 | 0.181957 | 0.203102 |
| B16_noTox21 | dili | 26 / 16 | 0.927083 | 0.961769 | 0.119333 | 0.140383 |
| B16_noTox21 | ames | 634 / 444 | 0.790442 | 0.887265 | 0.167258 | 0.083759 |
| B16_noTox21 | clintox | 81 / 4 | 0.863636 | 0.471599 | 0.049170 | 0.073914 |

### 新第二组的负迁移与小样本限制

B16−A4 在相同原四任务 validation 标签上的逐种子配对描述性差异如下。AUROC/AP 下降、Brier/ECE 上升是 adverse 方向；没有显著性或因果结论。

| 端点 | ΔAUROC | ΔAP | ΔBrier | ΔECE10 |
|---|---:|---:|---:|---:|
| herg | +0.008001 | +0.003610 | -0.021576 | -0.040517 |
| dili | +0.002083 | +0.000462 | -0.003498 | +0.006674 |
| ames | +0.001296 | +0.001617 | -0.000443 | +0.000043 |
| clintox | -0.000000 | -0.162155 | +0.010747 | +0.008109 |

第二组 ClinTox validation 81 分子仅 4 个阳性，未达到预注册 10/类条件，因此不驱动 checkpoint；新 B16 的 AP 均值相对 A4 下降约 0.162155，Brier/ECE 上升，AUROC 的差值约为浮点零。第一组 ClinTox 是 176 分子/12 阳性，仍属于少样本端点，不能把较高内部指标当独立可靠性。GPU 再训练不保证改善旧端点，也不能把历史 CPU 与本轮 CUDA checkpoint 差异归因于硬件本身。

三种子的零辅助损失消融仍保留独立工件和禁用任务状态；未混合成临床毒性概率。模型全部为 `formalAvailability=false`、未校准且适用域未验证。旧 test 和 Karim 的标签没有用于任何新的拟合/选择/校准。没有新的独立盲测、前瞻评价或全量编码器微调。

实际新 B16 CUDA 运行验收 [runtime-cuda-20261013.json](artifacts/integration/runtime-cuda-20261013.json) 通过：16 个真实任务、12 条独立通路、v2 的真实 CPU 四端点/归因、报告和拒绝路径均核验。原 v2 的 10 个保护文件未改，其中 9 个与 origin/main 字节一致，未公开进度文件依据原保护快照核验。

## 9. 本轮修改文件与数据流程

数据流为原始来源及固定历史分割 → 多层结构隔离审核 → 合法 train/validation → 固定修订 MoLFormer/Morgan 特征 → train-only 预处理与预测头拟合 → 开发 validation → 独立版本工件 → 研究加载/API → 真实预测、归因和证据报告。旧 test/Karim 仅用于禁止重叠和历史审计，不参与新模型选择。

| 范围 | 修改/新增文件 |
|---|---|
| 数据和模型加载修复 | `backend/v3_data.py`、`backend/versioned_local_model.py` |
| 训练设备记录 | `backend/v3_experiments.py`、`backend/multitask_experiments.py` |
| 不可变训练源码核验 | `backend/training_provenance.py`、`scripts/verify_toxnet_v3_run.py`、`scripts/verify_multitask_run.py` |
| 真实 CUDA 和集成验收入口 | `scripts/verify_v3_cuda.py`、`scripts/verify_toxnet_integration.py` |
| 新增/加强回归 | `test/test_v3_data.py`、`test/test_versioned_local_model.py`、`test/test_v3_experiments.py`、`test/test_multitask_experiments.py`、`test/test_training_provenance.py`、`test/test_training_device_provenance.py`、`test/test_verify_toxnet_v3_run.py`、`test/test_v3_cuda.py`、`test/test_toxnet_integration.py` |
| 文档及提交范围 | `README.md`、`CUDA_VALIDATION.md`、`TOXNET_V3_INTEGRATION.md`、`.gitignore`、`.gitattributes` |
| 聚合验收证据 | `artifacts/integration/` 中明确白名单的验收 JSON、两组 `experiment.json` 和可移植依赖清单 |

Git 仅提交聚合指标、计数、版本、哈希及诊断。新研究权重、分子/标签行、embedding 缓存、原始来源、临时日志、安装包、浏览器下载和 API 密钥均不在本轮提交范围。完整可加载研究权重保存在本机两个独立训练输出目录；新 clone 需按文档合法准备数据并复现。
