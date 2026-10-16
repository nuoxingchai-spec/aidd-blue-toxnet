# Stage 7B 补充：EPA ToxCast 结构映射与资格审查

日期：2026-10-16（Asia/Shanghai）。结论：`STAGE7C_BLOCKED`。本轮找到了官方、可保持标签盲态的端点成员查询合同，但实际请求均因缺少个人 API 授权返回 HTTP 401。**没有获得三个端点的实际物质成员或结构映射，不能宣称结构审计已完成。**

本报告是补充证据，不覆盖 `STAGE7A_DATA_SOURCE_REVIEW.md`、`STAGE7B_CANDIDATE_AUDIT.md` 及原三端点元数据回执。未下载结果表、活性值、hitcall、候选标签或大型数据库；未调用模型、训练或评价。

## 真实来源、版本与安全获取方法

官方 [Exploring ToxCast Data](https://www.epa.gov/comptox-tools/exploring-toxcast-data) 指向 invitrodb 4.3（2025 年 8 月）以及固定发布 DOI [10.23645/epacomptox.6062623.v11](https://doi.org/10.23645/epacomptox.6062623.v11)。原轮已验证的两个官方 XLSX 为独立的实验元数据文件，不是化合物活动矩阵：

| 官方文件 | 固定 SHA256 |
|---|---|
| [Assay annotations](https://clowder.edap-cluster.com/api/files/68af6bd3e4b02565fc7c3aa8) | `eecbdac47fe4b4c3db765d412d15526331c269d36c7d79dde938f59ea60461f9` |
| [Assay target mappings](https://clowder.edap-cluster.com/api/files/68af6bd3e4b02565fc7c3aa0) | `bf45c47dc383c16aea9c4cf92993f7034a3141a252aa02d9f7aa7216ac84c394` |

本轮重新查阅 EPA 官方说明、Clowder 文件目录及 CTX OpenAPI，实际新增官方 HTTP 响应共 **741,921 bytes**。原始响应保存在仓库与 OneDrive 之外的本机私有科研缓存；公开 [最终聚合回执](artifacts/independent-validation/stage7b-epa-structure-supplement-v2-20261016.json) 记录每次获取的 UTC、HTTP 状态、响应大小、源 URL、原始字节 SHA256。它不包含候选结构、实验结果、密钥或用户实际路径。新增 v2 聚合将同一批网络证据绑定至本轮最终的组件传播/严格 JSON 处理代码；先前本轮生成的聚合保留原字节，不覆盖。

| 资料 | 本轮实际结果 | 证据作用与边界 |
|---|---|---|
| [固定 Figshare v11 发布元数据](https://api.figshare.com/v2/articles/6062623/versions/11) | HTTP 403，118 bytes | 未核实固定发行包许可；不把历史浮动页许可自动套用本轮文件 |
| [EPA 链接的 Clowder 实验文档目录](https://clowder.edap-cluster.com/api/datasets/6894f2dae4b025654d12b716/files) | HTTP 200，290,034 bytes | 找到三个 AEID 的 GD211 PDF 文件身份；未下载 PDF，也没有取得成员或结构 |
| [CTX bioactivity OpenAPI](https://comptox.epa.gov/ctx-api/docs/bioactivity.json) | HTTP 200，123,563 bytes；SHA `5a14591752181a0f841d30ca6157006b1a9632ca6d524c878cd777d3b5257798` | 明确存在返回被测试 DTXSID 清单的独立成员路由 |
| [CTX chemical OpenAPI](https://comptox.epa.gov/ctx-api/docs/chemical.json) | HTTP 200，160,760 bytes；SHA `5f7bfb2f890bc7b9d0a7feff702ea49735fbd3d2a37f51e48a793605ab77f2ad` | 记录 DTXSID/DTXCID 化学详情和结构投影；不是对任何实际候选完成映射 |
| [CTX API 官方鉴权说明](https://www.epa.gov/comptox-tools/computational-toxicology-and-exposure-apis-about) | HTTP 200 | API 免费，但要求个人 `x-api-key`；用户需自行向官方 `ccte_api@epa.gov` 申请。未发送邮件、扫描密钥或索取密钥明文 |

OpenAPI 实际定义：

```text
GET https://comptox.epa.gov/ctx-api/bioactivity/assay/chemicals/search/by-aeid/{aeid}?projection=dtxsidsonly
description: return list of chemicals (DTXSIDs) tested by ToxCast assay component endpoint ID (AEID).
security: x-api-key (header)
```

这优于从完整活动矩阵删除结果列，但 **live CTX 路由未提供本轮已验证的 invitrodb 4.3 固定快照绑定**。即使将来获得个人 key 并请求成功，也必须另行取得版本、完整性与结果无关成员选择的证据；不能把线上当前清单直接称为 v4.3 成员。未执行任何 `/bioactivity/data/` 活动结果路由。

## 三个实验端点与实际进展

端点定义来自原轮固定元数据；表中“适用性”是方案语义审查，不含候选结果判断。

| AEID / 官方实验名 | 实际实验定义 | 与原 TDC hERG 任务的适用性 | 本轮成员请求 |
|---|---|---|---|
| 686 / `NVS_IC_hKhERGCh` | 人类靶标；CHO 来源无细胞蛋白；[3H]-Astemizole 放射配体结合，约 0.75 h；归一化类型 `percent_activity` | 结合 readout 不足以证明与原二分类标签可直接比较；不能将 AC50、hitcall 或放射配体置换结果直接转换成原功能阻断概率 | `projection=dtxsidsonly`，HTTP 401 |
| 3184 / `ERF_TW_IC_hKCNH2` | 人类靶标；重组 HEK293 来源无细胞蛋白；[3H]-Dofetilide 放射配体结合，约 1 h；`percent_activity` | 同上；缺少原实验批次、文献/实验来源映射及原任务标签合同证据 | `projection=dtxsidsonly`，HTTP 401 |
| 3210 / `TOX21_hERG_U2OS_Antagonist` | 人类 U2OS；铊敏荧光 ion-channel functional reporter，约 0.17 h；`percent_activity` | 元数据的 functional reporter、binding 和 inducible reporter 字段存在不一致；且来源属于历史已使用的 Tox21 项目家族，保守来源排除仍适用 | `projection=dtxsidsonly`，HTTP 401 |

3210 不因名称为 hERG 就与原十二个 Tox21 通路标签相同；也不因通路不同就自动成为独立来源。对 686/3184，同样不能因来源缩写 NVS/ERF 与历史训练文件名不同而认定为来源独立。

Clowder 实际文件目录列出的文档身份为：`AEID686_NVS_IC_hKhERGCh.pdf`（file id `68a37c19e4b02565fc7b1f32`）、`AEID3184_ERF_TW_IC_hKCNH2.pdf`（`68a37cebe4b02565fc7b5611`）、`AEID3210_TOX21_hERG_U2OS_Antagonist.pdf`（`68a37cece4b02565fc7b59d9`）。这些是出处身份；本轮未读取 PDF 或其可能附带的实验结果。

## 实际统计：缺失输入不写成已筛查的零

| 阶段 | 本轮实际计数/状态 |
|---|---|
| 已有 hERG/KCNH2 实验元数据 | 3 个端点 |
| 本轮安全端点成员 GET 请求 | 3 次，全部 HTTP 401 |
| 实际端点成员行数、唯一 DTXSID 数 | `null / not-acquired` |
| 结构投影行数、唯一 DTXCID/SMILES 数 | `null / not-acquired` |
| 缺失结构、映射歧义、混合物、无效结构数 | `null / not-run` |
| canonical / 母体 / 互变异构 / 立体同位素 / 骨架重叠数 | `null / not-run` |
| 通过结构筛查数、完整独立资格数 | `null / not-run` |

没有取得数据文件与取得后发现空集合是两种情况。本轮未构造空的“真实候选清单”，未按一般化学库存的规模冒充这些实验的实际测量成员数。

## 新增的最小离线结构映射合同

`backend/epa_structure_audit.py` 和 `scripts/prepare_epa_structure_audit.py` 实现结构映射与可追溯聚合，没有网络下载、标签映射、模型调用、封存或评价入口。

由独立数据保管方准备三份**不含任何活性结果**的文件：一个 manifest、明确的 endpoint-membership 投影、单独的 DSSTox structure 投影。manifest 的 SHA256 需经独立渠道事先固定；随后先验证两份投影字节 SHA，再解析行。不能提供含结果字段的原矩阵让开发者删除列。

- Manifest schema：`aidd.stage7b.epa-structure-projections.v1`。固定 `selectionPolicy=endpoint-membership-without-results`、源版本、原始 URL、UTC、文件 SHA、chemistry/identity/RDKit 版本。额外字段拒绝；SHA 只能证明字节完整，不会自动批准来源、保管方或许可。
- Membership 每行仅允许 `aeid`（686/3184/3210）、`sourceSubstanceId`、`dtxsid`（可显式 null）、`batchId`（可 null）。化学库存不是这些端点成员的替代品；原始受测实体/批次不能凭名称补推。
- Structure 每行仅允许 `dtxsid`、`dtxcid`（可 null）、`smiles`（可 null）、`substanceType`（single-substance/salt/mixture/unknown）、`mappingRecordId`。DTXSID 表示物质，DTXCID 表示结构；不按名称猜测，不用 MS-ready 去盐结构替代原受测实体。
- 分别记录原实体 ID、唯一物质、唯一 canonical 结构；缺失 DTXSID/结构、未知物质类型、混合物、无效结构、同一物质的一对多映射、同一 DTXCID 的相互矛盾结构均隔离。盐结构保留完整输入，再在审计层比较母体。
- 复用项目 `canonicalize_smiles`、`leakage_identity`、`historical_forbidden_index`、`structural_overlap`、`_group_rows` 和 `lineage_overlap`；身份策略继续包括去盐母体、互变异构、去立体同位素、Bemis–Murcko 及共同 `[ACYCLIC]` 分组。在任何历史/内部重复排除前建立所有有效结构的连通组件；历史污染传播至整个组件，内部相关结构一并隔离，不挑第一种表示。不会静默修改旧规则。
- 结果顺序固定，公开聚合不含结构/原始行；完整历史参考不足时停止。返回 `qualificationGranted=false` 和 `STAGE7C_BLOCKED`；结构映射通过不等于来源、标签语义、许可或独立科学资格通过。

### 可复现离线命令

本轮真实 blocked 回执来自对官方响应缓存重新核验 SHA 的以下命令。变量是环境相对位置示例，不包含实际用户私有路径。已有输出不能覆盖；再次执行应选择新的日期/文件名。

```powershell
$epaPrivate = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation\stage7b-epa-supplement-20261016'
.\.venv-v3\Scripts\python.exe scripts/prepare_epa_structure_audit.py `
  --network-receipt (Join-Path $epaPrivate 'network-receipt-final.json') `
  --output artifacts/independent-validation/stage7b-epa-structure-supplement-v2-20261016.json
```

将来取得独立保管方的**真实无结果投影及冻结 SHA**后，软件可执行下列结构映射审计；这条未来命令本轮未运行，不能视为已经取得数据或通过科学门禁：

```powershell
.\.venv-v3\Scripts\python.exe scripts/prepare_epa_structure_audit.py `
  --manifest '<私有目录中的结构投影manifest.json>' `
  --expected-manifest-sha256 '<保管方事先冻结的SHA256>' `
  --output '<新的聚合回执.json>'
```

如果官方结构 API 的响应字段比合同更宽，必须由保管方在结构专用、安全投影中规范化并留存来源关系；不能让模型开发者下载实验结果后整理。API key 只在本机环境或秘密存储配置，不能提交、写进 URL、截图或报告。

## 测试与剩余人工证据

实际执行：

```powershell
.\.venv-v3\Scripts\python.exe -m unittest test.test_epa_structure_audit -v
```

**18 项全部通过**，覆盖来源 URL/UTC、manifest/文件 SHA、RDKit 版本、端点与字段白名单、缺失/歧义/混合物/盐、结构与母体/立体同位素/骨架重叠、历史污染的组件传播、来源家族暴露、顺序确定性、重复 JSON key/非有限常数拒绝、禁止覆盖及输入缺失。测试使用明确标记的合成软件夹具；不计入任何科学候选数量，也不证明真实 DSSTox 映射正确。全项目回归与依赖检查以本轮总门禁报告的实际执行记录为准。

进入进一步资格建设前仍需：

1. 用户自行取得免费个人 CTX 授权，或独立保管方提供真实 result-free 成员/结构投影；无需向助手发送 key。确认 API 投影、端点覆盖与具体快照版本。
2. 核实 invitrodb 4.3 固定发行许可、各来源附加条件、本地科研用途；再分发和云端外传单独审批。政府网站与开放 API 不自动授予所有资料的任意再分发权。
3. 提供化合物受测实体、DTXSID/DTXCID、批次/队列与实验协议映射；核查是否为盐、混合物、不可结构化物质、历史重复实验或跨库重现。
4. 补齐原 TDC hERG 标签合同和候选 assay 方案的可比性。未经批准的 IC50、Ki、AC50、hitcall 不互换；由隔离数据管理员处理需要原始数值的资格检查，模型开发流程保持不接触标签。
5. 冻结完整协议、预定评价规模及标签隔离机制，并满足所有门槛后另行申请后续阶段。本轮没有资格提出 `STAGE7C_CANDIDATE_READY`。
