# Stage 7B 补充：ChEMBL 37 扩展无标签审计

2026-10-16（Asia/Shanghai）。本报告扩展首轮 200 条先导调查，保留首轮报告与回执。**本轮仍是元数据和独立性审计，未获准进入 Stage 7C，也没有读取候选活性结果或运行预测。** 最终数量以本轮新增机器可读聚合为准；不是对 ChEMBL 全库可用性的结论。

## 冻结的检索范围和边界

`scripts/survey_chembl_expanded_labelblind.py` 沿用原无标签投影字段，不改变首轮脚本或其历史行为。

- 固定发布：ChEMBL 37；读取官方 `status.json`，采集前后内容必须相同且 `chembl_db_version=ChEMBL_37`。在线 API 本身不是按发行号永久固定的地址；不匹配时停止。
- 目标检索：UniProt `Q12809`、`SINGLE PROTEIN` 的 target 元数据，核验唯一 `CHEMBL240`、`Homo sapiens`。本次不把其他物种、同源目标或蛋白复合物静默并入人类 KCNH2。
- 扩展选择：按 `activity_id` 升序，从 offset 0 开始，200 条一页，最多 10 页 / 2,000 条。首轮 200 条被包含在此次同一冻结范围中，不能把两次调查计数相加。
- 数据预算：每个响应最多 1 MB，所有响应合计最多 5 MB，最多 120 次请求；不是全库或完整数据库下载。达到预算、页数不符、ID 重复/乱序或总数变化时停止，不补造记录。
- 关联来源：从活动元数据提取 assay/document ID，以排序去重后的 40 个 ID 为一批，获取相同投影合同下的实验与文献元数据。缺少、替换、重复或额外分页的关联记录导致获取失败。
- 请求固定官方 HTTPS 地址，禁用环境代理和重定向；不跟随服务器 `next/previous` 链接，下一页只使用冻结的整数 offset。

私有目录中的 `plan.json` 在首次网络请求及首次活动元数据获取之前写入。计划绑定原身份策略、评估最低支持要求及 hERG 预审核策略；后者明确 `MANUAL_REVIEW_REQUIRED`，阈值、比较运算符和边界规则为 `null`。冻结一个“禁止映射、待审核”的预策略，**不等于**已批准的标签合同或评估协议。

## 实际获取范围

本轮真实获取且经原始响应重建核验：**2,000 条活动元数据、237 个 assay、161 个 document**。采集时该目标总活动元数据为 **41,078**；因此本轮扩大了调查，但仍不代表全目标或全库。原始响应合计 **810,225 bytes**，低于冻结的 5 MB 总预算。161 个文献元数据均有 DOI，年份范围 **1995—2008**；年份早于历史汇编发表只说明存在被历史来源纳入的机会，不是逐实验重复已证实。

| 获取证据 | SHA256 |
|---|---|
| 冻结计划 | `106489148e36295b95a403fcd241f25c69b46c2dbc27eb26713e7ba4504877cd` |
| 私有 bundle 原始字节 | `0d9a4acb2f2046c3b4a8b155ff47c6c6172bd002fab993e92bbc893562a306e9` |
| 私有获取回执 | `be2b9927a51128dd45c4331e6699a5ed30707ce510241c510ec229d79a9c7c2b` |
| 实际提取脚本 | `7814628ef99fbc7839736307c4a4c3052d4223a1918a5157e37499223b00aa18` |
| hERG 禁止映射预审核策略对象 | `720daf2db990068fc43f2ac70ff45a71a095ed40803a722e9bcc2030ce67ffe7` |

按**测量定义元数据**计数如下，不能据此判断结果存在、是否精确或是否阳性：

| 元数据类型 | 单位与记录数 | 科学限制 |
|---|---|---|
| IC50 | nM 795；`ucm` 3；单位缺失 32 | 未知测量方法不能默认为人类电流阻断；`ucm` 保留未审核，不改写成 μM；结果关系符未读取 |
| Ki | nM 437；单位缺失 5 | 亲和力不自动等同功能阻断 |
| Inhibition | % 424 | 未知测试浓度，不能反推 IC50 |
| Activity | % 39；nM 74；uM 42；单位缺失 14 | 泛化测量类型不能建立 hERG 二分类合同 |
| IP / Inflection point | nM 85 / 13 | 不推断与 IC50 等价 |
| EC10 / EC25 / EC50 | 3 nM / 1 uM / 3 nM + 1 单位缺失 | 分别保留定义，不合并入 IC50 |
| IC20 / IC25 | uM 6 / 6 | 浓度效应水平不同 |
| Log IC50 / pIC50 | 单位缺失 5 / 2 | 未读取数值，不还原或设阈值 |
| QT interval / Ratio Ki | 单位缺失 4 / 6 | 不视为原阻断任务 |

assay 粗分类对应记录：B=1,539、F=155、T=306；实验物种元数据对应人类 1,756、非人 29、未指定 215。非人记录为豚鼠 24、雪貂 1、犬 4。类型分类与目标映射仍不能代替具体实验方案。

来源标识 `src_id=1` 对应全部 2,000 行，但一个整数来源标识不足以证明实验独立性。161 个 DOI 中包含计算/汇编研究，例如 [QSAR affinity prediction, 2003](https://doi.org/10.1016/s0960-894x(03)00492-x)、[CoMSiA/homology modeling, 2003](https://doi.org/10.1016/s0960-894x(03)00196-3)、[SVM discriminant model, 2005](https://doi.org/10.1016/j.bmcl.2005.03.080)。这里只读取官方文献元数据，没有打开这些候选文献中的活性表；这几个例子说明 DOI 不足以认证原始实验，不能认定 161 个 document 就是 161 个新的独立实验。

## 实际分阶段结构排除结果

本轮[机器可读完整审计](artifacts/independent-validation/stage7b-supplement-20261016/chembl-expanded-audit.json)保留两个明确不同的参考范围。首轮及 Stage 6 的固定历史合同没有被改写；新增完整源检查是**追加排除**，不能把新结果回填到旧回执。

全部 2,000 条记录均能规范为有效结构，缺失和非法结构记录均为 0；唯一 canonical 与唯一 molecule ID 各为 1,638。206 个重复 canonical 组涉及 568 行；分子 ID 重复统计同为 206 组 / 568 行。重复计数包括已经历史重叠的行，不能再次全量扣除。

| 阶段 / 唯一结构 | 原 Stage 6 固定参考范围 | 加入完整历史 Karim/Tox21 源后 |
|---|---:|---:|
| 输入 canonical 唯一结构 | 1,638 | 1,638 |
| 历史任一结构层级直接重叠 | 1,267 | 1,273 |
| 只经候选连通分组追加污染 | 0 | 0 |
| 对可用参考观察为结构互斥 | 371 | 365 |
| 观察为结构与重复筛查通过 | 325 | **319（临时观察数）** |
| 完整历史范围下可核验的通过数 | 此列仅代表旧固定范围 | **`null`：历史存在未解析结构** |
| 通过全部科学独立资格 | 0 | **0** |

新增历史源检查只读取原始文件中的结构列：Karim `Drug`、Tox21 `X`，不检查其标签。Karim SHA 固定源有 13,445 行，全部可规范、唯一结构 13,445；Tox21 原始源有 7,831 行，7,822 行可规范且唯一，**9 行结构缺失或不支持**。7,822 个有效 Tox21 结构摘要与原已审源一致，但不能把未解析的 9 行静默视为不存在，故追加完整历史检查的 `complete=false`。

加入可用完整历史源后，另发现 6 个唯一结构的历史重叠；因此 325 减为 319。**319 不是“已核验通过的独立结构”，不是合格样本数，也不是允许盲测的队列。** 对未解析历史结构、来源、任务、许可与隔离尚有阻塞，`verifiedStructureAndDuplicatePasses=null`、`independentlyQualifiedStructures=0`。

按优先级形成的**互斥记录处置**如下，每列各自合计为 2,000，不能与非互斥原因频数混加：

| 记录首要处置 | 原固定参考 | 追加完整历史源后的观察 |
|---|---:|---:|
| 历史结构 / 骨架重叠 | 1,579 | 1,585 |
| 剩余候选重复，全部隔离 | 96 | 96 |
| 剩余记录元数据未解决 | 35 | 并入下项，未放行 |
| 剩余待任务、来源、权利及保管审核 | 290 | 319 |

追加历史范围的非互斥层级命中数为 canonical 1,050、去立体/同位素近等价 1,142、去盐中和母体 1,145、互变异构母体 1,169、骨架 1,273。这些集合相交，不能相加。固定参考范围的 244 行未建立人类 assay / 直接目标映射、69 行测量定义缺少单位；这些原因也可能与历史重叠、重复或其他原因同时存在。

来源家族检查与历史 `tdc-herg`、`herg_karim` 匹配，全部 2,000 行继续受来源家族、逐文献/实验/队列映射、任务等价性及具体用途权利的保守阻断。此结论不等于逐行已经证明同一原始实验，而是尚未得到来源独立证据、不能放行。**独立来源审核没有通过；最终状态为 `STAGE7C_BLOCKED`。**

完整审计文件原始字节 SHA256 为 `591a7697c7b577806308d4a2facbf91d7ab4ef639389b4e0a043d5b8d8bf368e`；审计对象摘要为 `a467e57eba0f9f5ef31e8f278234d9cdfd83dcaf2f80e5c672f111955f13e530`。字节摘要与规范对象摘要用途不同，不互换。完整历史源投影回执对象摘要为 `21e183b8c380be1699209c828e32bc07d12a603b80fae2d00fc9d1d1d711c756`。

## 标签隔离

允许字段仅包括分子结构、活动/分子/实验/文献/来源身份、测量类型和单位、目标映射、实验物种与细胞/组织元数据。文献只获取 DOI/PMID、标题、期刊、年份等元数据，不打开候选结果表或补充活性数据。

`confidence_score` 只用于目标映射审核，不当作实验结果质量、预测可信度或临床适用性证据。

以下字段不被请求、分类或用于筛选：`value`、`standard_value`、`relation`、`standard_relation`、pChEMBL、activity comment、有效性/质量结果标志、阳阴性标签。严格校验响应全部字段；服务端忽略 `only` 投影时停止并拒绝保存该响应。

因此，“精确 IC50 / 不等式 / 范围值 / 缺失结果 / 质量异常”在本轮均为 **`not-inspected-independent-custodian-required`**，没有可报告的分类数量。不能把 `IC50 + nM` 的元数据行称为精确 IC50，也不能把未知类别计成 0。此项需要独立保管人在原始标签合同、异常处理规则和隔离权限冻结后另行处理；本模型开发流程不读这些结果。

## 原始证据与离线重建

官方来源及许可见 [Stage 7A 来源调查](STAGE7A_DATA_SOURCE_REVIEW.md)：

- [ChEMBL 37 固定发行目录](https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/)
- [固定版本 DOI](https://doi.org/10.6019/CHEMBL.database.37)
- [官方 API](https://www.ebi.ac.uk/chembl/api/data/)
- [固定发行 LICENSE](https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/LICENSE)，CC BY-SA 3.0；底层来源及具体研究、再分发用途继续单独审核。

每份成功响应在私有科研目录保存原始字节、URL、投影查询、UTC 时间、字节数和 SHA256；不提交原始实验行或结构。获取回执关联计划、bundle、全部响应及提取脚本 SHA。离线加载必须提供事先记录的获取回执 SHA，再验证全部原始响应、页顺序、目标总数、ID 关联及重建 bundle 的成员和顺序。只重新计算派生 bundle 的 hash 不能替代来源关联核验。

计划先写入的执行顺序、文件时间与响应 UTC 构成本机追溯记录；不是独立保管证明或不可篡改时间戳。审计版本和源码指纹也不等于已为所有间接导入依赖建立完整代码供应链证明。

## 科学语义和来源限制

人类目标不代表每条 assay 都使用人类功能性电流阻断方法；ChEMBL B/F/T 分类也不能代替 patch-clamp、结合、reporter 等具体方案。IC50、Ki、EC50、AC50、单浓度抑制率不直接合并；单位可量纲换算不证明终点等价。

文献 DOI 可检索不等于已取得原始实验、实验队列及化合物实体映射；综述、计算论文和跨库导入尤其需要回溯原始来源。历史 Wang/TDC 与 Karim 文献汇编来源家族仍保守阻断；数据库版本更新、缺少精确 DOI 字符串交集或结构侧互斥均不能自动证明来源独立。MoLFormer 预训练结构曝光也继续为未知。

对 canonical、去立体/同位素近等价、去盐中和母体、互变异构母体和 Bemis–Murcko 骨架使用原历史排除规则。全部有效候选先做身份连通分组，再排除历史污染和重复，不能通过删除桥接记录逃逸。原 `[ACYCLIC]` 保守分组不变。重复相关记录全部隔离，不依据未读结果择优、平均或改标签。

## 复现和软件验证

以下命令在仓库根目录 PowerShell 运行；私有目录不能在仓库或 OneDrive 内，不会随 Git 克隆取得。重新获取必须使用新的目录和新的输出文件，脚本拒绝覆盖历史证据。

```powershell
$stage7Py = '.\.venv-v3\Scripts\python.exe'
$env:TOXNET_VALIDATION_ALLOW_LABELS = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$stage7Expanded = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation\stage7b-chembl37-expanded-20261016'

# 离线先核对事先记录的真实 SHA，不把重算自身 hash 当作来源批准。
$stage7ReceiptSha = 'be2b9927a51128dd45c4331e6699a5ed30707ce510241c510ec229d79a9c7c2b'
& $stage7Py scripts/survey_chembl_expanded_labelblind.py `
  --private-dir $stage7Expanded --receipt-sha256 $stage7ReceiptSha `
  --output artifacts/independent-validation/stage7b-expanded-retrieval-repeat.json

# 复用原固定参考，再增加完整历史 Karim/Tox21 源的结构列审计。
# 需要本机已有 SHA 匹配的历史缓存；无网络、无预测、无标签访问。
& $stage7Py scripts/audit_stage7b_supplement.py `
  --private-dir $stage7Expanded --receipt-sha256 $stage7ReceiptSha `
  --output artifacts/independent-validation/stage7b-expanded-audit-repeat.json

& $stage7Py -m unittest test.test_chembl_expanded_labelblind test.test_survey_chembl_labelblind -v
```

此独立检索脚本只输出获取聚合，不授予任何资格。测试为 `httpx.MockTransport` 拦截的合成软件夹具，与真实审计数量分开；实际本轮执行上述两个模块，**30 项测试通过**（新增扩展模块 13 项，既有获取模块 17 项）。覆盖冻结计划、2,000 条上限、页顺序与完整性、40-ID 来源关联、投影失败、版本变化、固定主机、重定向、预算、SHA 失配、快照重建及禁止覆盖。项目完整回归结果统一见资格门禁报告，不把这 30 项称为完整验收。

仍需人工确认原 hERG 阈值/边界与实验组成；独立保管人确认逐实验、文献、批次与队列来源；解析或提供不含结果的历史未解析结构证据；核查具体用途许可；由保管人冻结并批准结果关系符、范围、异常与重复处理规则及类别/骨架支持度证明。没有这些证据时不得通过结果筛选来“补足”样本。

本轮没有运行 A4/B16/Morgan 在候选上的预测、指标、标签分布、重新训练或校准；`formalAvailability=false` 和正式模型保持原状。来源、权利、任务和隔离机制未全部批准之前，结论保持 **`STAGE7C_BLOCKED`**。
