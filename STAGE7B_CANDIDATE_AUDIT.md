# Stage 7B：无标签候选独立性审计

2026-10-16（Asia/Shanghai）。**ChEMBL 先导样本有 17 个结构通过结构与重复筛查，但独立验证合格数为 0，不能进入 Stage 7C。** EPA 仅完成实验元数据审查，没有化合物结构输入，其结构通过数为 `null / not-run`。

本轮止于 Stage 7A/7B；没有候选结果值、阳阴性数、模型预测、盲测指标、训练、校准、实际标签封存或 Stage 7C/7D/7E 操作。以下计数来自实际采集与审计，单元测试的合成数据不参与这些统计。

## 分支、合同与证据

- main 基线：`3a7e5138398f8eea5f07ae14a083135bbc94d26b`。
- 新分支：`codex/stage7-labelblind-source-audit`，从 Stage 6 `7604ad1e9fb4ea77c6fa8879892e9fe9aa879c9e` 创建；未修改 main。
- PR #6 保持 Draft、未合并。所有 v3 继续 `formalAvailability=false`。
- 保留 Stage 6 的固定历史文件、身份策略、模型绑定和后续评价许可要求。Stage 7 的调查合同不是 `CandidateMetadata` 的批准或封存替代品；不伪造审核人、任务可比性或标签 SHA，不创建评价协议。

公开回执只含聚合、版本、来源引用及 SHA，不含分子/实验原始行、结果标签或实际私有路径：

| 文件 | 实际执行内容 |
|---|---|
| [ChEMBL 先导审计](artifacts/independent-validation/stage7b-chembl37-pilot-20261016.json) | 严格无标签投影、固定历史结构与来源家族排除 |
| [EPA 元数据审计](artifacts/independent-validation/stage7b-toxcast-metadata-20261016.json) | 两份固定 SHA 的官方 XLSX 元数据，未接入化合物 |
| [来源证据摘要](artifacts/independent-validation/stage7a-source-evidence-20261016.json) | 官方响应 URL、UTC、SHA、大小、实际核验状态及方案侧计数 |

ChEMBL 私有快照字节 SHA 为 `2b8cf8ad21ab9f0a0bb735c3227787cab1080fde724599d48e5f950fc0681f0e`，获取回执 SHA 为 `2ab195eaa322a2c2465f0c9a4c1876cfe5bc11af348632f6aa06e237e4deb812`，严格审计内容 SHA 为 `e2359e0ebb124f8c9cacb1abc68886d03184338b105f85de5ce00969bfc57c89`。字节 SHA 与规范对象 SHA 是不同摘要，不相互替代。

## ChEMBL：实际范围与盲化

固定目标为 human KCNH2 / `CHEMBL240` / UniProt `Q12809`。API 前后均实测 ChEMBL 37，status 内容相同。以 `activity_id` 升序取首 200 条，不使用实验值、活性分类、模型分数、正负比例或文献新旧筛选。该次 API 显示总目标活动元数据 41,078 条；本轮不是全库筛查。

实际取得 200 条活动元数据、28 个 assay 元数据和 13 个 document 元数据，成功快照 HTTP 响应合计 99,738 bytes。服务端 `only` 排除 `value/standard_value/relation/pchembl_value/activity_comment` 等结果字段；首次响应自动附带非结果 `type/units` 后曾停止，确认字段性质后显式请求并严格校验，未放宽结果字段限制。

私有缓存位于本机科研目录、仓库及 OneDrive 之外。离线审计必须给出事先记录的获取回执 SHA，再核验每份响应的原始字节 SHA，并从响应重建 bundle 对比成员和顺序。仅重新计算一个 bundle 自身的 hash 不能替代这些关联核验。

## 结构与重复排除结果

固定历史参考的 11 个结构角色均存在并通过原有文件/成员 SHA 核验，`completeReferences=true`，参考摘要为 `07a8f334af4cd78a1c99971b63fd20ef7ec101c473651ae1ea5b3da7beaefce0`。

| 审计量 | 实际数量 |
|---|---:|
| 输入活动元数据行 | 200 |
| 具有有效、受支持结构的行 | 200 |
| 缺少结构 / 非法或不支持结构 | 0 / 0 |
| 唯一 canonical 结构 / 唯一分子 ID | 186 / 186 |
| 重复 canonical 组 / 涉及行 | 13 / 27 |
| 重复分子 ID / 涉及行 | 13 / 27 |
| 直接命中历史任一结构层级的唯一结构 | 166 |
| 仅经候选连通分组传播而追加污染的唯一结构 | 0 |
| 与固定历史结构索引互斥的唯一结构 | 20 |
| 通过结构与重复筛查的唯一结构 | 17 |
| 通过全部独立验证资格的结构 | **0** |

同一结构的多次实验不依据未读结果择优保留，重复相关记录全部隔离。全部有效结构先做身份连通分组，再排除历史污染和重复；去盐/互变对象不能通过删除桥接记录逃逸。去盐、中和、去立体/同位素只用于排除，不改变模型输入化学身份。原 `[ACYCLIC]` 保守规则继续生效。

按结构层级命中的唯一结构数如下；**层级之间相交，不能相加**：

| 身份层级 | 唯一结构命中数 |
|---|---:|
| canonical | 72 |
| 去立体 / 同位素近等价 | 121 |
| 去盐中和母体 | 158 |
| 互变异构母体 | 161 |
| Bemis–Murcko 骨架 | 166 |

按历史角色命中的唯一结构数也不能相加：v2 train 51、validation 22、test 18；v3 四任务 train 32、validation 7；v3 十六任务 train 18、validation 21；其 audited 副本同为 18/21；已曝光 Karim 池 90；历史 Tox21 源 38。

这里的“历史参考完整”只指 Stage 6 固定参考合同：Karim 的结构角色是已曝光 **9,978** 分子池，不能把它写成对原始 13,445 行的重新全量结构审计。整个 Karim/ChEMBL 文献汇编来源另外受到来源家族阻断；17 个结构通过不证明其未出现在原始源、论文、队列或预训练语料中。历史实验出处尚不完整，未据结构侧通过放行。

按优先级形成的**互斥行处置**如下，合计正好 200：

| 首要处置原因 | 行数 |
|---|---:|
| 历史结构 / 骨架重叠 | 177 |
| 剩余候选 canonical 重复，全部隔离 | 6 |
| 结构与重复筛查通过，但记录元数据仍不完整 / 不一致 | 8 |
| 结构与重复筛查通过，仍待任务、权利及来源审核 | 9 |

20 个互斥结构中有 3 个重复结构组，涉及上述 6 行，故最后留下 17 个结构。另有 27 行涉及重复的总计包含已被历史重叠先行隔离的行，不能再从 200 中重复扣除。

## 实验语义与来源结论

实际测量定义如下，**没有读取测量结果或建立阳性阈值**：

| 元数据终点 | 单位 | 行数 |
|---|---|---:|
| Ki | nM | 105 |
| IC50 | nM | 50 |
| IP（含义待核） | nM | 32 |
| EC10 | nM | 3 |
| EC25 | uM | 1 |
| Inhibition | % | 9 |

严格结构审计回执将尚未审核的 IP/EC10/EC25 统一列为 `other-unreviewed`（nM 35、uM 1）；来源证据摘要另保留上述真实定义计数，二者是同一批记录的不同展示，不是标签重映射。

assay 粗分类对应的记录为 B=140、F=59、T=1。记录的实验物种为人 138、非人 18、未指定 44。28 个 assay 中，人类 18、豚鼠 4、雪貂 1、未指定 5。**目标标为人类并不能使这 200 条记录全部成为人类功能性阻断实验。** 62 条记录未同时满足人类 assay 物种与直接目标映射的方案侧要求；两类原因计数都为 62，不能当成 124 个不同样本。

13 个 document 均带 DOI，年份 1997—2004；部分是综述/计算研究，DOI 可追踪不等于原实验已经追踪。原始引用及方案、单位、许可依据见 [Stage 7A](STAGE7A_DATA_SOURCE_REVIEW.md)。没有直接把 Ki、不同 IC50、百分比和其他测量合并成 hERG 标签。

来源家族检查实际命中历史 `tdc-herg` 与 `herg_karim` 的 `herg-literature-compilations`；200 条记录均保持保守来源阻断。Karim 明确整合过 ChEMBL/BindingDB/DeepHIT，不能以数据库来源不同认定独立。

文献、assay ID 已取得并用于来源身份比对，但历史 `wang-2016-herg` 等别名没有完整 DOI/assay/队列映射。没有字符串命中不代表来源互斥；队列独立、原始试验实体和跨库导入关系均为 `unresolved`。本轮没有伪造缺失的 cohort ID，也没有将缺失记录当作“无交叉”。

这 17 个结构仍共有五类阻塞：任务等价性未审核、选定用途权利未审核、来源总体独立性未证明、逐文献/实验/队列映射不完整、历史 hERG 来源家族不互斥。全部候选未被授予进入科学验证的资格。

## EPA 备选审查结果

实际离线核验 EPA 官方 invitrodb 4.3 的 assay annotations 和 target mappings 两个小文件 SHA，按 hERG/KCNH2 元数据检索得到 **3 个实验端点**：NVS_IC_hKhERGCh、ERF_TW_IC_hKCNH2、TOX21_hERG_U2OS_Antagonist。

- 2 个为 radioligand binding reporter，1 个为 functional reporter；3 个均为 human 目标元数据与 `percent_activity` 定义。
- 1 个端点来自 Tox21 家族，不能作为新的独立来源直接放行。
- 1 个存在 functional design 与 binding function 字段不一致，需实验方案核查。
- 目标映射缺少 assay 注释数为 0；这只说明这份元数据关联完整。
- 固定 Figshare 发布 API 返回 403，具体发布许可本轮未重核成功。6 份已缓存来源响应已重新核对字节 SHA；403 的失败响应仅有当时回执，原响应体未缓存，公开证据摘要明确标注无法再次核验该响应字节。

没有 EPA 化合物结构或活动结果输入，故候选结构数、结构通过数和独立合格结构数均为 `null`。不把“找到三个实验端点”写成“找到三个合格化合物”，也不把缺少结构的审计写成通过零个或全量完成。

## 新增代码和复现

| 文件 | 边界 |
|---|---|
| `backend/stage7_audit.py` | 新的预资格聚合，复用历史身份、来源与连通分组；始终不能授予封存/评价资格 |
| `scripts/survey_chembl_labelblind.py` | 固定官方主机、ChEMBL 37 前后核验、严格服务端投影及查询、最多 200 行 / 30 次请求 / 5 MB、离线响应重建 |
| `scripts/audit_toxcast_metadata.py` | 固定两个官方 XLSX SHA，标准库离线读取元数据，不读活动表 |
| `test/test_stage7_audit.py`、`test/test_survey_chembl_labelblind.py`、`test/test_toxcast_metadata_audit.py` | 合成软件夹具，不产生真实实验资格或科学数值 |
| `.gitignore` | 仅精确放行三份聚合回执；既有 `-text` 属性保持工件字节 |

现有 Windows 环境无需安装新依赖、下载权重或启用 GPU。仓库根目录 PowerShell：

```powershell
$stage7Py = '.\.venv-v3\Scripts\python.exe'
$env:TOXNET_VALIDATION_ALLOW_LABELS = 'false'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$stage7Private = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation\stage7-chembl37-pilot-20261016'

# 使用本轮已存在的私有无标签快照；输出用新名字，不能覆盖本轮回执。
& $stage7Py scripts/survey_chembl_labelblind.py `
  --private-dir $stage7Private `
  --receipt-sha256 2ab195eaa322a2c2465f0c9a4c1876cfe5bc11af348632f6aa06e237e4deb812 `
  --output artifacts/independent-validation/stage7b-chembl-local-repeat.json

$stage7Epa = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation\stage7-source-evidence-20261015'
& $stage7Py scripts/audit_toxcast_metadata.py --private-dir $stage7Epa `
  --output artifacts/independent-validation/stage7b-epa-local-repeat.json
```

历史参考索引构建需要数分钟 CPU 化学规范化；不用 GPU，也没有推理或标签访问。缺少快照、SHA 不匹配、历史文件缺失或环境版本不符时应修复依赖或停止，不能用测试夹具替代。上述私有缓存不会随 Git 克隆获得。

需要重取一份**同样有界的官方无标签元数据**时，使用新的私有目录；下列命令只申请最多 200 条元数据，不下载全库。API 已升级到其他版本时明确失败，不默默接受新版本：

```powershell
$stage7NewPrivate = Join-Path $env:LOCALAPPDATA ('AIDD-Blue-ToxNet\scientific-validation\stage7-chembl-' + (Get-Date -Format 'yyyyMMdd-HHmmss'))
& $stage7Py scripts/survey_chembl_labelblind.py --fetch --private-dir $stage7NewPrivate `
  --output artifacts/independent-validation/stage7b-new-metadata-only.json
```

脚本不继续翻页至全库，不自动申请大规模下载。EPA 缺少缓存时从 Stage 7A 的官方链接取得**指定小型元数据文件**，保持固定文件名并核验 SHA；不要下载活动/命中矩阵。

## 实际软件验证

```powershell
& $stage7Py -m unittest test.test_stage7_audit test.test_survey_chembl_labelblind test.test_toxcast_metadata_audit test.test_independent_validation_data -v
npm test
& $stage7Py -m pip check
& $stage7Py scripts/verify_compare_preservation.py
git diff --check
```

本轮实际执行结果：

| 检查 | 实际结果 |
|---|---|
| 上述四个 Python 测试模块 | **59 项全部通过**，其中 Stage 7 新增 36 项、原数据合同回归 23 项；0 失败，0 跳过 |
| `npm test` | **135 项全部通过**，0 失败，0 跳过 |
| `.venv-v3` 的 `pip check` | `No broken requirements found` |
| 六份新增 Python 源码/测试的 `py_compile` | 通过 |
| `verify_compare_preservation.py` | `unchanged`：50 份历史科研文件、4 份原 Compare 回执 |

软件测试包括严格无结果字段、非法结构、层级交集、重复/分组传播、缺失来源、缓存 SHA 篡改、结果值排序拒绝、未完成资格、EPA 元数据关联及禁止覆盖；均为 CPU 软件夹具，不等于科学验证。完整 Python 全套与浏览器交互本轮未重跑：没有改变 HTTP、工作台或模型推理路径。实际模型训练、推理、GPU 测量及独立盲测不在本轮执行范围内。

## 明确结论与人工待办

本轮找到真实可追溯的 ChEMBL 活动/结构元数据及 EPA 实验方案线索，尚未找到满足全部准入条件的独立实验集合。ChEMBL 先导样本结构与去重通过 17、最终合格 0；EPA 结构未审计。该结论不能外推为 ChEMBL 37 全库或所有外部来源均无可用数据。

进入下一阶段前，需人工确认原 TDC/Wang 任务判定合同；由数据保管人补齐候选论文—原始实验—实体—队列与历史别名映射；解决方法/物种/单位及 EPA 注释冲突；核对选定用途的许可；提供保持标签封闭的独立性审核证据。未来即使补齐这些证据，仍需另行批准 Stage 7C，不能把本轮调查授权当作盲测权限。

目前 **不可以进入 Stage 7C**。未产生独立 AUROC、AP、Brier、ECE、阳阴性统计或新模型准确率排名；MoLFormer 预训练结构曝光仍未知。
