# Stage 7B：EPA 无 API Key 离线替代调查

日期：2026-10-17（Asia/Shanghai）。结论：**`STAGE7C_BLOCKED`**。

本轮找到 invitrodb v4.3 的真实公开离线资源和成员字段说明，但**尚未恢复三个端点的实际受测成员或分子结构**。不能以总体化学清单、文件目录或实验注释代替成员。结构映射、缺失结构和通过审计数量均为 `null / 未执行`，不是零个成员或零个有效结构的观测结论。

仅在 `codex/stage7-labelblind-source-audit` 增补本报告、来源核验程序、测试及聚合回执。开始时 HEAD 为 `013a4c980a1c08276cc581a5a66832edb8757eee`，main 为 `3a7e5138398f8eea5f07ae14a083135bbc94d26b`。没有修改既有 Stage 7 报告、历史科研记录、模型权重或正式接口，没有执行 Stage 7C、训练、封存、校准、候选性能评价或收费调用。PR #6 保持 Draft；v3 保持 `formalAvailability=false`。

## 1. 实际获取范围及来源可访问性

先复用 2026-10-15/16 的官方页面、注释工作簿与无 Key 调查缓存，再补查此前没有请求过的公开目录。**本轮没有调用 CTX API**。前轮 CTX 401 记录仍保留在原报告中，不作为本轮无 Key 端点请求。

[机器可读来源回执](artifacts/independent-validation/stage7b-epa-no-api-20261017/source-audit-final.json) 重新校验 **26 份缓存、1,065,075 bytes** 的原始字节 SHA256、来源 URL 与 UTC；其中 2026-10-17 新获取 **12 份元数据响应、527,227 bytes**。另有 6 次档案可访问性探测，仅一次读取了 ZIP 第一个字节；其余不读正文或未收到响应头。两个既有固定 XLSX 单独核验 SHA，不计入上述 26 份缓存的字节总量。原始响应、PDF、日志与本机 manifest 均留在仓库及 OneDrive 之外的私有缓存，没有提交原始实验数据。

| 无 Key 来源 | 实际访问结果 | 作用与限制 |
|---|---|---|
| [EPA Exploring ToxCast Data](https://www.epa.gov/comptox-tools/exploring-toxcast-data) | 复用 HTTP 200 页面 | 指向 2025-08 invitrodb4.3、DOI `10.23645/epacomptox.6062623.v11`；不是成员文件 |
| [固定 Figshare v11 API](https://api.figshare.com/v2/articles/6062623/versions/11) | 前轮缓存 403；本轮未重取该 API | 原状态见既有 EPA 报告；未验证发行文件或许可 |
| [固定 Figshare v11 公开页面](https://epa.figshare.com/articles/dataset/ToxCast_Database_invitroDB_/6062623/11) | 复用 2026-10-16 的 403 / 118 bytes | 未取得文件；403 本身不足以判断需要登录、网络策略还是服务限制，未绕过 |
| [Clowder Summary_Files 目录](https://clowder.edap-cluster.com/api/datasets/687e392ae4b02565bc3e2914/files) | 200；7 个文件，JSON 1,465 bytes | 包括汇总 ZIP、两个实验注释表、cytotox、analytical QC、README 及较晚上传的 PubChem ZIP；没有单独的成员表 |
| [Summary README](https://clowder.edap-cluster.com/api/files/697b7530e4b0731a6170449e) | 200；171,736 bytes | 官方 schema 说明，含字段定义，没有读取候选实验行 |
| [Summary ZIP 文件页](https://clowder.edap-cluster.com/files/68af6b70e4b02565fc7c3a98) | 200；89,125 bytes | 页面列出 31 个库存项：30 个文件、1 个目录。它是发布方元数据，未从 ZIP 中解析中央目录 |
| [Summary ZIP 文件元数据](https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98/metadata) | 200；报告档案大小 **7,502,075,199 bytes** | 未全量下载；页面报告的 checksum 不等于本地整包核验 |
| [MySQL_Data 目录](https://clowder.edap-cluster.com/api/datasets/687e3940e4b02565bc3e291e/files) | 200 | dump `invitrodb_v4_3.sql.gz` 为 **17,651,807,605 bytes**；未下载、未导入 |
| [MySQL README](https://clowder.edap-cluster.com/api/files/68b751b7e4b02565fc7c77e5) | 200；120,768 bytes | 说明超过五亿条记录及 `max_allowed_packet >= 1GB` 等导入要求；本轮无必要性依据全量导入 |
| [历史 DSSTox 6062524](https://api.figshare.com/v2/articles/6062524) / [README 6062527](https://api.figshare.com/v2/articles/6062527) | 复用 API 与公开页面的 403 缓存 | 未取得结构文件、文件大小或文件许可；没有冒充 v4.3 映射快照 |
| [Dashboard DSSTox 5588566 API](https://api.figshare.com/v2/articles/5588566) / [公开页面](https://epa.figshare.com/articles/dataset/Chemistry_Dashboard_Data_DSSTox_Identifiers_Mapped_to_CAS_Numbers_and_Names/5588566) | 本轮两者均 403 / 118 bytes | 实际结构文件版本、SHA、大小与许可未知 |
| [EPA Batch Search 帮助](https://www.epa.gov/comptox-tools/chemicals-dashboard-help-batch-search) / [官方导出手册](https://s3.amazonaws.com/dmap-ccte-ccd/current/manual/step-3b---selecting-download-options.html) | 帮助页复用 200；导出手册新获取 200 / 41,386 bytes | 支持精确标识查询与 CSV/Excel/SDF 导出。实际导出列及本轮成员导出尚未验证，不能声称已取得 SMILES/InChIKey 文件 |

Summary 的 cytotox、analytical QC 和新 PubChem ZIP **未下载**，不能根据活性/QC 结果挑选候选成员。公开目录中的 `pubchem_invitrodb_v4_3_19AUG2026.zip` 大小为 **192,901,527 bytes**，上传日期晚于原始发布；没有为绕过大包或增加数量下载它，也未视为原始 v11 文件。

### 档案探测的真实错误

对 [官方 Summary 下载地址](https://clowder.edap-cluster.com/api/files/68af6b70e4b02565fc7c3a98) 仅尝试小范围请求，无登录、代理规避或授权绕过：

| 方法 / Range | 真实结果 | 正文处理 |
|---|---|---|
| HEAD，无 Range | HTTP 404 | 不读正文；不能据此断言 GET 文件不存在 |
| GET `bytes=0-0` | HTTP 206，`Content-Range: bytes 0-0/7502075199` | 仅读取 1 byte ZIP 文件头；没有读取实验数据条目 |
| GET `bytes=-65536`，API 下载 URL | HTTP 500 | 不读正文 |
| GET `bytes=-65536`，[官方 blob URL](https://clowder.edap-cluster.com/files/68af6b70e4b02565fc7c3a98/blob) | HTTP 500 | 不读正文 |
| GET `bytes=7502009663-7502075198` | 25 秒 `ReadTimeout`，响应头之前超时 | 0 bytes；该次未记录精确 UTC，回执明确保留 `null`，没有补造时间 |
| GET `bytes=7502074175-7502075198` | 40 秒 `ReadTimeout` | 0 bytes |

因此只确认起始单字节 Range 可用，**未确认尾部随机访问可用，未取得或解析 ZIP 中央目录**。未因 Range 失败改为全包下载。响应错误的 `Content-Length` 只是服务器声明，不计为实际已读正文。

## 2. 三个端点与实际成员恢复

先同时校验两份既有工作簿的 SHA，再由 assay annotation 的 `aeid/acid/aid` 与 target mapping 的 `KCNH2` 交叉绑定，避免将 assay id 当 component id。

| AEID | ACID | AID | 官方名称 / 实验含义 | 实际成员与结构 |
|---|---|---|---|---|
| 686 | 423 | 237 | `NVS_IC_hKhERGCh`；人类靶点、CHO 来源无细胞蛋白，[3H]-Astemizole 结合 readout | spid/chid/DTXSID 成员数、结构数均未取得 |
| 3184 | 2914 | 897 | `ERF_TW_IC_hKCNH2`；人类靶点、HEK293 来源无细胞蛋白，[3H]-Dofetilide 结合 readout | 同上 |
| 3210 | 2939 | 910 | `TOX21_hERG_U2OS_Antagonist`；人类 U2OS、铊敏荧光功能 readout；元数据同时标注 binding，需审核 | 同上；Tox21 来源家族暴露保守规则仍适用 |

三个端点的 `normalized_data_type` 均为 `percent_activity`。这些是实验方法注释，**不是原 hERG 的 IC50 二分类标签合同**。原任务阈值、比较运算符、单位和边界尚未核实，见既有 `STAGE7B_HERG_LABEL_CONTRACT_REVIEW.md`；不得借用 Karim 的阈值。

官方 Summary README 的 schema 明确说明：

- `spid` 是受测 sample 标识，可联系化学来源；`chid` 是 invitrodb 内部 chemical 索引，不等于 DTXCID。
- `dsstox_substance_id` 是 DTXSID；`aeid` 是 endpoint id；`acid` 是 component id，部分处理阶段按 component 而非 endpoint 存储。
- `mc4` 和 `mc5–6` 说明中含 `spid/chid/dsstox_substance_id/aeid`，但同时包含响应、拟合参数或活性结果。`sc1/sc2` 同样将记录标识与结果耦合。仅导出 winning fits 也不能证明涵盖所有原始受测成员。
- 成员恢复需从实际原始测试/处理记录建立覆盖证明，并由冻结的 AEID→ACID 表正确关联；不能把 assay-level 或总体 chemical 清单扩展到每个 endpoint，也不能按阳性、hitcall、活性大小或质量得分筛选。

本轮只读字段定义，没有下载/打开上述活动表。当前既没有受测记录，也没有真实 DTXSID 清单可提交 CompTox 或 PubChem；**未进行辅助结构查询**。名称/CAS 模糊匹配没有被用作结构确认。

## 3. 版本、完整性与许可证据

| 已核验文件 | 原始字节 SHA256 |
|---|---|
| 既有 assay annotations XLSX | `eecbdac47fe4b4c3db765d412d15526331c269d36c7d79dde938f59ea60461f9` |
| 既有 assay target mappings XLSX | `bf45c47dc383c16aea9c4cf92993f7034a3141a252aa02d9f7aa7216ac84c394` |
| Summary README PDF | `fffa727f45f01a2e3593d93348aee527a03c5b1957bfe75b7567da7354d108af` |
| MySQL README PDF | `2cc2387b246743e2c4e7e7d83e766aa3c3ed6b32b107f9174dde30cfd158ee29` |
| Summary ZIP 公开文件页 HTML | `d181c3ae53ebf0d9827548674954140dd945b579e4f6c6a335cd4cffbacb80af` |
| 官方 Dashboard 导出手册 HTML | `615a7ca8b278a3ffc529a9ad2d364e798d1f8eff74da7ffc446b4eb5cb8e1066` |

其余文件的 URL、UTC、HTTP 状态、字节大小与 SHA 见机器回执。Summary ZIP 页面发布方报告 `c61d46cf685d5d84e72d0d4f6f46746ab1714d6f924e43480d390925ae827c59`，但本轮未取得整包，故明确记录 `publisherSha256LocallyVerified=false`、`completeArchiveSha256=null`，不能把该值当成本机下载验证结果。

须保留的版本差异：

1. EPA 主说明页引用固定 DOI **v11**；[Clowder v4.3 space](https://clowder.edap-cluster.com/spaces/687e388ce4b02565bc3e28e4) 描述引用 **v14**。本轮没有核验 Figshare v14 的文件与 v11 相同，不能自动替换发行版本。
2. Summary README 标题/正文写 August **2025**，包内列名却出现 `AUG2024`；两个单独 XLSX 的官方上传名为 `AUG2025`。摘要 README 上传时间为 **2026-01-29**，是晚于初始发布的文档。名称疑点保留，版本以来源、发行证据和文件 SHA 联合审核，不能静默改名。
3. [Dashboard release notes](https://www.epa.gov/comptox-tools/comptox-chemicals-dashboard-release-notes) 说明结构快照 `20241112_DSSTox`（链接文字 `202411120208_DSSTox`），另有历史 `20240208_DSSTox`。这些均不自动等于 invitrodb4.3 的受测实体快照，不能将 MS-ready / QSAR-ready 去盐表示替代原物质。

Summary ZIP 页面许可证 Type/Holder 为空；Figshare 403 阻碍文件级条款核验。公开可访问不等于已确认任意再分发或商业使用权。需由实际审核者核实 EPA/供应商/DSSTox 文件的本地科研使用条件、引用要求及再分发范围。本轮没有取得法律授权，也没有提交这些原文件。

## 4. 结构与来源独立性：未完成，不能以映射替代资格

| 阶段 | 实际计数 / 状态 |
|---|---|
| 固定实验注释确认的 hERG/KCNH2 端点 | 3 |
| Summary_Files 公开文件库存 | 7 个外层文件；ZIP 页面声明 31 项，解析到 30 个文件名 |
| 实际测试成员行、唯一 spid/chid/DTXSID | `null / 未恢复` |
| 成功映射、结构缺失、映射歧义、混合物、无效记录 | 均为 `null / 未执行` |
| canonical、母体、互变异构、立体/同位素、骨架重叠 | 均为 `null / 未执行` |
| 通过结构审计或完整独立资格的结构数 | 均为 `null / 未执行` |
| 实验批次、队列、文献、来源家族独立性 | `MANUAL_REVIEW_REQUIRED`；3210 的 Tox21 来源家族已知暴露 |
| 可进入 Stage 7C | **否** |

现有 `backend/epa_structure_audit.py` 的 RDKit canonical、去盐/中和母体、互变异构、去立体/同位素、Murcko 及共同无环分组规则继续保留。结构先分组再传播历史污染；不能为样本量放宽骨架门槛。其完整历史参考、来源审核及新增完整原始 Tox21 覆盖的未决问题仍见前轮报告，本轮没有据此授予资格。

当合法获得**真实、无结果的成员/结构投影**后可复用既有映射审计。但当前 v1 投影合同只接受 `invitrodb4.3` 来源绑定，不能将另一个 DSSTox 日期快照硬填成 4.3；跨快照映射需要另行增加明确版本合同并复审，本轮没有悄悄修改旧合同。

## 5. 可复现流程与自动化

新增 `backend/epa_no_api_fallback.py` 与 `scripts/audit_epa_no_api_fallback.py` 仅核验安全元数据：来源 manifest/文件 SHA、累计体积、严格 JSON/字段、官方无凭据 URL、AEID/ACID/AID、公开档案库存和探测记录。禁止 CTX 路由、编码凭据、任意下载 URL、结果字段及覆盖历史文件。探测中的 206 必须符合请求的 Content-Range、完整档案长度及实际读取字节数；未读正文不能声称有正文 SHA。

该脚本没有网络、活动表读取、模型或评价入口；不会把库存文件当作测试成员。来源分类及本地回执仍是可审计工程记录，不是独立管理员签署或不可篡改的法律证明。

在 Windows 11 PowerShell 的仓库根目录，复核本机已有缓存，不下载模型/大包：

```powershell
$py = (Resolve-Path '.venv-v3\Scripts\python.exe').Path
$epaRoot = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation'
$epaEvidence = Join-Path $epaRoot 'stage7b-epa-no-api-20261017\verified-source-evidence\evidence-receipt.json'
$expectedReceipt = '8f9c0a4aa3a8e53c9f1a945d272fdb4c4a704e87b354967772cf3870e4611644'
if ((Get-FileHash -LiteralPath $epaEvidence -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expectedReceipt) {
    throw '来源回执不匹配；停止，不使用替代或模拟数据。'
}
& $py scripts/audit_epa_no_api_fallback.py `
  --evidence-receipt $epaEvidence --expected-receipt-sha256 $expectedReceipt `
  --metadata-dir (Join-Path $epaRoot 'stage7-source-evidence-20261015') `
  --output (Join-Path $epaRoot 'epa-no-key-recheck-new.json')
# 输出必须不存在；重复执行请明确使用另一个新文件，不覆盖旧证据。
```

上述命令已实际运行过同等输入，产物是公开聚合回执；这里使用新的私有输出以保留原字节。新电脑缺少这些缓存时会明确失败，不会下载大包、自动构造成员或绕过验证。原始响应可由操作者沿第 1 节官方链接合法获取，并按 `Receipt` 严格合同记录获取 UTC、SHA 和元数据种类；**不把完整结果矩阵交给开发流程删列**。

如需重新核查小型官方文件目录，可在一个新的私有输出路径执行以下只获取目录的命令；这不是受测成员导出，也不会取得大 ZIP：

```powershell
$newIndex = Join-Path $epaRoot 'summary-files-recheck-new.json'
if (Test-Path -LiteralPath $newIndex) { throw '请使用新的输出名。' }
Invoke-WebRequest -Uri 'https://clowder.edap-cluster.com/api/datasets/687e392ae4b02565bc3e2914/files' `
  -TimeoutSec 30 -OutFile $newIndex
Get-FileHash -LiteralPath $newIndex -Algorithm SHA256
```

### 实际测试

```powershell
& $py -m unittest test.test_epa_no_api_fallback test.test_epa_structure_audit -v
& $py -m unittest discover -s test -p 'test_*.py' -v
npm test
& $py -m pip check
& $py scripts/verify_compare_preservation.py
```

- 新无 Key 来源测试 **17/17**；加既有映射测试合计 **35/35**。覆盖 SHA、端点 ID、来源类型、编码凭据/CTX 拒绝、字节预算、缺失输入、未知统计、库存与成员区别、档案哈希声明、Range 一致性、确定性与禁止覆盖；均为明确的合成软件夹具。
- 完整 CPU Python 回归 **772 项：770 通过、2 跳过、0 失败、0 错误**。两项跳过为 Windows 无 symlink 创建权限、POSIX 权限模式检查不适用。unittest 报告耗时 48.070 秒。
- JavaScript **135/135**；`pip check` 通过；历史保护检查 **50 个科研文件及 4 份原 Compare 回执未改变**。开始时额外记录的 116 个已有跟踪模型/工件/文档文件也逐字节复核。
- 本轮没有真实候选结构映射、标签保管方投影、模型预测、科学准确率、GPU、浏览器或下载后的真实映射验收。全套软件测试包含临时合成 toy 训练，未重新训练任何 A4/B16/Morgan 正式模型。

最终执行回执见 [verification.json](artifacts/independent-validation/stage7b-epa-no-api-20261017/verification.json)。测试通过不证明候选数据独立，也不证明许可或实验语义已获批准。

## 6. 下一步及停止条件

本轮停止在 Stage 7B。**`STAGE7C_BLOCKED`**，具体缺项为：

1. 由独立数据管理员在有权访问的官方 v4.3 缓存中，以事先冻结的脚本导出三个端点的完整无标签受测成员及来源记录；或者由 EPA 提供小型结果无关成员导出。大包只能在明确必要性、合法用途、磁盘/内存预算及单独下载授权下处理，本轮没有申请或执行全库下载。
2. 获取可核验的 DSSTox 结构快照和精确 DTXSID→DTXCID/SMILES/InChIKey 关联。保留 spid→chid→DTXSID 原始链；混合物、盐型、一对多、缺失结构及跨版本冲突分别隔离，不能用名称/CAS 猜测弥补。
3. 核实原 hERG 阈值/比较符/单位/边界及实验构成；分别审核结合 IC50、功能 IC50、AC50、hitcall 等，未经批准不互换、不生成新的二分类标签。
4. 完成结构/母体/骨架与历史全来源排除、文献/实验/批次/队列追溯和文件级许可审核；结构映射成功仍不能替代这些证据。
5. 冻结独立管理员、标签访问权限、评价规模与完整协议，再确认是否达到预先规定的评价要求。现有每类至少 10、至少 10 簇的 bootstrap 支持规则不是候选集合已合格或样本量足够的证明。

在全部资格证据齐备前不提出 `STAGE7C_CANDIDATE_READY`，不读取候选标签分布，不进行正式独立盲测，也不自动合并 PR。
