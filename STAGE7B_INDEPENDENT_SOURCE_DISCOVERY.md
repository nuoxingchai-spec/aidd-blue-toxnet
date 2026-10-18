# Stage 7B：独立实验来源调查

证据获取：2026-10-17；报告收尾：2026-10-18（Asia/Shanghai）。**优先交管理员核实两项 2025 年原始文献候选：DOI `10.1021/acs.jmedchem.4c02635` 与 `10.1021/acs.jmedchem.5c00231`。它们只是更值得核查的来源，不是合格独立数据集。最终状态仍为 `STAGE7C_BLOCKED`。**

本轮没有扩大 EPA 第三方镜像研究。调查指定 PubChem AID 588834、1671200、1511 与 ChEMBL 官方 assay/document 元数据，不请求候选活动数值、分类、结果分布或模型输出，不训练、微调、校准、封存或评价。

## 项目状态与原合同

开始时工作区干净，HEAD `89bc076568bd0e5155257cba5cb508ddf2760e87`，研究分支 `codex/stage7-labelblind-source-audit`。只读 `git ls-remote` 核实 main 仍为 `3a7e5138398f8eea5f07ae14a083135bbc94d26b`，Stage 6 远端分支为 `7604ad1`，远端没有本 Stage 7 分支。开始时 `7e47c55/013a4c9/cf2434c/89bc076` 共四个提交未推送；本轮不推送或改 main。

已读 Stage7A 来源报告、初始候选审计、EPA 无 Key 报告、镜像可行性报告、独立验证协议及 hERG 合同复核。保留其历史字节，不改旧实验结论。

原任务为 Wang/TDC hERG，Dataverse `4259588`，原文件 SHA256 `f2b00c05f12763c9da91d46246800e30925a626d5038e15eec366d633bdffe54`。只确认 **blocker=1 / non-blocker=0**；统一阈值、比较符、单位、边界和 assay 组成仍未知。冻结 prereview policy SHA 为 `720daf2db990068fc43f2ac70ff45a71a095ed40803a722e9bcc2030ce67ffe7`，标签映射仍不可执行。不能借用 Karim 的阈值，也不能从候选结果反推合同。

## 本轮实际发现

- **PubChem 官方身份路径可用**：588834 的 `all SID` 为 5,381 个，1671200 为 9,667 个，共享 8 个 SID；这些不是标签分布或独立结构数。1511 请求超过冻结 5 MiB 上限，停止且不保存部分响应，数量未知。1511 的科学目标为保护/救援而非直接阻断，不优先为原任务处理。
- **ChEMBL 新来源窗口**：ChEMBL37 官方 source-only 投影取得 34 篇文献、关联人类 `CHEMBL240` 的 88 个 assay，文献年份 2020—2025。方法词投影中 53 个出现 patch-clamp 词；这是方法元数据，不是已经确认的实验类型、效力或质量。
- **来源相同不等于独立**：与先前有限缓存的 161 个文献、237 个 assay 相比，本轮文献 ID、DOI、assay ID 精确交集均为 0。该缓存不是完整训练来源书目，不能据此放行。
- **没有候选结构资格结果**：本轮未请求 CID/SMILES 或 ChEMBL 活动/分子记录。canonical、母体、互变异构、骨架和来源全覆盖审计尚未执行，结构通过/合格数均为 `null`。

详细实验与身份审查见 [PubChem 报告](STAGE7B_PUBCHEM_ASSAY_REVIEW.md)，来源排序与下一步见 [资格矩阵](STAGE7B_SOURCE_ELIGIBILITY_MATRIX.md)。

## ChEMBL 官方检索与范围

API 基址：[ChEMBL data service](https://www.ebi.ac.uk/chembl/api/data/)。最终有效查询先在 `document` 资源以 `title__icontains=hERG`、`year__gte=2020`、`order_by=-year`、固定上限 200 选择来源，仅返回 document ID、DOI、PMID、journal、year、source ID 和 document type；**不返回标题、摘要或结果**。随后用这些文献 ID 与 `target_chembl_id=CHEMBL240` 关联 assay，只返回身份/物种/细胞/类型/目标映射等白名单。不是 ChEMBL 所有 KCNH2 文献的完整调查，会遗漏标题未提 hERG 的来源。

最终成功窗口共 **4 个 HTTP 200 响应、33,007 bytes**。前后 status 字节完全相同，均为 ChEMBL37，SHA `29dc1fb09487a8d2962253a4d0d92c4ded0aa0d831effab42445d0c3483d4740`。在线 API 是可变服务，必须同时固定响应 SHA、获取 UTC 和计划；不能只写版本名。

所有关联 assay 的元数据为人类目标 direct mapping/score 9；类型分布为 B 34、A 2、T 52。这是 assay 方法分类，不是候选活性类别分布。分类和 confidence score 都不能证明真实电流测量、实验可靠性或科学资格。两个优先文献来源对应 assay 为 `T`，不是因为它们的样本数或模型成绩而被选择。

方法核查另有冻结计划，只请求 assay ID + description，在受限内存匹配预先列举的方法词，输出 assay ID + 枚举；自由文本、数值和原响应正文不保存或展示。仅出现 patch-clamp/whole-cell 不排除其在讲背景、对照或复用旧实验，必须由独立管理员提供无结果的原始协议证明。

### 必须保留的失败检索证据

前置尝试均只请求元数据，未用于最终新来源或数量结论：

1. status 响应合同初次不匹配，停止且不缓存未知内容。
2. 按 `-assay_chembl_id` 取先导页时发现它是字符串排序，不是发表年代，不能据 ID 较大宣称研究新。
3. `assay` 查询中未知的 `document__year__gte` 参数被服务忽略，关联文献年份不符合请求，弃用于新研究结论。
4. 最终改为在 `document` 资源明确过滤年份、验证每个返回年份，再强校验 assay 的文献和目标关系，取得上述有效窗口。未根据任何实验值调整查询。

## 历史来源交叉

缓存历史方法与书目只投影来源名称、引用 ID/DOI，没有打开结果段、实验表或新标签。Karim 的来源包含 **BindingDB、ChEMBL、DeepHIT/Ryu**，数据库不同不是实验来源不同。

| 历史已曝光来源 | 官方/书目标识 |
|---|---|
| Wang/TDC 原任务 | DOI `10.1021/acs.molpharmaceut.6b00471`；PMID 27379394 |
| Karim | DOI `10.1186/s13321-021-00541-z`；PMID 34399849；PMC8365955 |
| DeepHIT/Ryu | DOI `10.1093/bioinformatics/btaa075` |
| Cai / Doddareddy / Didziapetris | DOI `10.1021/acs.jcim.8b00769` / `10.1002/cmdc.201000024` / `10.1007/s10822-016-9986-0` |
| Siramshetty / Konda | DOI `10.1021/acs.jcim.8b00150` / `10.1016/j.comtox.2019.100089` / `10.1021/acs.jcim.0c00884` |

PubChem 588834 的早期文献有被上述汇编使用的机会；1671200 还有 Tox21 来源风险。未在有限 ChEMBL 投影里发现 AID 字符串并不证明无交叉；本轮两个新文献也可能复用旧结构或旧实验。发表较晚不等于新湿实验。现有历史别名、DOI、具体 assay、供样批次与队列交叉仍不完整。

一轮过宽的本地来源搜索曾命中已曝光历史结果文本，未引用或用于当前选择；随后限制为指定来源文档。没有由此读取新的候选标签，也不把历史已曝光池重新当作盲测。

## 许可与身份映射

ChEMBL37 的固定 LICENSE 已有原字节证据，CC BY-SA 3.0，SHA `f0c22ca33207ad8a85129a71f1454fcf8f2f71c99e68da5da01538210fa3ffba`。本轮复用该同版本证据，不把它当作两个具体原实验的本地研究/再分发审批，也不覆盖原文和补充材料的版权。

PubChem/NCBI 官方政策提示第三方版权；PubChem 专门版权页返回浏览器挑战。两者的官方公开服务均不等于自动放行所有用途。每个候选的本地科研、商业、数据库再分发及外传范围需要分别确认。

SID→CID→结构和 ChEMBL molecule→原始实验实体都是待审核路径。本轮没有执行结构映射，不以 PubChem 标准化 compound 自动替代盐/混合物或实际测试样品，不按名称/CAS 猜测。

## 可复现证据与命令

公开聚合 [sources.json](artifacts/independent-validation/stage7b-source-discovery-20261017/sources.json) 记录实际 URL、UTC、响应/安全投影 SHA、计划 SHA、文献/assay 来源身份及明确的未知状态，不含 SID 清单、分子结构、活性结果或实际私有路径。完整安全投影留在 `%LOCALAPPDATA%` 私有科研目录；结果原体和不受审自由文本不留存。

| 有效计划/投影 | SHA256 |
|---|---|
| 新严格 SID 计划 | `6b9a5cf083d31556f0f748c0e5eda986d9420dad5c2b29732d02894eee57c3b3` |
| ChEMBL source-only 查询计划 | `0aa164f3ea73b934e8c885a058a31be378265578fba74bb5a94d5317c1c217c1` |
| ChEMBL source-only 获取回执 | `aa167bc9738e4193dfb8c416310523a820c95e99f429182fc3425f48e9601226` |
| ChEMBL 方法词计划 / 输出 | `023d66a9ed72a3aaefac27d2f7bdcfd1df8d8f14eb8381cc3eb232ec40da8d90` / `cd9369817b50b282764cab5e1e91d4a909c020a6fa5f0d0c78f248f1d797dbdc` |

Windows 11 PowerShell 在仓库根目录，输出目录必须新建、在 checkout 和 OneDrive 之外：

```powershell
$py = (Resolve-Path '.venv-v3\Scripts\python.exe').Path
$sourceRoot = Join-Path $env:LOCALAPPDATA 'AIDD-Blue-ToxNet\scientific-validation'
# 只请求两个已经验证可取得完整身份列表的 AID；不重试超限的1511。
& $py scripts/survey_pubchem_identities.py `
  --private-dir (Join-Path $sourceRoot 'pubchem-identities-recheck-new') `
  --aid 588834 --aid 1671200
# live响应变化时须产生新回执，不能覆盖原文件或声称相同快照。
```

该脚本只允许固定三 AID 的 `sid_type=all`；拒绝额外字段、重复 JSON key、非有限常数、错误 AID、重复/非法 SID、大小/长度异常和覆盖已有目录。未知/错误响应不打印、不缓存正文，也不会以空集合冒充成功；输出仍保持 `STAGE7C_BLOCKED`。提交时间和完整原始总体不能由这个脚本证明。

ChEMBL 的完整已执行 URL、`only` 字段及参数保存在公开来源回执中，复核时只请求这些资源和投影；对未知字段、忽略的过滤或版本变化应停止。原始自由方法叙述不得直接输出，需要独立的结果隔离审核。没有执行任何论文/数据下载脚本或付费调用。

本轮实际测试与保护结果写入 [verification.json](artifacts/independent-validation/stage7b-source-discovery-20261017/verification.json)，不是科学性能验证。新 SID 测试 12 项通过；相关回归执行范围、依赖及历史保护以该最终回执为准，不把之前的完整回归数字算作本轮。

### 本轮实际验证

```powershell
.\.venv-v3\Scripts\python.exe -m unittest test.test_pubchem_identities test.test_epa_no_api_fallback test.test_epa_structure_audit test.test_stage7_audit test.test_stage7b_supplement_audit -v
.\.venv-v3\Scripts\python.exe -m pip check
.\.venv-v3\Scripts\python.exe scripts/verify_compare_preservation.py
.\.venv-v3\Scripts\python.exe -m py_compile scripts/survey_pubchem_identities.py test/test_pubchem_identities.py
```

已执行相关 Python **69 项，全通过**（含新身份测试 12 项）；`pip check` 无损坏依赖，编译通过。历史保护脚本确认 **50 个科研文件及 4 个原 Compare 回执不变**；本轮开始时额外固定的 **122 个已有模型/工件/Markdown 文件逐字节不变**。这两组文件可能交叠，不能相加为独立文件总数。GitHub 只读复核 PR #6 仍为 Draft、未合并，base 为原 main。

新测试使用合成身份/服务响应，验证固定官方路径、完整字段白名单、顺序、错误 AID、重复键、非有限值、额外结果字段拒绝、大小/长度异常、错误响应不落盘以及不覆盖已有文件。两个真实 SID 请求另有来源回执，不能把 Mock 测试称为真实实验验证。本轮未重跑完整 Python、JavaScript、浏览器或 GPU 回归；没有模型代码/UI 改动，也未执行任何科学性能评价。

## 下一步与门禁

优先把两个 2025 文献来源交给**真正独立的数据管理员**，先确认新原始实验、完整无标签成员/结构及用途权利，再补齐原任务与候选实验的标签合同。开发者、子代理或同一环境中的另一个进程不能冒充独立保管人。本轮受限投影只是防泄漏工程手段，不是科学隔离的独立证明。

所有来源仍缺少全部资格证据、预定样本规模支持和权限冻结，不能建议 Stage 7C。main、正式模型、历史结果不改；所有 v3 保持 `formalAvailability=false`，PR #6 继续 Draft、未合并。本轮停止于来源调查和审核。
