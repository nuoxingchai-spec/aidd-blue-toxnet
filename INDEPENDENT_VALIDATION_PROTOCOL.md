# 独立科研验证协议 v1

当前状态：**独立科学验收未完成**。没有合法且真正独立的新测试数据，因此没有新盲测指标或正式可用声明。所有 v3 保持 `formalAvailability=false`。

## 冻结范围与访问边界

PR #5 已用普通双父 merge 合并；基线 main 为 `3a7e5138398f8eea5f07ae14a083135bbc94d26b`。研究分支 `feat/toxnet-independent-validation` 只增加评测基础设施，不修改 v2、B16、MoLFormer、旧 split 或历史结果。

协议分成四个权限层：操作者登记本机候选；标签盲审计结构与来源；经审核者明确许可封存独立评价或校准集；冻结预测完成后才读取获准的评价标签。浏览器只能发送登记 ID，不能发送路径、文件、标签、模型目录、超参数或训练请求。科研评价默认禁止标签访问，且独立 CPU 子进程不会共享 Compare GPU 调度。

`backend/independent_validation_data.py` 提供严格 `CandidateMetadata`、`IndependentProtocol`、`audit_dataset`、`seal_dataset`、`load_sealed_dataset`。候选行仅为 `smiles/sourceRowId`；禁止混入标签与可穷举的逐行标签哈希。真实标签使用独立私有文件与整文件 SHA256。封存只允许已通过审核、时间顺序一致的 frozen 协议，不允许覆盖已有目录。封存中的文件角色使用固定文件名，防止标签伪装为元数据。

资格需要完整历史参考、精确与 canonical 结构、去盐母体、立体/同位素近等价、互变异构体、骨架及来源排除。来源审查涵盖 sourceId、publication/cohort/assay/record 身份；未知来源不能靠结构不重叠取得独立资格。当前保守 v1 将无环骨架统一分组，可能排除许多无环分子；不能在看过结果后放宽该规则。MoLFormer 大规模预训练结构曝光仍未知，实验标签独立性与预训练结构独立性分别说明。

## 固定评价

首个评价冻结 A4/B16/C_Morgan seed42、实际 weights/manifest/task-contract SHA、候选版本、成员、参考集、身份策略、RDKit 版本、评测源码哈希和统计设置。`frozen_evaluation_settings()` 固定 AUROC/AP/Brier/ECE10Bins、500 次骨架簇 bootstrap、每类至少10及至少10簇的区间支持规则。点估计与区间可计算状态分别返回；单类别 AUROC 为不可计算，小样本区间为 `insufficient-support`，不填0或1。

`evaluator_source_sha256()` 固定 metrics、predictor 和 evaluator 脚本的文件字节摘要。源代码或设置变化时拒绝使用旧冻结协议；不能先看标签再修改源码并声称初次盲测。执行设备/batch 会记录并加入曝光账本。模型加载复用已有 SHA 审核与特征分支，失败成员保留在覆盖率分母中。所有模型采用相同可比较成员与正类定义，Tox21 单独评价，不合成临床风险分数。

曝光账本在预测/标签访问前保守登记。中断仍留下曝光记录；同协议复现标记 `previously-evaluated-same-frozen-protocol`。不同模型、协议、设置不能通过换输出文件名取得新盲测声明。本机账本及操作者审核是研究防护，并非不可篡改法律证明；更强盲测需要外部数据保管人与权限隔离。

## Windows 11 启动与审计

在仓库根目录执行，已有环境无需重新安装；下列流程不会下载模型或调用收费 API：

```powershell
$py = (Resolve-Path '.venv-v3\Scripts\python.exe').Path
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:TOXNET_VALIDATION_ALLOW_LABELS = 'false'
& $py -m uvicorn backend.main:app --host 127.0.0.1 --port 4185 --workers 1
# 浏览器：http://127.0.0.1:4185/validation
```

端口已有工作台时复用或先正常关闭原服务，不启动第二个 Compare 数据库所有者。科研默认目录为 `%LOCALAPPDATA%\AIDD-Blue-ToxNet\scientific-validation`，服务端 ACL 限制当前用户/SYSTEM；不放 OneDrive，不公开原始行。

```powershell
# 只读结构/来源，不评价历史标签；输出必须是新文件
& $py scripts/audit_independent_dataset.py --output artifacts/independent-validation/local-new-audit.json
$body = @{ datasetId='historical-reference-inventory' } | ConvertTo-Json
$ack = Invoke-RestMethod http://127.0.0.1:4185/api/research/validation/audit -Method Post -ContentType 'application/json' -Body $body
Invoke-RestMethod "http://127.0.0.1:4185/api/research/validation/runs/$($ack.runId)"
# 只读报告完成后可导出；取消：同一 runs/{runId} 用 -Method Delete
```

完整CPU研究环境需要 requirements.txt、requirements-model.txt、requirements-v3.txt 与 requirements-dev.txt；基础 `.venv` 未安装可选Transformers时，不能执行所有v3/TxGemma测试。新环境应先按既有v3训练文档安装固定CPU torch及这些依赖，不下载模型权重。缺原始缓存/固定哈希不符时输出依赖缺失并拒绝资格；网络恢复也不自动下载大型数据。研究候选从本地合法来源准备，在数据目录内写 reviewed 元数据、严格 label-blind JSONL 和 frozen 协议。引用与许可证据必须由实际审核获得，不能复制测试 fixture 当真实授权。

```powershell
$dataRoot = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation'
# 文件由操作者合法准备；命令中的ID/版本须与reviewed元数据相符
& $py scripts/register_independent_dataset.py --data-root $dataRoot --dataset-id new-audited-study --name '已审查独立研究' --version v1 --metadata new-study/metadata.json --rows new-study/rows.jsonl --protocol new-study/protocol.json
& $py scripts/audit_independent_dataset.py --metadata "$dataRoot/new-study/metadata.json" --rows "$dataRoot/new-study/rows.jsonl" --protocol "$dataRoot/new-study/protocol.json" --output artifacts/independent-validation/candidate-audit.json
```

登记不会授予数据权利或自动封存。通过审计后才由操作者使用 `seal_dataset(audit_result, new_directory, labels=authorized_labels, label_read_permission=True)`，记录返回 manifest SHA；标签不经浏览器。将该封存清单通过登记 CLI 的 `--sealed-manifest/--manifest-sha256` 绑定到对应登记对象；登记不可覆盖，应采用新版本ID。只有审核时间早于协议冻结且许可完整才可能成功。全套字段定义以严格 Pydantic 类为准。

## 后端合同

`aidd.scientific-validation.v1`，同源 JSON；额外字段拒绝。

| 路由 | 含义 |
|---|---|
| GET `/api/research/validation/datasets` | 真实登记、缺失依赖、模型文件状态与科学限制 |
| POST `/api/research/validation/audit` | `{ "datasetId": "historical-reference-inventory" }`，202/运行ID |
| POST `/api/research/validation/evaluate` | `{ "datasetId": "new-audited-study", "protocolId": "reviewed-protocol-v1", "modelIds": ["A4","B16","C_Morgan"], "seed": 42 }` |
| GET `/api/research/validation/runs` | 最多100条聚合运行元数据，不回放模型 |
| GET `/api/research/validation/runs/{runId}` | pending/running/completed/failed/cancelled；重启旧执行只读为 interrupted |
| DELETE `/api/research/validation/runs/{runId}` | 幂等取消，等拥有的CPU进程清理确认 |
| GET `/api/research/validation/reports/{runId}` | 完成报告，重核结果SHA；不公开原始结构、标签、密钥或私有路径 |

评价还要求服务器 `TOXNET_VALIDATION_ALLOW_LABELS=true` 与合格 independent-evaluation seal；开启该开关不会替代权限、独立性或固定协议核验。完整RDKit预检查通过线程执行，避免阻塞API的状态和取消请求；随后隔离CPU worker重复核验。默认403禁止标签；无资格409；无报告409；SHA损坏503；非法字段422；队列满429。

并发CPU worker为1，队列最多4，超时600秒；先停止本服务拥有的进程，再公布终态和释放名额。不能确认清理或遇到重启前未知工作进程时持久 quarantine；不自动清除。需要操作者核对进程、备份聚合记录并采取明确维护处理。磁盘写失败可能留下running记录，重启保守阻断，不擅自重跑或读标签。聚合历史保留在保护目录，当前最多显示100条；没有自动删历史/自动续跑功能。

## 获准后离线评价

目前不执行下面的评价，因为没有合格数据。真实模型路径必须存在，未知模型不能被软件 fixture 替代。

```powershell
& $py scripts/evaluate_independent_models.py --sealed-dataset "$dataRoot/new-study/seal/seal.json" --manifest-sha256 '<实际seal文件SHA256>' --models-root artifacts/integration/multitask-cuda-20261013 --encoder-dir models/pretrained/molformer --device cpu --output artifacts/independent-validation/new-independent-evaluation.json
```

RTX4060的CUDA环境可使用 `.venv-v3-cuda\Scripts\python.exe` 检查 `torch.cuda.is_available()`；离线CLI可明确传 `--device cuda`，不要与Compare/Ollama同时占GPU。网页科研worker固定CPU。评测不会完整训练、调整超参数、拟合校准或修改模型。无权重/缺依赖/显存不足是失败，不用模拟指标替代。

## 验证与后续

```powershell
& $py -m unittest discover -s test -p 'test_*.py' -v
npm test
& $py -m pip check
& $py scripts/verify_compare_preservation.py
```

优先获得任务定义一致、来源独立且获准本地研究的真实新实验，再完成来源/结构排除与外部保管封存。新增校准需要另一个隔离的 calibration 角色；校准测试和独立评价不能混用。详细限制、真实开发分析与法律待办分别见科学限制、模型比较和许可审查文档。
