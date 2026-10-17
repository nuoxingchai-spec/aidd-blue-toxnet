# Stage 7B：无 Key 镜像可行性核验

日期：2026-10-17（Asia/Shanghai）。基线 `cf2434ccbbf8c3d38dd1df51c7678280167499aa`，分支 `codex/stage7-labelblind-source-audit`。结论：**`STAGE7C_BLOCKED`**。

两个项目的源码均可访问。Scigantic 的公开 S3 对象通过了 HEAD 可访问性检查；Biobricks 所测的公开 DVC 索引没有取得。**两个镜像都不能在本轮被认定为已核验、完整、标签隔离的 v4.3 实际受测成员来源。** 尚未获得身份投影，三个 AEID 的实际存在性、成员数、单浓度及缺失覆盖均为 `null / 未验证`，没有用仓库声明、总体规模或结构覆盖声明代替统计。

本轮只增加本报告和白名单聚合证据，不改生产模块、依赖、正式模型或历史报告；不安装/导入第三方包，不执行外部构建脚本，不请求 CTX，不训练、预测、评价或封存，不展示任何候选活性值、结果类别或标签分布。第三方仓库描述文件中发现了 profile 容器，处理边界见第 5 节；**不能宣称镜像标签隔离已获完整证明**。

## 1. 固定修订与可访问性

| 项目 | 审查的固定 Git revision | 实际可访问范围 | 结论 |
|---|---|---|---|
| [Scigantic/scigantic-comptox](https://github.com/Scigantic/scigantic-comptox/tree/888a433667aa9bf37d7bf87907f4a8426c253171) | `888a433667aa9bf37d7bf87907f4a8426c253171` | GitHub 元数据/源码、公开 `_MANIFEST.json`；5 个 S3 对象 HEAD 均 200 | 可以继续向维护者/独立管理员核对来源和身份投影；本轮未 GET Parquet 或 footer |
| [biobricks-ai/toxcast](https://github.com/biobricks-ai/toxcast/tree/a2008912e1019e2f90f670700f08d7483c2b1931) | `a2008912e1019e2f90f670700f08d7483c2b1931` | GitHub 元数据/源码可访问；所测 4 个 DVC 目录索引 GET 均 404 | 数据对象可用性未确认；404 只适用于所测路径，不能推断所有镜像服务不可用 |

实际网络证据：Scigantic **19 次源码/元数据 GET，111,906 bytes**，另有 **5 次不读正文的 HEAD**；Biobricks **21 次仓库元数据/源码或 DVC 索引请求，88,769 bytes**（17 个 200、4 个 404）。合计 40 份 GET 响应、200,675 bytes；其中部分仓库描述正文在风险识别后已移除，仅保留获取回执和整文件哈希，详见第 5 节。

对两个固定 Git tree 共核对 **28 个仓库文件的 Git blob SHA1**，字节一致，并计算本地 SHA256。这里的文件包括文档和描述元数据；哈希一致不表示内容经过标签隔离批准，也不证明转换过程实际执行过。公开 [可行性聚合证据](artifacts/independent-validation/stage7b-mirrors-20261017/feasibility.json) 只列安全来源字段、哈希、大小、UTC 和人工审查状态，不含原始描述正文、实验行或结构。

### Scigantic 数据对象：仅 HEAD

基址为 `https://scigantic-comptox.s3.us-east-1.amazonaws.com/v4_3/`。

| 对象 | HTTP | 服务器报告大小（bytes） | 完整数据 SHA256 / VersionId |
|---|---|---|---|
| `bioactivity/mc5_6_winning_model_fits.parquet` | 200 | 666,287,217 | 未取得 |
| `derived/pubchem_bridge.parquet` | 200 | 62,537,446 | 未取得 |
| `derived/structures.parquet` | 200 | 1,666,679 | 未取得 |
| `reference/assay_annotations.parquet` | 200 | 918,962 | 未取得 |
| `reference/assay_target_mappings.parquet` | 200 | 71,684 | 未取得 |

HEAD 记录的 Last-Modified 为 2026-08-31 或 2026-09-01，不是 EPA 原始实验日期。ETag 已记录但**不能作为 SHA256 或完整本地验证**；部分 ETag 是分段上传形式。对象可达不等于内容/schema/成员覆盖已经核验。

Biobricks 的 `.dvc/config` 声明 remote 为 `https://ins-dvc.s3.amazonaws.com/insdvc`。依据 `dvc.lock` 的两个目录 MD5，仅检查 legacy 与 `files/md5` 两种标准索引布局，未枚举猜测其他对象或绕过访问限制。输出目录声明 `1dda6a53f75c17d913ca7be0dbb8f1a3.dir`、4 文件、783,268,567 bytes；这是 DVC 声明，不是本轮取得的数据或成员统计。

## 2. v4.3 血缘与哈希：没有闭环

### Scigantic

- [固定 connection.py](https://github.com/Scigantic/scigantic-comptox/blob/888a433667aa9bf37d7bf87907f4a8426c253171/src/scigantic_comptox/connection.py#L166) 按 `release` 构造该对象路径，当前 manifest 声明的版本为 `v4_3`；源码本身没有将调用者固定在这一数据修订。
- 实际取得的 [S3 manifest](https://scigantic-comptox.s3.us-east-1.amazonaws.com/_MANIFEST.json) SHA256 为 `b028ad6522cb3281197ad065b2a7673a6cc2ecada1d946bc94601e826403def3`，generated 字段为 `2026-09-01T21:00:00Z`，声明 release `v4_3`。
- manifest 没有 EPA 源文件 URL/SHA、镜像对象 SHA 或构建证明；固定仓库 tree 中没有生成这些数据对象的完整构建流水线。不能从 release 名、文件名和上传时间推出它与官方 v11 逐字节或逐行相同。
- `latest()` 使用可变 manifest，失败时回退到随包快照；研究流程不能把该回退当成已验证的固定数据修订。
- 包中的 live EPA 查询与 S3 镜像路径是不同能力；S3 的无 Key HEAD 成功不能证明整个包的所有 API 都无需授权。本轮没有调用其 CTX client。

### Biobricks

- [固定下载脚本](https://github.com/biobricks-ai/toxcast/blob/a2008912e1019e2f90f670700f08d7483c2b1931/stages/01_download.py) 指向官方 Clowder `68af6b70e4b02565fc7c3a98/blob`，与上一轮同一个 Summary ZIP 文件 ID，预期体积 7,502,075,199 bytes。下载器只检查体积，没有固定官方整包 SHA256。
- [固定构建脚本](https://github.com/biobricks-ai/toxcast/blob/a2008912e1019e2f90f670700f08d7483c2b1931/stages/02_build.py) 读取 `mc5-6_winning_model_fits-flags_invitrodbv4_3_AUG2024.csv`，转换为 Parquet。本轮两个 stage 脚本的 MD5 与 `dvc.lock` 相符；源码没有额外按 AEID 或阳性类别筛选，但输入本身已经是 winning-model 汇总。
- DVC MD5 是其内容寻址声明，不等于官方源包 SHA256；目录索引未取得，也没有本地数据验证。不能仅凭 lock 文件认定数据工件已经真实复现。
- 旧 `analysis/dump-ddl.sh` 仍指向 invitrodb **v4.1**，不能当成 v4.3 数据来源或查询依据。

前轮已记录的版本问题继续存在：EPA 说明页指向固定 DOI v11，Clowder/镜像描述提及 v14；Summary 内文件名为 `AUG2024`，正文/上传记录说明 August 2025。原报告中的发布方 ZIP SHA 仍未通过本地整包验证。本轮没有放宽这些差异，也没有将两个镜像视为两个独立实验来源。

## 3. 三个 AEID、身份字段及覆盖范围

固定官方注释先前已确认 AEID/ACID/AID 分别为 **686/423/237、3184/2914/897、3210/2939/910**。这是官方元数据事实，不是这两个镜像中存在对应数据行的证明。

| AEID | Scigantic 源码的按 AEID 过滤能力 | Biobricks 源码/schema 的能力 | 镜像实际存在性、MC 行数、唯一 spid/chid/DTXSID | SC 行数、完整总体缺失数 |
|---|---|---|---|---|
| 686 | 有参数化 `aeid = ?` | 声明 `aeid` 列，理论可由受审投影过滤；没有专用查询服务 | 两者均 `null / 未验证` | 两者均 `null / 无法确认` |
| 3184 | 同上 | 同上 | 同上 | 同上 |
| 3210 | 同上 | 同上 | 同上 | 同上 |

[Scigantic bioactivity.py](https://github.com/Scigantic/scigantic-comptox/blob/888a433667aa9bf37d7bf87907f4a8426c253171/src/scigantic_comptox/bioactivity.py#L55) 的 AEID 参数说明代码具有过滤能力，但其后默认 `SELECT *`，没有在本轮运行。身份列在连接源码中包括 `spid/chid/dsstox_substance_id`，后者另名 `dtxsid`。Biobricks 的声明 schema 同样列出这些身份字段；尚未从真实数据核验列型、空值或唯一性。`acid` 没有在该主表声明中得到确认，不能把 `chid` 或 `aid` 误当作它。

覆盖必须分层：

1. **多浓度已处理摘要覆盖**：两者都以 `mc5–6 winning-model` 表为主要数据源。即使以后取得身份行，也只能证明该样本在该处理层有记录。
2. **原始多浓度实际受测总体**：原始记录、未能进入该处理层或失败拟合记录覆盖未知，不能补推为零。
3. **单浓度覆盖**：受审流水线未提供 `sc1/sc2` 成员表；这不意味着 EPA 没有单浓度成员，实际成员数继续未知。
4. **相对完整总体的缺失数**：缺少官方完整分母、处理链和样本来源/批次映射，无法计算。也未使用第三方 profile 的行数或缺失率代替它。

本轮没有安全恢复的真实身份投影，故不报告“查询结果为 0”或任何推算样本数。`spid` 是 sample，`chid` 是 invitrodb 内部 chemical 索引，DTXSID 是物质；三者不能互相代替，DTXSID 也不等于唯一分子结构。

## 4. PubChem 结构映射与许可

本报告使用自定义证据层级：**L0** 未见映射来源；**L1** 维护者声明/对象可达；**L2** 真实标识关联、歧义和结构一致性已核验；**L3** 另有官方快照、完整血缘及用途权利审核。它不是标准科学证据分级，也不代替独立资格门禁。

| 项目 | 本轮证据等级 | 已确认 | 未确认 |
|---|---|---|---|
| Scigantic | **L1** | `structures.parquet` HEAD 200；[structures.py](https://github.com/Scigantic/scigantic-comptox/blob/888a433667aa9bf37d7bf87907f4a8426c253171/src/scigantic_comptox/structures.py) / README 声称由 DTXSID synonym 查询 PubChem | 实际 CID/结构对应、PubChem 时间快照、映射生成脚本、一对多处理、缺失/盐/立体/同位素冲突、文件权利均未核验；映射和缺失数量均 `null` |
| Biobricks | **L0**（受审仓库范围） | 本流水线不提供 PubChem 结构映射；声明主表没有 SMILES/InChIKey | 不能自动借用另一个镜像的结构并称为同源；需要另行绑定结构快照和映射证据 |

Scigantic 的 [pubchem_bridge.py](https://github.com/Scigantic/scigantic-comptox/blob/888a433667aa9bf37d7bf87907f4a8426c253171/src/scigantic_comptox/pubchem_bridge.py) 是 EPA 向 PubChem BioAssay 提交数据的整理表，源码文档列 `aeid/tx_sample_id/dtxsid`。**`tx_sample_id` 不是 PubChem CID，表名 bridge 不表示已取得 DTXSID→CID→结构的可靠桥接。** 该对象本轮也没有下载。

以精确 DTXSID 字符串调用 PubChem 的通用 name/synonym 检索，比猜名称/CAS 更明确，但仍须证明它对应正确物质、CID 和原受测实体；不能默认唯一，不能把标准化母体代替盐或混合物。没有取得实际映射行，因此没有对候选执行 RDKit 化学身份确认，也没有按 CAS 或名称自动补配。

Scigantic 代码许可证为 **MIT-0**，不自动覆盖 EPA/供应商实验数据和 PubChem 贡献内容。Biobricks 仓库未检测到 LICENSE；其描述中的公共领域/CC0 声明不是本项目已完成的文件级权利审查。官方 [PubChem copyright 页面](https://pubchem.ncbi.nlm.nih.gov/docs/copyright) 实际返回 HTTP 200，但内容是浏览器挑战，未取得政策正文，未绕过。故**合法可用于本项目的具体结构映射文件尚未获确认**，本地科研、再分发与商业使用条件均保留人工核查。

## 5. 标签隔离与依赖审查

本轮没有 GET 活动 Parquet、读取其 footer/列统计、查询结果接口或调用包的 `connect()`。Parquet footer 可能含结果列最值等统计，不能把开发进程中“只选身份列”当作已经证明标签盲态。

另有一项必须披露的边界：Biobricks 的 `.bb/validation.jsonld` **不是纯 schema**，包含结果列 profile 的容器。检查只揭示了键/类型，没有输出或采用具体值；相关 profile 不用于成员覆盖、缺失数或任何筛选。为避免后续缓存传播，本轮新取得的 `.bb/` 描述正文及可能携带描述 diff 的 `main-commit.json` 共 **6 份正文已移除**，仅保留来源、获取回执、字节大小和整文件哈希。未改变历史缓存或用户原始数据。**不能声称这些自由文本已被证明完全 label-free，也不以本次源码审查替代独立数据管理员。**

| 项目 | 受审安全/重现问题 | 本轮处置 |
|---|---|---|
| Scigantic | `connect()` 自动 `INSTALL/LOAD httpfs`；建立远端全列原始/衍生视图；现成查询默认全部列 | 不安装、不导入、不调用，不使用默认 CLI 或缓存功能 |
| Scigantic | `duckdb/pandas/requests` 为宽版本范围，无依赖锁；缓存下载不验证对象 SHA，已有文件可直接复用；live `latest` 可漂移 | 不能作为本项目已验收供应链；将来需固定 wheel/扩展/对象哈希及受审加载方式 |
| Biobricks | 下载构建脚本会整表读取结果列；PyArrow 写出使用 `delete_matching`；依赖无锁；workflow 使用浮动 `@main` | 只静态审阅，不执行脚本、DVC pull/repro 或 workflow |
| 仓库描述 metadata | 某些元数据可含结果 profile，不是安全身份投影 | 不呈现/采用其结果摘要；移除相关新缓存正文，保留人工阻塞 |

这些是静态风险与缺失证据，不是“发现恶意代码”或“已完成漏洞扫描”的结论。本轮未做第三方包安装、动态沙箱或 CVE 完整审计。

### 交给独立管理员的固定投影要求（未执行）

管理员需先确认用途权利、确切数据对象、完整 SHA256、schema 和工具链，再冻结投影及异常处理。开发流程只接收无结果的身份文件与来源/完整性证明。

```sql
-- 仅为管理员审阅草案，本轮未运行；列型和原文件仍需核实。
SELECT aeid,
       NULLIF(spid, 'NA') AS spid,
       NULLIF(chid, 'NA') AS chid,
       NULLIF(dsstox_substance_id, 'NA') AS dtxsid
FROM read_parquet('<管理员已固定并核验的本地对象>')
WHERE aeid IN (686, 3184, 3210);
```

不能出现 `SELECT *`、结果条件/排序、活性或 QC 筛选，也不能让异常日志、自由文本、原文件缓存或输出统计泄露结果。须固定仓库 commit、构建脚本 SHA、对象版本与 SHA、投影 SQL/SHA、UTC、输出投影 SHA；输出只允许预审身份列。ACID 如需补充，必须取自已固定官方 AEID→ACID 对应，不凭名称猜测。

该草案只恢复已处理表的覆盖，不授予“完整受测成员”资格；独立管理员、隔离权限和完整原始 MC/SC 覆盖证明仍是前提。当前缺少这些条件，因此未执行该草案，也没有实现自动下载/放行入口。

## 6. 独立性审计与 Stage 7C 门禁

没有真实身份和结构投影，本轮**没有执行候选历史重叠计算**，canonical/母体/互变异构/立体同位素/Murcko 骨架、来源及合格结构数量全部未知。未来得到映射后仍必须沿用现有冻结规则：历史 train/validation/test、Karim、Tox21 全来源排除；先分组再传播历史污染；文献/批次/队列/供应商/来源家族分别审查，不为数量放宽规则。

镜像只改变获取途径，不产生新的实验独立性。3210 的 Tox21 来源家族暴露、原始 hERG 标签合同未决、完整历史 Tox21 的 9 条不支持结构等前轮阻塞继续保留。不能由镜像摘要自行生成原 hERG 标签，也不能将 ToxCast readout 等同原 IC50 定义。

现有 `epa_structure_audit.py` v1 合同只接受已审官方来源与固定版本；不能将第三方 URL 伪装成官方来源。以后如引入镜像，应新增明确的镜像 provenance 合同，并在它之外审查资格，而不是放宽旧门禁。

仍需 EPA 官方文件、维护者补证或独立管理员提供：

1. EPA→镜像对象的完整文件哈希/版本/构建闭环，及明确的许可和用途范围。
2. 无标签身份投影；逐 AEID 的已处理 MC 行数和唯一身份数；独立的原始 MC、SC 与缺失覆盖证明。
3. 可追溯的 DTXSID→CID/DTXCID→结构快照及所有歧义/缺失的隔离记录。
4. 冻结标签隔离、完整历史结构/来源排除、原 hERG 科学合同与预定评价规模。

当前无完全合格候选集，**`STAGE7C_BLOCKED`**，不开展独立评价。

## 7. 可复现命令与实际验证

下列 PowerShell 命令只核对公开对象响应头，不下载实验数据；本轮已执行等价 HEAD：

```powershell
curl.exe --head --max-time 20 `
  'https://scigantic-comptox.s3.us-east-1.amazonaws.com/v4_3/bioactivity/mc5_6_winning_model_fits.parquet'
curl.exe --head --max-time 20 `
  'https://scigantic-comptox.s3.us-east-1.amazonaws.com/v4_3/derived/structures.parquet'
# 不运行 pip install scigantic-comptox、dvc pull/repro、connect() 或 read_parquet。
```

两个镜像的源文件审查均绑定上列固定 commit；公开聚合记录相应原始 URL 和本地 SHA256，未提交第三方源码或数据。举例，Scigantic `connection.py` 的 SHA256 为 `382ab7c7c7a8968b82dc4776e206a1b292caaf7e87c2b882cf7eb45abfe87da8`；Biobricks 下载/构建脚本分别为 `8001b5dacac640643b916526cacc2b01fda7964da1f6c11243e2c312313d787e` / `99e18a0e6c88df64253b6884714ba61b68c1178b9b8168c38fa792394030b197`。源文件哈希不能替代数据哈希。

本轮实际执行：

```powershell
$py = (Resolve-Path '.venv-v3\Scripts\python.exe').Path
& $py -m unittest test.test_epa_no_api_fallback test.test_epa_structure_audit `
  test.test_stage7_audit test.test_stage7b_supplement_audit -v
& $py -m pip check
& $py scripts/verify_compare_preservation.py
```

- **57 项相关软件测试全部通过**，耗时 0.401 秒；使用合成软件夹具，不是真实镜像成员验证。
- `pip check` 通过。50 个历史科研文件、4 份原 Compare 回执及开始时记录的 119 个既有跟踪模型/工件/文档文件保持不变。
- 另核验缓存字节 SHA256、固定 Git blob、DVC stage 脚本哈希、HEAD 无正文及公开产物隐私；这些是来源/工程检查，不是候选科学验证。
- 本轮未重跑完整 Python/JS、浏览器、GPU、真实镜像查询、映射或预测评价；没有前后端运行代码变更，未把上轮 772/135 项结果冒充本轮执行。

执行范围见 [verification.json](artifacts/independent-validation/stage7b-mirrors-20261017/verification.json)。main、历史科研记录和正式模型保持不变；`formalAvailability=false`；PR #6 仍为 Draft、未合并。
