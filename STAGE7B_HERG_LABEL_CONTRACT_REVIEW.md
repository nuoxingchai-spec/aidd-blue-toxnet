# Stage 7B 补充：hERG 科学标签合同核验

核验日期：2026-10-16（Asia/Shanghai）。结论：**`MANUAL_REVIEW_REQUIRED`**。可以恢复原模型预测的来源标签语义，但尚不能恢复原实验标签的统一浓度阈值、比较符与边界规则，因此不能为新候选执行标签映射，也不能进入独立盲测。本文不修改原四端点合同，不读取新候选的活性值、关系符、质量结果或标签。

## 1. 实际代码中的标签链

| 层级 | 可验证证据 | 可确认的含义与限制 |
|---|---|---|
| 原始来源声明 | `backend/training_data.py:DATASETS` | hERG 使用 Harvard Dataverse 文件 **4259588**；固定原文件 SHA256 为 `f2b00c05f12763c9da91d46246800e30925a626d5038e15eec366d633bdffe54`。不是 `hERG_Karim` 或 `hERG_Central` |
| 原始列到训练行 | `read_tdc_rows` | 将 CSV 的 `Drug`、`Drug_ID`、`Y` 映射到结构、来源 ID、`label`。未从 IC50、Ki、抑制率等连续值计算新标签 |
| 正负含义 | `DATASETS` 与 v2 `manifest.json` 的 hERG 来源声明 | **1 = hERG blocker；0 = non-blocker**。负类仅为来源任务阴性，不证明任何剂量、物种或人体条件下安全 |
| 标签合法性及重复 | `normalize_rows` | 接受已有二元标签的 0/1 及明确字符串写法；拒绝缺失/非二元值；同一 canonical 结构—端点有冲突时全部隔离；同标签重复沿用既有历史处理。没有统一原实验阈值 |
| 模型监督方向 | `scripts/train_toxnet.py` | 标签写入对应输出位置，使用有观测 mask 的 BCEWithLogits；没有反转 blocker/non-blocker。本文只阅读代码，不执行历史训练或评价 |
| v2 输出 | `backend/local_model.py` | `sigmoid(logit / temperature + bias)` 是来源标签预测；温度/偏移是历史 validation 校准，并非实验浓度或新候选标签规则 |
| 四任务 v3 | `backend/v3_data.py` | `aidd.toxnet.four-real-label-tasks.v3.1` 从同一 `DATASETS` 构造 positiveClass、sourceSha256、reference；验证加载时严格比较合同 |
| A4/B16/C_Morgan 多任务 | `backend/multitask_data.py:task_contracts`、`backend/multitask_model.py` | `aidd.toxnet.four-toxicity-twelve-assay-tasks.v1` 继续保留四端点合同，Tox21 的 assay active 是另外十二个任务；不能替代 hERG |

原来源声明明确写的是：`assay thresholds follow the source dataset, not a new uniform threshold`。这证明项目没有新增统一阈值，**不证明原始论文没有阈值**。原论文方法证据未恢复时，应保留“未知”，不能把代码中未见阈值写成原科学定义的否定结论。

`local_model.py` 的适用域 `threshold=0.5` 是 Morgan/Tanimoto 相似度研究启发值；任何概率分界值也是输出决策设置，均不能当作 IC50 浓度阈值。

## 2. 本轮实际官方证据核查

仅获取任务说明与书目元数据；未获取候选结果表、活性补充表或模型预测。

| 来源 | 获取时间 UTC / HTTP | 字节数 / SHA256 | 实际支持的结论 |
|---|---|---|---|
| [TDC 官方毒性任务页](https://tdcommons.ai/single_pred_tasks/tox/#herg-blockers) | 2026-10-16T12:22:42.326571+00:00 / 200 | 39,560 / `b60096de20decee11f1bbaf9b5ce81dd2a6a7d5cdc3a8c0743181b2f1ca75112` | hERG blockers 页面写 1/0 分类和 Wang 2016 引用，648 为**文档统计**，不是本轮候选计数；未提供该原任务的 IC50 阈值、比较符、边界或逐 assay 条件 |
| [Crossref Wang DOI 元数据](https://api.crossref.org/works/10.1021/acs.molpharmaceut.6b00471) | 2026-10-16T12:22:43.151228+00:00 / 200 | 8,266 / `ac8c551a21641309e36d1e823747adb7b86d8ade5fe4a46f2ed585b559eda026` | DOI、标题、ACS 出版者与发表日期可追溯；响应没有可确认该数据用途的 license 字段；不提供阈值方法证据 |
| [ACS 官方 abstract 地址](https://pubs.acs.org/doi/abs/10.1021/acs.molpharmaceut.6b00471) | 2026-10-16T12:22:43.937284+00:00 / **403** | 响应 5,558 / `159eb194d37899f21034f3a2e229e8839d21fb064cc6e5e35819b97a187290a0` | 访问失败；未绕过访问控制、未下载全文/补充材料。403 页面不保存为方法证据，不据此推断科学定义 |
| [Europe PMC exact-DOI core 元数据](https://www.ebi.ac.uk/europepmc/webservices/rest/search?query=DOI:10.1021/acs.molpharmaceut.6b00471&format=json&resultType=core&pageSize=2) | 2026-10-16T12:26:08.068615+00:00 / 200 | 6,251 / `7e42dcab388f160d18661a544df0f50a75e5223555d4f779cd0935008f3113ae` | PMID 27379394，`inPMC=N`、`isOpenAccess=N`，未给 PMCID；唯一 fullTextUrl 标记 Subscription required。公开摘要没有阈值/比较符/单位或 assay 体系 |
| [PubMed DOI 搜索](https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi?db=pubmed&term=10.1021%2Facs.molpharmaceut.6b00471%5BAID%5D&retmode=json) | 2026-10-16T12:26:09.221005+00:00 / 200 | 219 / `94bd0f099bbfeca33250965f0b66172a2497dab9a000e8958bf0bd24c1056112` | 精确 DOI 找到 PMID 27379394；这是文献身份，不是候选结构计数 |
| [PubMed 官方摘要 XML](https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi?db=pubmed&id=27379394&retmode=xml) | 2026-10-16T12:26:28.342986+00:00 / 200 | 7,380 / `ccb6760db265a7655c15e57450ae05268b619f6815662a7a17fc217a27ad6ae1` | 确认作者及分类建模摘要；未提供恢复浓度标签规则所需的方法段。仅历史原始任务摘要，无新候选结果 |

实际书目：Wang, Shuangquan, et al. *ADMET Evaluation in Drug Discovery. 16. Predicting hERG Blockers by Combining Multiple Pharmacophores and Machine Learning Approaches*. Molecular Pharmaceutics 13(8), 2855–2866 (2016). DOI：[10.1021/acs.molpharmaceut.6b00471](https://doi.org/10.1021/acs.molpharmaceut.6b00471)。Crossref 元数据的 published date 为 2016-07-18；在线发表日与卷期年份分别保留，未推断原实验时间。

因此本结论并非仅由 ACS 403 得出：TDC 原任务描述、Crossref、Europe PMC 与 PubMed 可访问内容均已核查。它们未提供精确方法，不等于所有合法馆藏/作者存档均不存在。可由用户/独立科学审核者提供合法方法摘录，再依据原始证据审核；本轮不绕过许可、不擅自购买。

TDC 同页的 **hERG Karim** 明确描述 `<10 μM` 为 blocker、`>=10 μM` 为 non-blocker；**hERG Central** 另有单浓度抑制及 hERG_inhib 定义。它们均是另外的数据合同，不能跨标题复制到原 hERG。本轮没有读取这些历史数据的标签行。

TDC 页面同时显示 `Dataset License: Not Specified` 与 `CC BY 4.0` 链接。不能用 TDC 软件 MIT 或页面通用许可自动替代数据集特定权利核查。原实验方法文本摘录、底层数据和候选数据库的使用与再分发权限需要分别确认。

## 3. 待核验字段与禁止的默认值

| 字段 | 当前值 | 所需原始证据 |
|---|---|---|
| 正类/负类方向 | 已确认：blocker 1 / non-blocker 0 | 来源声明与官方任务页 |
| 原任务是否有统一 IC50 阈值 | `null` / `MANUAL_REVIEW_REQUIRED` | 原论文方法、来源数据字典或作者确认；不能由 Karim 反推 |
| 阈值数值及单位 | `null` / `MANUAL_REVIEW_REQUIRED` | 原始任务的带单位规则，不是新候选分布 |
| 比较符 `<`、`<=`、`>`、`>=` | `null` / `MANUAL_REVIEW_REQUIRED` | 明确规则文本及适用类别 |
| 恰好等于阈值的边界归属 | `null` / `MANUAL_REVIEW_REQUIRED` | 精确阈值边界说明；缺失时不补猜 |
| 浓度单位换算后判定 | 不可执行 | nM→μM 的量纲换算不能证明 assay 等价；先确认原任务、测量和精度规则 |
| assay 构成及读出方式 | unresolved | 电流阻断/配体结合/报告系统、物种、细胞系/表达体系、温度、电压、孵育与重复实验来源 |
| 历史实验—论文—队列链 | incomplete | Wang 汇编成员来源与候选 DOI/assay/实体映射，而非仅给汇编论文的 DOI |
| 新候选标签映射、正负样本数 | **not-run** | 原任务规则获审、独立保管人隔离、先冻结映射政策；本轮不执行 |

## 4. 各实验测量的科学适用性

| 测量 | 与原标签直接可比？ | 原因及准入条件 |
|---|---|---|
| 已证实的人类 hERG 电流功能性 IC50 | 尚未批准，优先审查 | 需要原任务阈值和 assay 构成证据，并审查体系/条件差异；目标名称相同不足够 |
| 放射配体竞争/结合 IC50 | 否，不能自动替代 | 结合位点/配体置换不等同于电流功能抑制；不能借浓度单位相同合并 |
| Ki / Kd | 否，不能自动替代 | 亲和力与功能 IC50 是不同测量；缺乏底物/配体浓度及模型假设时不能转换 |
| EC50 / AC50 | 否，不能自动替代 | 半效应或模型化活性浓度需明确响应/曲线/读出定义；不是原 hERG 二分类标签的同义项 |
| EPA hitcall | 否，不能自动替代 | 依赖检测阈值、曲线拟合、质控和读出方案；不是统一的 IC50 阻断阈值 |
| 单一浓度 inhibition %、percent_activity | 否，不能自动替代 | 单点响应不能确定完整 IC50；需要测试浓度和读出定义，不补造未观察的半效应值 |
| 无物种、无实验体系或无原始来源 | 不合格 | 缺失保持阻塞，不由 target=KCNH2、assay=B/F 或 DOI 存在推断补齐 |

EPA 的 `NVS_IC_hKhERGCh` / aeid 686 与 `ERF_TW_IC_hKCNH2` / 3184 是放射配体结合；`TOX21_hERG_U2OS_Antagonist` / 3210 是需进一步解决注释冲突的报告/通道读出，且涉及历史 Tox21 来源家族。端点身份延续初步调查，补充结构统计与新来源证据以 `STAGE7B_EPA_STRUCTURE_AUDIT.md` 为准。没有将这些端点映射到原 hERG 标签。

## 5. 预先冻结的是审查限制，不是虚假的已批准标签协议

新增 `backend/herg_label_contract_review.py` 提供：

- `prereview_policy()`：返回独立副本，明确阈值/比较符/单位/边界为 null，`labelMappingExecutable=false`。
- `prereview_policy_sha256()`：确定性内容摘要 **`720daf2db990068fc43f2ac70ff45a71a095ed40803a722e9bcc2030ce67ffe7`**。扩展采集前绑定此限制；绑定回执以扩展调查脚本为准。
- `original_contract_review()`：只哈希八份历史源码/清单证据，不打开任何 CSV 或候选数据。当前证据摘要为 **`1323d2e9a3717151cc6daf18abfe9ecba88cdf09fa305e52348e15542f9bb3c2`**；返回原声明及未知状态。
- `require_custodian_mapping()`：无授权覆盖参数，始终抛出 `PermissionError`。没有连续活性到标签的实现；不能靠调用者传 `approved=true` 放行。

该预审查政策不封存候选集、不生成独立评价许可，也不假称已有审核人。M、mM、μM、nM 的浓度量纲因子仅作方法说明，**不读取数值、不执行换算/分类**。原科学证据补齐后，需要新的真实审核和版本化政策；不能静默改变本政策或用自我批准替代独立保管。

必须在保管人接触结果前冻结的处理原则：精确值不在阈值未知时分类；不等式不当作等号；范围不取中点；缺失不当阴性；质量异常隔离；重复/冲突实验不按结果挑选或平均。具体关系符、范围、缺失值、质量标志与冲突数量需要读取结果相关字段，**全部留给独立保管人，当前为 not-run**，模型开发流程不能输出这些分布或据此筛选。

## 6. 人工决策清单：仍待批准

| 决策 ID | 必需证据与负责方 | 当前门禁 |
|---|---|---|
| HERG-01 | 科学审核者提供可合法阅读的原 Wang/TDC 方法或数据字典，确认阈值、单位、比较符和边界；可只提供不含新候选结果的方法摘录 | `MANUAL_REVIEW_REQUIRED` |
| HERG-02 | 原实验来源保管人补齐 assay、物种/体系/条件和历史汇编成员的实验来源链 | `MANUAL_REVIEW_REQUIRED` |
| HERG-03 | 数据使用负责人确认具体候选来源的本地研究许可；论文文本、数据库、再分发、商业和云端分别处理 | `MANUAL_REVIEW_REQUIRED` |
| HERG-04 | 独立保管人管理结果字段/标签，提供身份、访问控制、签署时间和不含标签的合规/隔离证明；当前开发者环境不充当独立保管人 | `MANUAL_REVIEW_REQUIRED` |
| HERG-05 | 在不看结果的前提下，由科学审核者批准新版本的精确/删失/范围/缺失/质量异常/重复规则并记录证据 SHA | `MANUAL_REVIEW_REQUIRED` |
| HERG-06 | 在结构与来源、任务、权利均通过后单独冻结评价人群、模型绑定、成员数要求和区间规则，并另行批准 Stage 7C | `STAGE7C_BLOCKED` |

既有协议规定每类至少 10 与至少 10 骨架簇是 bootstrap 区间支持条件，**不是本轮新候选样本量/统计效能已经合格的证明**。没有候选标签隔离证明或预先批准的评价要求时，不能仅凭结构数量提出 ready。

## 7. 实际验证与未执行内容

实际命令（仓库根目录 PowerShell）：

```powershell
& .\.venv-v3\Scripts\python.exe -m unittest test.test_herg_label_contract_review -v
& .\.venv-v3\Scripts\python.exe -c "import json;from backend.herg_label_contract_review import original_contract_review;print(json.dumps(original_contract_review(),ensure_ascii=False,indent=2))"
```

本模块 **7 项软件测试实际通过**：未知阈值/映射关闭、政策副本不可污染、不同 TDC 任务不替换、无可覆盖的映射权限、仅跟踪源码/清单证据、缺失证据失败、证据字节变更改变摘要。使用临时合成源码片段测试摘要机制，不使用真实候选标签；这些测试不构成科学标签验证或全项目完整验收。

未执行：原论文全文/受限补充材料获取、候选关系符/数值/异常结果统计、候选标签映射、正负分布检查、模型训练/推理/准确率评价、校准拟合、封存及 Stage 7C。官方小型元数据获取记录保存在本机私有证据目录，原始响应不提交 GitHub。

**最终标签合同门禁：`STAGE7C_BLOCKED`。** Blocker/non-blocker 方向已核验，但精确原实验判定合同、任务等价性、来源独立、具体使用权与独立标签保管尚未齐备。
