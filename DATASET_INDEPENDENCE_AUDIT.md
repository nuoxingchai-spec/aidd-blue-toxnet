# 独立验证数据资格与结构、来源审计

本模块为独立验证建立准入和封存合同，不下载数据、不训练或选择模型、不运行推理、不利用旧 test 或 hERG_Karim 标签。当前本机存在历史研究数据缓存，但**没有已核验、已封存的新独立测试集**。数据门户不同、结构没有精确重复、或已有清洗脚本成功，都不能自动证明实验总体独立、数据权利已经解决或模型准确率提升。

## 当前真实依赖与候选角色

六份原始 TDC 数据文件的字节 SHA256 已与既有代码固定值核对一致：四端点 hERG、DILI、Ames、ClinTox，以及 hERG_Karim 和 Tox21。原标签文件、四任务和十六任务开发集、证据索引继续保留，不由本模块写入。

| 本机来源 | 可确认的现有事实 | 新独立验证资格 |
|---|---|---|
| 原四端点 TDC | `data/raw` 与处理清单存在；全部已划分成员来自历史研究来源 | 历史 train/validation/test 都是排除参考，不重新命名为新盲测 |
| hERG_Karim | 原始 13,445 行；9,978 分子回顾性池已经评价并曝光 | 不得训练、调参、校准或声称全新盲测；10 μM 标签定义不自动等价原 hERG |
| Tox21 | 历史清洗 7,822 个结构、77,853 条观察；十二独立通路；B16 已用于开发 | 即使挑出未进入 B16 的结构，也不自动变成独立来源；本模块保守排除整个已审计源结构 |
| FDA DILIrank 2.0 / DILIst | 本机官方 XLSX 和来源元数据存在；1,336 / 1,279 个药物记录；无 SMILES 列，结构映射尚未开展 | 只可来源审计；不能猜药物名对应结构。与 TDC DILI 的 FDA/NCTR 药物证据总体可能交叉 |
| 本机实验检索索引 | 来自以上历史来源的派生索引 | 不是新数据库，不形成独立实验观察 |
| 新 ChEMBL / PubChem 等来源 | 未在本模块自动下载或接入 | 需固定版本、实验语义、出处、权利和暴露历史后才可审核；门户名称本身不是独立性证据 |

TDC 文档中的 `Not Specified` / CC BY 链接尚未解决底层文件的全部权利；TDC 的 MIT 软件许可不等于实验数据许可。FDA 页面公开下载也不能证明所有第三方内容允许商业再分发。历史协议维持 `research-only-source-rights-unresolved` 与 `commercialRedistributionApproved=false`。本模块不会把这些旧状态升级为审核批准。

## 必须重用的身份和历史排除

复用 `backend.v3_data.leakage_identity/identity_tokens/build_forbidden_index`，保持 `IDENTITY_VERSION`、化学版本、RDKit 版本和原身份政策；不修改历史实现。输入身份仍用严格 canonical isomeric SMILES，保留盐、立体、同位素和电荷；以下处理只用于保守排除：

- canonical、去立体/同位素等价结构、最大有机母体中和、母体互变异构规范化。
- 原结构、母体、互变母体的 Bemis–Murcko 骨架；所有无环结构同属 `[ACYCLIC]`，不为增加样本而放宽。
- 跨候选的身份连接分量不可拆分；任一成员与历史参考重叠，相关候选分量一并隔离。
- 同 canonical 的多行不挑选一个标签，全部隔离为 `duplicate-or-conflicting-canonical-structure`；不读取标签来决定保留哪行。

历史参考包含 v2 全部 train/validation/test、v3 四任务及十六任务开发 train/validation、已曝光 Karim 池和已审计 Tox21 源。清单文件与成员结构整体摘要均有固定 SHA。不能修改 manifest 并重新计算派生索引，就宣称历史排除已经完整。缺少任一必要文件、SHA 不匹配或版本不兼容时，`completeReferences=false` 或明确报错，不能合格。

`build_historical_references(root)` 仅投影 split 的结构成员和 CSV 的 `smiles` 列。历史 JSON 需要被解码，但标签字段不被取值、转换、统计、返回或用于任何选择；CSV 标签列同样忽略。`refs.receipt` 仅包含角色/文件摘要、结构数量、版本和缺失原因，不含结构字符串、标签或本机路径。

来源审核另比对 sourceId、family、publication、cohort、assay；DOI URL 与 DOI 文本统一比较。结构互斥不能抵消同来源家族、同文献、同队列或同实验的交叉。MoLFormer 预训练结构暴露无法在当前缓存中核查，明确 `pretrainingStructureExposure=unknown`，不宣称这些分子从未被预训练编码器见过。

## 严格的操作员本机合同

所有候选与协议为 Pydantic 严格合同，额外字段和类型强制转换拒绝。浏览器只选择服务端注册 ID；不提交路径、原始数据库行或标签。CLI 路径由本机操作员提供，与浏览器输入边界不同。

结构输入为 JSONL，每行只允许 `smiles` 和 `sourceRowId`。明文 `label/labels`、逐记录 `observedLabelHashes`、路径、工具参数等一律拒绝。二元标签的逐条 hash 很容易枚举，不能当作安全的“盲化”；本模块不保存或返回这些值。

`CandidateMetadata`（`aidd.independent.candidate.v1`）包括：

| 字段 | 用途 |
|---|---|
| `datasetId/datasetVersion/sourceId/sourceVersion/sourceUrl` | 固定数据身份与无凭据 HTTPS 来源引用；不执行 URL 下载 |
| `retrievedAtUtc/originalSampleCount` | 已知的真实下载 UTC 时间与原始样本数；可为 null 表示尚无证据，不能用清洗或审计时间替代下载时间 |
| `rawSha256` | 原始来源文件的字节摘要，由操作员源文件审核记录 |
| `rowsSha256` | 完整结构输入列表的规范 JSON SHA，绑定实际成员，不能换另一份结构文件 |
| `labelsSha256` | 整份 `{structureId:{task:0或1}}` 标签映射的规范 JSON 内容摘要；不得使用逐标签摘要。由有权数据保管方固定，不需要标签进入审计输入 |
| `lineage` | `families/publicationIds/cohortIds/assayIds`，不能只写门户名 |
| `role` | `independent-evaluation/calibration/audit-only`，不能混用 |
| `taskContractVersion/taskContracts` | 各任务的正类、实验类型、物种、单位、阈值、定义及明确审核到已训练任务的 `trainedTaskContractSha256`；补充负类与实验条件可为 null，不能推测补齐；`comparisonApproved` 与证据 SHA 必填才可合格 |
| `rights` | 明确允许本机研究的角色、商业使用/再分发/外传许可、审核人/时间和证据 SHA；商业、再分发、外传默认 false，不随本机研究准入自动开放；未知或拒绝均不合格 |
| `independence` | 总体独立性审核状态、证据 SHA、审核人/时间；不能由结构互斥自动置 true |
| `exposure` | 标签已查看、用于训练、用于选择、来源已评价，任一为 true 都不能升级为新独立验证 |

`IndependentProtocol`（`aidd.independent.protocol.v1`）固定协议 ID/版本/角色、`status=frozen` 与 UTC 冻结时间、候选 metadata 和历史参考 SHA、任务合同版本、`approvedTaskContractHashes`、各比较模型的 ID/版本/权重 SHA/清单 SHA/任务合同，以及 `labelReadPermission`。模型和任务不是根据新标签成绩选择。默认禁止训练和模型选择，不能仅凭任务名称自动把不同阈值视为同一端点。

协议还必须在任何标签访问前固定 `evaluationSettings` 和 `evaluatorSourceSha256`。缺少其中任一字段，审计返回 `not-independently-qualified`，不采用 CLI 或当前代码的默认值来补齐旧协议：

```json
{
  "version": "aidd.independent.evaluation-settings.v1",
  "metrics": ["auroc", "averagePrecision", "brier", "ece10Bins"],
  "bootstrapReplicates": 500,
  "bootstrapUnit": "scaffold-cluster",
  "minimumBootstrapPerClass": 10,
  "minimumBootstrapGroups": 10,
  "seed": 42
}
```

这是当前 v1 预先固定的分析合同；500 个重采样、按骨架连接簇抽样、每类至少 10 个样本与至少 10 个簇是区间估计保护条件，达不到时不能伪造区间。不同统计方法需要新的经审核协议，不能读完测试标签后改设定。`backend.dataset_overlap_audit.evaluator_source_sha256()` 对实际 `scripts/evaluate_independent_models.py`、`backend/independent_validation_metrics.py`、`backend/validation_predictors.py` 的字节 SHA 映射计算固定摘要，不导入模型依赖。任一评估源文件变化即拒绝原封存的评价加载，包括默认只读结构加载；新代码不能自动获得旧盲测标签许可。该摘要是本机版本绑定，不是第三方代码可信认证。

权利和总体独立性是**记录的人类证据审核**，不是程序法律或科学认证。摘要只绑定所声称的证据，不会自动证明一份文件支持该结论；操作员仍需核查原文件内容、适用范围和真实权限。证据缺失、未冻结、已曝光、来源相交、结构相交或历史参考不全，均返回 `not-independently-qualified`，不能用演示值补成可用数据。

## 审计、标签分离与封存

稳定入口位于 `backend/independent_validation_data.py`：

```python
audit_dataset(rows, metadata, references, protocol)
seal_dataset(audit_result, output_dir, *, labels=None, label_read_permission=False)
load_sealed_dataset(path, expected_sha256, purpose='evaluate', allow_labels=False)
```

`audit_dataset` 返回内部结构投影与 `publicAudit`；服务只公开后者。公共报告只含计数、排除原因、来源交集、合同摘要和限制，没有原始行、结构或标签。`eligibleStructureCount` 仅表示通过结构层筛选的数量，`qualified=false` 时仍不允许评价或封存。审计不打开标签，也不自动封存。

未读标签时，公共 `positiveCount/negativeCount/missingLabelCount` 明示为 null，`labelCountsStatus=not-read-labelblind-audit`。独立评价阶段经过授权才能依据真实观察统计正负样本和缺失标签；不能为填齐工作台而读取旧 test 标签。原始行数可由结构源记录计数得到，不检查标签列数值。FDA inventory 逐份核对本机原始 XLSX 的固定 SHA，而非把来源元数据中的 hash 文本当作文件已验证。

`seal_dataset` 需合格且未改变的审计、协议内明确标签权限，以及本次 `label_read_permission=True`。它重新核对原结构审计，验证独立标签映射与预先固定摘要，然后在全新、当前用户私有的目录写 `structures.jsonl`、`labels.private.json`、metadata、protocol、聚合 audit 和 `seal.json`。不覆盖旧目录；失败留下的不完整目录没有完成清单，不能加载。标签封存是已授权数据处理，不授予公开数据库、模型选择或云端传输许可。

加载时调用者必须提供预先审核的 `seal.json` 字节 SHA。默认 `allow_labels=False` 完全不打开或 hash 标签文件，结构行也不含标签或可枚举的逐记录标签摘要。只有严格布尔 True，且清单和实际协议事先批准相应目的时，才读取标签。所有非标签工件的 SHA、实际协议与权利时间、评估源码和设定、模型任务绑定、来源资格、化学身份和分组都在标签文件任何字节或 hash 访问之前核验，不能只根据清单的权限副本提前 hash 标签。JSON 重复键、NaN/Infinity、符号链接和目录逃逸被拒绝。`independent-evaluation` 只能 `purpose=evaluate`，`calibration` 只能 `purpose=calibrate`，不能混用。缺失标签以字典中缺少任务表示，不能补为阴性。

读到标签时返回顶层 `labels={structureId:{task:0或1}}`，并为各结构行附加 `labels`，供已冻结评估使用。模型、任务、完整数据文件、来源、结构 ID 和分组 SHA 均继续核验。新测试首次使用及重复评价的暴露登记、统计和校准由独立评估模块管理；封存不等于已有实验成绩。

## Windows PowerShell：真实无标签审计

已有本机依赖即可执行，不下载模型、不使用 GPU、不发起网络或收费调用：

```powershell
Set-Location 'C:\Projects\aidd-blue-toxnet' # 替换为自己的仓库目录
$auditPython = '.\.venv\Scripts\python.exe'
& $auditPython scripts/audit_independent_dataset.py `
  --output artifacts/independent-validation/labelblind-inventory-20261015.json
```

已有同名输出时拒绝覆盖，复跑请换新文件名。真实 inventory 只读取已有固定来源、结构列及元数据；不创建独立测试标签文件、不改旧划分和权重。其结果记录于上述新聚合路径，最终数量以实际输出为准。

操作员自行依法准备了新候选资料之后，才能运行另一份独立审计：

```powershell
& $auditPython scripts/audit_independent_dataset.py `
  --metadata 'C:\PrivateResearch\candidate-metadata.json' `
  --rows 'C:\PrivateResearch\candidate-structures.jsonl' `
  --protocol 'C:\PrivateResearch\frozen-protocol.json' `
  --output artifacts/independent-validation/new-source-audit.json
```

这些文件必须真实存在且完成审核；命令不生成示例真实标签或自动批准。没有新合法独立数据时，工作完成状态为“工程审计可用，独立效能待验证”。原始来源、结构 JSONL、私有标签、封存目录和证据数据库不得提交 GitHub；公开只提交经核验的聚合审计。

## 软件验证

CPU 单元测试使用明确标注的软件夹具，覆盖合法/非法分子、结构等价与骨架交集、来源家族/文献/队列/实验交集、同结构重复隔离、缺权利/曝光/协议未冻结、文件 SHA 和合同篡改、默认不访问标签、权限在前及角色隔离。夹具不是新的真实数据或模型效果证明。

```powershell
& $auditPython -m unittest test.test_independent_validation_data -v
```

实际测试和真实 inventory 结果在交付时追加；不存在的新独立准确率、校准结果和盲测通过记录不填写。

2026-10-15 本轮实际 CPU 数据合同测试为 **23/23 通过**，包括缺少预注册统计设定、当前评估源 SHA 不匹配、实际协议拒绝标签读取、元数据资格失败时不得预先 hash 标签、将标签文件冒充 metadata 等非标签角色的替换拒绝，以及 FDA 元数据无法替代原始文件验证。v1 角色只能对应上述固定文件名，不能用另一份标签内容伪装成可提前读的工件。它们都是软件夹具检查，不是 23 份独立科学试验。历史 2026-10-14 inventory 保留，不覆盖；本轮新审计使用独立 dated 输出。

本轮真实 label-blind inventory 已执行完成：`artifacts/independent-validation/labelblind-inventory-20261015.json` 的字节 SHA256 为 `9554533148906cbabd78f83e9dd50875eca02f1f0153119480c2e4b229b6c440`，历史参考完整，标签未读取。Karim 13,445 个候选结构中，13,385 个触及历史多层排除，余下 60 个仍因已曝光来源、来源总体及权利未解决而不合格。Tox21 原始 7,831 行、7,822 个处理结构全部触及已审计历史参考，不能作为新盲测。FDA 1,336 / 1,279 个药物记录的本机原始文件 SHA 均已重新核对，但缺少审核后的结构映射、存在 DILI 来源总体交叉可能、底层权利未解决。**四份候选全部 `not-independently-qualified`，没有新增封存标签或独立准确率结果。**
