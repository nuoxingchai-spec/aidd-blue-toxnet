# Stage 7A：独立验证数据源调查

调查日期：2026-10-15—16（Asia/Shanghai）。本轮限于 Stage 7A 数据源调查与 Stage 7B 无标签独立性审计。**没有合格的新独立验证集，不能进入 Stage 7C。** 本文记录来源证据及未解决问题；实际结构排除统计见 [Stage 7B 审计](STAGE7B_CANDIDATE_AUDIT.md)。不以软件测试、门户更换或结构互斥代替科学独立性证明。

## 现有合同与准入边界

已审查 `INDEPENDENT_VALIDATION_PROTOCOL.md`、`DATASET_INDEPENDENCE_AUDIT.md`、`LICENSE_REVIEW.md`、`STAGE6_ACCEPTANCE.md`、`TOXNET_MODEL_COMPARISON.md` 及 `backend/independent_validation_data.py`、`backend/dataset_overlap_audit.py`、`scripts/audit_independent_dataset.py`。延用以下要求：

- 历史 v2 train/validation/test、v3 开发 train/validation、Karim 已曝光来源与 Tox21 已开发来源都是排除参考；不得把旧集合改名为新盲测。
- canonical、去盐母体、去立体/同位素近等价、互变异构母体和 Bemis–Murcko 骨架都需要排除。无环结构继续使用 `[ACYCLIC]` 共同分组；不为取得样本而放宽。
- 文献、实验、队列、来源家族的独立性另行审核。历史参考的文件和结构 SHA 完整不等于逐个实验来源已全部溯源。
- `CandidateMetadata` 的本地使用权、总体独立性、与已训练任务合同可比性均需要真实审查证据和审核记录。结构层通过不能自动写入 `qualified=true`。
- A4/B16/C_Morgan 的权重、清单、任务合同和评估代码绑定继续使用 Stage 6 合同。本轮不冻结评价协议、不封存标签、不运行预测、不训练或选择模型，所有 v3 继续 `formalAvailability=false`。

原 hERG 任务沿用 [TDC 官方任务页](https://tdcommons.ai/single_pred_tasks/tox/) 与 [Wang 2016 原始论文](https://doi.org/10.1021/acs.molpharmaceut.6b00471) 发布的阻断二分类标签。TDC 页面仅给出 blocker/nonblocker，未给统一阈值；项目没有把所有原实验重新统一到一个 IC50 阈值。本轮核查的论文元数据标注 subscription，未据缺失方法补猜规则。Karim 的 10 μM 定义也不能替代原任务合同。候选只标注“hERG”或“human KCNH2”仍不足以直接比较。

## ChEMBL 37：官方来源与版本

| 核查项 | 本轮可确认的证据 | 限制 |
|---|---|---|
| 数据发布者 | EMBL-EBI ChEMBL；主要整理公开文献、提交及整合来源的结构和活性信息 | 同一实验可能经 ChEMBL、PubChem、BindingDB 或汇编再次出现；数据库名不是独立来源 |
| 固定版本 | [官方发布目录](https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/)；[版本 DOI](https://doi.org/10.6019/CHEMBL.database.37) | 不使用 `latest` 作为固定数据身份，不下载完整数据库 |
| 在线 API 实测 | [status.json](https://www.ebi.ac.uk/chembl/api/data/status.json) 返回 `chembl_db_version=ChEMBL_37`、`chembl_release_date=2026-05-01`、`status=UP` | 在线 API URL 不含发行号；每次采集前后必须核对版本并固定响应字节 SHA。它不是永久不可变的版本化 API |
| 发布日期解释 | [官方 Downloads](https://chembl.gitbook.io/chembl-interface-documentation/downloads) 将 ChEMBL 37 列为 May 2026 | 发布、FTP 文件更新时间、论文发表时间、原实验实施时间是不同概念，不以发布时间证明实验新独立 |
| 人类目标 | UniProt `Q12809` 及 `target_type=SINGLE PROTEIN` 的官方 target 投影实际返回 `CHEMBL240`，KCNH2，`Homo sapiens`，tax_id 9606 | 蛋白目标物种不等于实验宿主、细胞系或所有记录均为人类功能性阻断试验 |

独立核查的 status 响应为 212 bytes，SHA256 `29dc1fb09487a8d2962253a4d0d92c4ded0aa0d831effab42445d0c3483d4740`。人类单蛋白 target 投影响应为 290 bytes，SHA256 `db003b0ccdc6f98c49c1dc6292bb7c07052d0b6f2276376b45d17728b04062f4`。这些是元数据证据，不是实验结果或资格认证；候选采集回执以 Stage 7B 的实际输出为准。

## 标签盲获取方法

[ChEMBL 官方 API 文档](https://chembl.gitbook.io/chembl-interface-documentation/web-services/chembl-data-web-services) 明确支持 `only` 字段投影；[官方 Python 客户端](https://github.com/chembl/chembl_webresource_client) 也说明只返回所选字段。可以在服务端请求活动身份、分子结构、实验/文献 ID、测量类型和单位，无需先下载结果再删除标签。

采用固定目标、固定记录顺序及有界记录数进行初始调查。不得根据实验值、正负比例或模型分数挑选页面；有界先导样本不代表 ChEMBL 全部 hERG 数据。结构及元数据仅保存在私有缓存，公开输出聚合计数和 SHA。

| 可请求的字段类别 | 不请求的字段类别 |
|---|---|
| activity/assay/document/molecule/record/source 标识、canonical SMILES | 活性值、标准化值、上下界、关系符、pChEMBL |
| `standard_type`、`standard_units` 等测量定义 | `activity_comment`、`data_validity_comment`、active/inactive、hit call 或质量标志中的结果信息 |
| 目标身份、物种、assay type、目标关系、confidence score、细胞系及可审的非结果实验定义 | 未约束的完整 activity 对象、结果表、补充数据、候选标签文件 |
| 文献 DOI/PMID/年份和来源交叉标识 | 论文中候选化合物的数值结果、逐行阳阴性标签及其可穷举哈希 |

服务端投影仍需严格响应字段校验；未知字段或版本变化时停止，不打印、处理或保存结果字段。任何临时网络失败都不能用假数据补齐。文献方法、实验描述可能夹带结果，进一步人工审核应由数据保管人提供不含结果的方案摘录；本轮不打开候选全文结果与补充活性表。

### 本轮实际采集范围

2026-10-16 实际按 `CHEMBL240`、`activity_id` 升序采集首 **200 条活动元数据**，关联 **28 个 assay 元数据、13 个 document 元数据**，整个成功快照的 HTTP 响应合计 **99,738 bytes**。该次 API 返回目标活动总数 **41,078**，这里只调查了先导页，不能外推全库合格率或宣称全库没有可用实验。采集前后 status 内容完全一致，均为 ChEMBL 37。

ChEMBL 会随 `standard_type/standard_units` 附带原始 `type/units`，两者都是测量定义。本轮首次严格校验因此停止；核查差异仅为这两个非结果字段后，将它们显式加入投影和严格合同，未接收或放宽实验结果字段。成功快照字节 SHA256 为 `2b8cf8ad21ab9f0a0bb735c3227787cab1080fde724599d48e5f950fc0681f0e`；私有获取回执 SHA256 为 `2ab195eaa322a2c2465f0c9a4c1876cfe5bc11af348632f6aa06e237e4deb812`。公开结构统计和各响应 SHA 见 Stage 7B 聚合回执。

候选引用的 document 年份为 1997—2004；这些是引用身份而不是“13 项新的独立实验”。其中包含综述、QSAR/建模论文及其他主靶点药物研究，不能仅凭 DOI 存在就认定为原始人类电流阻断实验。实际引用标识如下，仅核查元数据，未打开候选全文结果或补充活性表：

| 年份 | ChEMBL 引用 DOI |
|---|---|
| 1997 | [10.1021/jm970517u](https://doi.org/10.1021/jm970517u) |
| 2001 | [10.1021/jm0002432](https://doi.org/10.1021/jm0002432)、[10.1021/jm0004998](https://doi.org/10.1021/jm0004998)、[10.1021/jm000484+](https://doi.org/10.1021/jm000484%2B)、[10.1021/jm010156p](https://doi.org/10.1021/jm010156p) |
| 2002 | [10.1016/S0960-894X(02)00250-0](https://doi.org/10.1016/S0960-894X(02)00250-0)、[10.1021/jm0208875](https://doi.org/10.1021/jm0208875)、[10.1021/jm011030v](https://doi.org/10.1021/jm011030v) |
| 2003 | [10.1016/S0960-894X(03)00196-3](https://doi.org/10.1016/S0960-894X(03)00196-3)、[10.1021/jm0204542](https://doi.org/10.1021/jm0204542) |
| 2004 | [10.1016/j.bmcl.2003.11.007](https://doi.org/10.1016/j.bmcl.2003.11.007)、[10.1016/j.bmcl.2003.10.016](https://doi.org/10.1016/j.bmcl.2003.10.016)、[10.1016/j.bmcl.2003.12.040](https://doi.org/10.1016/j.bmcl.2003.12.040) |

这些年代早于 Wang/Karim 汇编的发表时间，只说明存在被历史来源纳入的机会，不据此宣称逐条重复已经证实。仍须补齐论文—原始实验—队列—化合物实体映射。

## 实验语义与原 TDC hERG 的一致性

ChEMBL 的测量类型、单位及 assay/target 元数据允许进行方案侧检查，但没有统一的“hERG 阳性”标签。官方 [Assay and Activity FAQ](https://chembl.gitbook.io/chembl-interface-documentation/frequently-asked-questions/chembl-data-questions) 将 B/F 分为结合/功能等大类，confidence score 表示目标映射可靠性；它们均不是实验真实性或临床可比性的保证。pChEMBL 将多种效力/亲和力测量置于负对数尺度，也不使其成为同一终点。

| 候选测量 | 允许作出的方案侧判断 | 进入比较前仍需的证据 |
|---|---|---|
| hERG 电流功能性 IC50，已核实实验系统 | 是优先调查方向；nM/μM 是量纲，可定义转换规则 | 实际方法、温度、电压/孵育条件、重复测量处理、边界值规则，以及与原标签定义的一致性 |
| 结合 Ki/Kd 或放射配体置换 IC50 | 结合与功能阻断不是自动相同的任务 | 不得直接沿用功能阻断阈值，必须有独立的任务定义及适用性审核 |
| 单浓度抑制率 / inhibition (%) | 依赖测试浓度和方案 | 不得换算为未观察的 IC50，也不直接并入二分类标签 |
| AC50/EC50、体内 QT、细胞毒性或其他测量 | 需要另立任务语义 | 不默认等价于 TDC hERG 阻断 |
| 物种/方法/单位缺失，或 homologous target mapping | 信息不足 | 不能从 `CHEMBL240`、数据库分类或化合物名称推断补齐 |

本轮不读取候选数值，不确定阳性数量、不设置数据驱动阈值、不合并重复实验结果。原始论文标识可追溯，只证明能找到出处；仍需排除历史汇编引用同一论文、同一实验/队列、再发表或跨库导入。

[Karim 2021 原论文方法](https://doi.org/10.1186/s13321-021-00541-z) 明确其 DeepHIT 来源整合了 BindingDB、ChEMBL 和文献。补充核查得到的历史论文包括 DeepHIT/Ryu 2020（`10.1093/bioinformatics/btaa075`）、Cai 2019（`10.1021/acs.jcim.8b00769`）、Doddareddy 2010（`10.1002/cmdc.201000024`）、Didziapetris 2016（`10.1007/s10822-016-9986-0`），其外部数据还引用 Siramshetty 2018/2020 与 Konda 2019。此处是历史已曝光来源的追溯，不读取新候选标签。它证明存在同来源总体风险，不能证明某个候选已实现逐实验独立。

Stage 6 的 `KNOWN_LINEAGE` 使用 `wang-2016-herg` 等历史身份，简单 DOI 规范化不能将别名自动映射为真实 DOI。本轮不修改其历史固定值；仅凭 publication 字符串无交集不得放行，必须保留 `herg-literature-compilations` 来源家族阻断，并补做别名与原实验映射。

## ChEMBL 使用条件与权利

实际读取 [ChEMBL 37 固定 LICENSE](https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/LICENSE)：HTTP 200，21,142 bytes，SHA256 `f0c22ca33207ad8a85129a71f1454fcf8f2f71c99e68da5da01538210fa3ffba`；声明 **Creative Commons Attribution-ShareAlike 3.0 Unported**。[ChEMBL 官方说明](https://chembl.gitbook.io/chembl-interface-documentation/about) 同时引用该许可及 EMBL-EBI 使用条款。

许可为遵守条件的复制、使用和适当修改提供基础；引用时保留来源、版本、许可及修改说明，公开适配材料时需核对相同方式共享义务。[EMBL-EBI 使用条款](https://www.ebi.ac.uk/about/terms-of-use/) 当前页面标注 2026-10-14 修订，要求归属并提示底层第三方权利仍由使用者负责。数据库许可不自动覆盖原论文全文、补充材料或所有提交方的额外权利。

因此：本轮可进行有界官方元数据与结构调查，但尚未将任何候选的 `rights.status` 标为 `approved-for-local-research`，也没有批准商业使用、数据库再分发或云端外传。下一步需操作者核对选定记录来源及用途，并留下真实审核人、时间和证据 SHA；不能拿许可证文件哈希替代具体审核结论。原始行不提交仓库。

## EPA ToxCast 备选来源

[EPA 官方介绍](https://www.epa.gov/comptox-tools/exploring-toxcast-data) 本轮实际 HTTP 200，指向 **invitrodb 4.3，August 2025** 与固定 [发布 DOI `10.23645/epacomptox.6062623.v11`](https://doi.org/10.23645/epacomptox.6062623.v11)。该来源包括 Tox21；项目历史已使用 Tox21，所以整个 ToxCast 不能视为新的独立总体。不能把高通量 hit call、AC50 或 reporter/pathway 活性直接当作原四毒性端点标签。

本轮仅下载 EPA 页面链接的两个小型实验元数据文件，均为 HTTP 200；没有活动结果矩阵：

| 官方元数据文件 | 字节数 | SHA256 |
|---|---:|---|
| [assay annotations](https://clowder.edap-cluster.com/api/files/68af6bd3e4b02565fc7c3aa8) | 1,088,201 | `eecbdac47fe4b4c3db765d412d15526331c269d36c7d79dde938f59ea60461f9` |
| [assay target mappings](https://clowder.edap-cluster.com/api/files/68af6bd3e4b02565fc7c3aa0) | 97,652 | `bf45c47dc383c16aea9c4cf92993f7034a3141a252aa02d9f7aa7216ac84c394` |

按 hERG/KCNH2 实际检索得到 **3 个实验端点，不是 3 个候选化合物**：

| 端点 / aeid | 实验元数据摘要 | 与原任务的限制 |
|---|---|---|
| `NVS_IC_hKhERGCh` / 686 | human，CHO 来源无细胞蛋白；[3H]-Astemizole 放射配体结合；0.75 h | 结合 readout，不直接视为功能性阻断 IC50 |
| `ERF_TW_IC_hKCNH2` / 3184 | human，重组 HEK293 来源无细胞蛋白；[3H]-Dofetilide 放射配体结合；1 h | 同上；具体原实验与历史来源映射未完成 |
| `TOX21_hERG_U2OS_Antagonist` / 3210 | human U2OS，铊敏荧光的 ion-channel functional reporter；约 0.17 h | Tox21 来源家族触及保守排除；不是项目原十二通路标签的同义项 |

三个端点元数据的 `normalized_data_type` 均为 `percent_activity`；这不是阳性定义、效力数值或统一 IC50 标签。3210 还存在元数据不一致：component/design 写 functional reporter/ion channel，而 endpoint 的 function 字段写 binding，另一描述写 inducible reporter。需由实验方案/GD211 或原作者材料确认，不能挑一个字段静默放行。

固定 Figshare 发布元数据 API 本轮返回 **HTTP 403**，未成功确认该固定版本的许可内容；不把旧审查、EPA 网站可访问或政府来源当作本轮已确认的具体文件许可。后续需人工核对发布页许可、数据提供方条款及本地用途，当前许可审核保持 unresolved。

本轮未下载 invitrodb 完整数据库或结果矩阵；没有可核验的结构投影时，结构通过数为“未审计”，不能填写零来冒充已完成筛查。

## 需要人工确认的证据与后续门槛

1. **原任务映射**：确认原 TDC/Wang hERG 标签规则、实验组成与候选方案是否可直接比较；不能借用 Karim 阈值。
2. **逐实验来源**：数据保管人提供不含候选结果的论文/实验/队列/来源家族清单，映射至历史来源，证明没有重复引用或跨库导入；标识缺失保持未知。
3. **结构来源**：候选 canonical 结构与实际测试实体的映射、盐/混合物/立体与同位素处理须可追溯，不能按名称猜测结构。
4. **权利适用**：确认选定来源允许本地独立研究，记录审核人与证据；再分发、商业和外传权限分开处理。
5. **盲化与后续授权**：只有结构/来源/任务/权利全部通过，并由独立保管方管理标签后，才可另行申请 Stage 7C；本次授权不包含该步骤。

即使将来通过上述条件，MoLFormer 预训练结构曝光仍是 `unknown`；实验标签独立不等于编码器从未见过该结构。当前只能形成调查及排除证据，不能生成独立 AUROC/AP/Brier/ECE 或模型科学优劣结论。
