# 分子毒性模型研究验证

本项目支持外部 API 与本机自主模型两条路径。蓝研 ToxNet v2 已使用真实 TDC 标签从随机权重完成训练，版本 `aidd-toxnet-v2-5c22f6cc5dd3`、参数量 2,374,390；其模型工件记录固定骨架划分、训练历史、validation 拟合的概率调整和 test 实测性能。另已完成 hERG 外部来源回顾性评价，来源人群独立性及前瞻验证仍未建立。完整数值、校验值与设备记录见 [MODEL_CARD.md](MODEL_CARD.md)。外部概率误差较大，当前模型仍是研究原型，不能由这些记录认证所有新分子的筛选能力。

## 数据和划分

标签 CSV 的最小列为 `smiles,endpoint,label`。端点为 herg/dili/ames/clintox，标签须来自明确的实验或数据集版本，使用整数 0/1，缺失标签不得补猜。另行保存数据来源、版本、许可、实验定义、日期和测量条件。

当前数据已通过 `scripts/prepare_toxicity_data.py` 下载四个固定版本的真实数据文件，并生成 `data/processed/toxicity.csv` 与来源清单。实际有效记录为 9,814 条，hERG / DILI / Ames / ClinTox 分别为 645 / 475 / 7,255 / 1,439。冲突、重复与无效结构的处理及许可文本不一致见 [DATA_SOURCES.md](DATA_SOURCES.md)。这是结构—端点标签数，跨端点可以共享分子。

```powershell
.\.venv\Scripts\python.exe scripts\validate_research.py split data\processed\toxicity.csv artifacts\split-recheck.json --dataset-version aidd-toxicity-75e5f2c8766efe24 --seed 42
```

工具使用严格 RDKit 解析、规范结构去重、冲突标签拒绝以及 Bemis-Murcko 骨架分组。相同结构跨端点和同骨架分子均留在同一集合；所有无环分子保守地归为一个组，避免按单分子拆散无环系列。

自主模型的固定 seed=42 划分保存在 `models/aidd-toxnet/split.json`，实际 train / validation / test 标签记录数为 7,851 / 982 / 981，不同分子数为 7,535 / 956 / 937。所有集合的四个端点均有正负两类。训练时合并同一分子的可用标签，用 mask 屏蔽未测端点；不会把“未测量”当作阴性。优化仅使用 train，模型检查点只由 validation 宏平均 AUROC 选择。正式运行实际 10 轮，选择第 3 轮权重，未依据 test 重训或挑选轮数。

80/10/10 是目标比例，不保证实际比例。大骨架簇或无环数据可能造成空验证集/空测试集，输出包含实际样本数、类别数、警示及 `completeThreeWaySplit`。不可划分的数据需要补充数据或重新制定有依据的验证策略，不能报告为有效三集合评估。

## 留出性能

预测 CSV 增加 `score,split` 两列；最终评估要求所有行声明 `split=test`。

```powershell
.\.venv\Scripts\python.exe scripts\validate_research.py evaluate data\predictions.csv artifacts\evaluation.json --dataset-version actual-data-version --model-version actual-model-revision --score-kind uncalibrated_score --split-manifest artifacts\split.json
```

工具分端点计算 AUROC 和 average precision，正确处理并列分数；单类别和无数据明确输出不可计算状态。`uncalibrated_score` 允许有限的 logits/margins 做排序评价，不计算概率校准指标；这是离线评价工具的范围，工作台 API 分数仍限定 0..1。

`--score-kind probability` 时额外计算 Brier score 和十等宽分箱 ECE。输出仅表示指标已测量，不能据此直接认证校准。`binary_label` 仅允许 0/1，排序指标的信息量有限。

有划分清单时，核对 test 成员、标签和跨集合结构/骨架分离；没有清单时输出 `heldOutStatus=declared-not-verified`。即使清单核对成功，也不能证明供应商模型从未见过这些数据，`trainingHistoryVerified=false`。通用商业模型的训练历史通常无法独立核验，应单独披露这一限制。

本机自主模型不同于供应商黑箱：训练脚本从随机初始化开始，记录训练代码、数据、划分和权重校验值，并在冻结检查点及概率调整后第一次评价 test。因此本机训练输出额外记录自身训练历史证据；这不是对第三方模型训练历史的证明。固定 Morgan 指纹逻辑回归基线仅使用 train，未在 test 上搜索参数。指标表同时报告基线，以核对神经网络的额外价值。

测试 AUROC 的区间来自每个端点 500 次分子级 bootstrap 的百分位数估计；不校正模型选择、骨架组相关性或数据偏差。ClinTox 测试只有 16 个阳性，应同时看 average precision、阳性比例与不确定性，不能仅依靠 AUROC 得出稳定结论。

实际 v2 测试结果如下，概率误差为 validation 调整后的值。

| 端点 | test 总数 / 阳性 | Transformer AUROC | Transformer AP | Brier | ECE（10 箱） | 指纹逻辑回归 AUROC |
|---|---:|---:|---:|---:|---:|---:|
| hERG | 113 / 78 | 0.735897 | 0.873353 | 0.200405 | 0.121915 | 0.788278 |
| DILI | 65 / 42 | 0.763975 | 0.868845 | 0.197407 | 0.140622 | 0.838509 |
| Ames | 566 / 285 | 0.775851 | 0.731558 | 0.195348 | 0.060184 | 0.786851 |
| ClinTox | 237 / 16 | 0.742647 | 0.164085 | 0.065938 | 0.036678 | 0.870192 |

四端点简单基线的 AUROC 和 AP 均高于 Transformer，尚未证明神经网络在此数据规模上有额外优势。这是首个可复现的自主权重版本，不宜描述为性能领先；没有执行差异显著性检验。由于 test 已经公开用于评价，新数据扩展和后续架构选择应采用新研究版本及另行冻结的独立验收集，原 test 只能作为历史回归参考。

## 外部来源回顾性评价

`scripts/evaluate_external_herg.py` 已在固定 v2 上评价 hERG_Karim 候选审计池，实际 9,978 分子、4,886 阳性 / 5,092 阴性。读取前核对候选清单、池文件、模型清单及原 split 哈希；重新计算每个分子的规范结构和审计母体，再对原四端点全部 train / validation / test 核验精确结构、中和去盐母体、完整结构及母体的 Bemis–Murcko 骨架分离，拒绝重复或冲突。

| 模型 | AUROC | AP | Brier | ECE（10 箱） |
|---|---:|---:|---:|---:|
| 固定 v2 Transformer | 0.643476 | 0.625562 | 0.334936 | 0.311605 |
| 原 train 指纹逻辑回归基线 | 0.647018 | 0.642957 | 0.346826 | 0.314246 |

评价不修改神经网络权重、不重拟合校准、不选阈值。固定 C=1 基线仅在原 train 拟合，前三个分子的批量预测与部署客户端误差小于 `1e-6`。脚本任务耗时 42.9 秒，涵盖本轮处理与评价，不是性能吞吐量基准。结果和逐分子分数分别保存在 `artifacts/external-herg-evaluation.json` 与 `artifacts/external-herg-evaluation-predictions.json`；默认拒绝覆盖已有输出，复查须指定新路径：

```powershell
.\.venv\Scripts\python.exe scripts/evaluate_external_herg.py --output artifacts/external-herg-recheck.json
```

hERG_Karim 正类使用 `<10 μM`，原 hERG 集合未建立统一匹配阈值，实验来源、标签定义和阳性率都可能变化。当前评估类型为 **来源与标签变化条件下的回顾性外部来源评价**；`sourcePopulationIndependenceVerified=false`、`prospective=false`。结构和骨架无交集降低已知泄漏，不能证明文献、实验来源或人群独立。不能用内部和外部 AUROC 的差异单独断言泛化下降的因果；本轮没有外部骨架簇区间或模型差异显著性检验。

外部 ECE 0.311605、Brier 0.334936 提示现有概率调整在这个来源上误差较大，暂不支持可靠筛选结论。若后续使用该池评价来调整模型或校准，它必须转为开发集，另找未用于选择的验收数据。保持原清单和内部评价不变，把新评估关联至同一权重版本，避免改写历史证据。

后台 `GET /api/local-model/info` 可返回 `externalEvaluation` 附录：仅加载已完成且模型版本、权重及模型清单 SHA256 与当前已加载模型匹配的记录，同时要求权重/校准/阈值未改变和数值合法；不匹配的记录不展示。真实浏览器已确认“模型详情”显示 9,978 分子的模型/基线指标及标签、来源独立性和研究原型限制，截图为 `artifacts/aidd-toxnet-external-evaluation.jpg`。附录独立保存、自身 SHA256 可追溯，内部工件历史不被覆盖。

## 校准与阈值

自主模型使用 validation 拟合每个端点的正温度与偏置：`sigmoid(logit / temperature + bias)`，以验证集对数损失和固定弱正则进行优化。检查点和调整参数在 test 评价前冻结。模型清单记录每个端点的实际拟合状态，并在 test 测量调整前后的 Brier 与十等宽分箱 ECE。状态为 `measured-not-certified`，不是“已认证校准”；少量正类及分布差异仍可能产生较大校准误差。

本轮四个端点均成功拟合，调整后的 Brier 和 ECE 均低于调整前，完整对照见模型说明；DILI ECE 从 0.253109 降为 0.140622，仍有明显误差。单端点调整是单调变换，因此 AUROC 和 AP 不变。一次内部集上误差下降不足以证明新实验条件或新人群中的概率可靠性。

没有合格验证数据、拟合失败或外部供应商没有证据时，继续明确未知或未拟合状态。不要在测试集调参后继续称其独立留出结果。当前未建立任务专用决策阈值或临床效用认证；概率输出也不是人体在特定剂量下发生毒性的风险。

专用预测服务可返回带证据编号和数据版本的校准声明，工作台将其标为服务声明。通用对话模型自己生成的“已校准”文字不作为证据。

## 适用域

`backend.validation.assess_applicability()` 可对真正的训练参考集合计算 Morgan radius-2 / 2048-bit 指纹的最大 Tanimoto 相似度。无参考集合时为 unknown；有参考集合时也只返回 similarity-heuristic，不宣称适用域已验证。阈值默认 0.5 是工具参数，必须用独立验证数据确定，不能作为通用科学阈值。

不能用六个演示化合物冒充训练参考域。真实服务应记录参考数据版本、指纹设置、阈值来源，以及域外样本的性能变化；通用模型训练参考集未知时继续显示未知。

自主模型只使用真实 train 集的规范结构作相似度参考，数据版本与权重一起记录。服务输出最大 Tanimoto、参考数量及固定 0.5 阈值，明确 `validated=false`。该阈值未通过独立域内/域外性能研究确定，不是经过验证的适用域边界，也不构成预测置信度。

## 本机训练复现

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements-model.txt
.\.venv\Scripts\python.exe scripts/prepare_toxicity_data.py --offline
.\.venv\Scripts\python.exe scripts/train_toxnet.py --seed 42 --device cpu --threads 6 --epochs 30 --patience 7 --batch-size 48 --output models/aidd-toxnet-new
```

没有原始文件时去掉 `--offline`。新输出目录保留现有模型；未完成的模型不会作为可用服务加载。记录包括来源清单、骨架划分、训练过程、测试预测、温度/偏置、比较基线和完整模型清单。跨 PyTorch、RDKit、设备版本可能存在数值差异，比较时应同时记录这些版本。

本轮最终完整软件验收通过 101 项 Python 测试与 41 项 JavaScript 测试，`pip check` 和前端脚本语法检查通过，五组等价 SMILES 的正式权重 HTTP 集成重新通过。本次约 0.03–0.08 秒的暖 HTTP 单次检查仅说明功能路径完成，未测 p95、冷启动或并发吞吐。软件验收与模型效能证据分别解读。

## 下一阶段的必要材料

新增数据库筛选记录见 [DATASET_SELECTION.md](DATASET_SELECTION.md)，优先按照当前端点含义、真实结构与标签、实验条件、版本、许可和来源重叠判断是否可用。候选数据不直接扩大原训练集：TDC 中相近任务可能来自同一文献或更大合集，MoleculeNet/DeepChem 也可能只是当前数据的分发镜像；必须核对规范结构、标签和来源后再确定训练扩充或外部评估用途。

`scripts/prepare_candidate_data.py` 对官方固定 hERG_Karim、Tox21 文件执行可追溯清洗，输出到 `data/candidates/processed/`，并对当前四端点模型的全部 train / validation / test 成员检查 canonical SMILES、去盐中和母体及两层骨架交集。母体标识仅用于保守泄漏排查，不替代原输入的化学身份；没有进行互变异构体合并。hERG 候选池额外排除母体冲突及重复，状态仍为回溯外部来源、来源人群独立性未验证。Tox21 十二通路标签保持独立，仅导出实测标签，未测标签继续 mask，当前四端点权重不使用这些候选。

该候选管线在结束前核对冻结模型清单和 split 哈希未变化，并记录原始文件、规范 CSV 与审计文件哈希。结构与骨架无交集只能减少已知泄漏，不能证明汇总数据的实验来源独立；若后续按新评估结果调参或校准，这批评估数据必须转为开发用途，不能继续声称未见外测。

自主模型下一步需要新增来源明确的真实数据、确认实验来源独立性的外部与前瞻测试、按实验条件分层的评价、校准稳定性检验、阈值效用分析及独立适用域验证。已完成的 hERG 外部来源回顾性评价不替代这些材料。外部 API 另需真实账户、模型修订和实际推理验收。若后续使用 TxGemma 或其他预训练模型，应另立研究版本，保留其原始训练历史、许可及潜在数据重叠说明。
