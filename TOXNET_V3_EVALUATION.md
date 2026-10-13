# 蓝研 ToxNet v3 评测协议与实验记录

本文件区分开发模型的实际训练记录、用于选择模型的 validation 结果，以及尚未建立的独立效能证据。**v3 本轮不具备新盲测集，不因使用预训练表征而宣称泛化能力提升。**正式 v2 模型、原 test 评价和已曝光的 hERG_Karim 回顾性评价均保持历史版本。

## 共同数据与评价边界

真实数据协议由 `backend/v3_data.py` 准备，训练仅允许来自冻结 v2 **原 train** 的四端点实验标签。canonical 结构、盐/中和母体、立体与同位素近等价对象、互变异构母体及完整/母体骨架的连通组不能被拆到两个集合。与原 validation、原 test、已曝光 hERG_Karim 的禁止身份交集隔离；这些集合的标签不参与拟合、选择或校准。

MoLFormer 的词表、无 `<unk>` 和最多 202 tokens 的支持检查在共同划分前完成，筛选不读取标签。全部 B0/B1/B2 使用相同结构、样本顺序、mask、数据版本和 train/validation 清单；输入保持完整 canonical isomeric SMILES，不用去盐母体替代原始化学输入。Tox21 十二通路和 FDA 数据只进行角色及重叠审计，不提供四端点训练标签。

本机已准备的数据版本为 `aidd-toxnet-v3-development-c1d448002df99aa3`，任务合同 `aidd.toxnet.four-real-label-tasks.v3.1`，划分 SHA256 为：

```text
7701f6c3d8b69a7b1ed918e1365d39e79726414ce2898dca157f7a42134cbc91
```

以下计数读取自 `data/v3-development/manifest.json`，属于数据审计记录，不是模型性能。train 共 4,243 个分子，validation 共 1,045 个分子；同一分子可能只有部分端点有实验标签。

| 端点 | train 阳性 / 阴性 | validation 阳性 / 阴性 |
|---|---:|---:|
| hERG | 146 / 82 | 67 / 30 |
| DILI | 78 / 93 | 26 / 29 |
| Ames | 1,967 / 1,531 | 475 / 297 |
| ClinTox | 19 / 485 | 12 / 164 |

这些标签分别保留 hERG 阻断、DILI 肝损伤、Ames 致突变和 ClinTox `CT_TOX` 正类定义。ClinTox 不是 `FDA_APPROVED`。未测端点保持缺失 mask，不能充作阴性。TDC 软件许可不代表底层数据的商业再分发许可；训练许可记录为 `research-only-source-rights-unresolved`，原始数据不提交到仓库。

## 固定对照及指标

| 编号 | 输入 | 可训练部分 | 预处理 |
|---|---|---|---|
| B0 | Morgan radius-2 / 2048-bit，`includeChirality=True` | 四个独立 LogisticRegression，C=1 | 原始二进制指纹 |
| B1 | 冻结 MoLFormer 768 维 embedding | 四个独立 LogisticRegression，C=1 | 仅在 train 拟合 StandardScaler |
| B2 | 同一冻结 MoLFormer embedding | 128 隐藏维的小型 MLP，四个输出 | 仅在 train 拟合 StandardScaler |
| F1（可选） | 冻结 embedding + Morgan；可关闭单个分支 | 同类小型 MLP | 仅在 train 拟合 StandardScaler |

固定种子为 42、43、44。B0/B1 的 `lbfgs` 是确定性求解器，种子重复可能得到相同结果；这不是独立数据抽样或误差区间。B2 仅在观测 mask=1 的位置计算 BCE，训练类权重仅依据 train 标签，使用 validation 宏平均 AUROC、再以宏平均 AP 解同分来选择检查点。不搜索旧 test、hERG_Karim 或 validation 的概率校准参数。完整 Transformer 微调当前受控拒绝，不能跳过冻结基线。

每个端点记录观测总数、阳性/阴性数、AUROC、AP、Brier 和 10 等宽箱 ECE。没有观测标签时所有指标为 `null`；只有单一类别时 AUROC/AP 为 `null` 并说明 `both-classes-required`，Brier/ECE 仍可在已有标签上描述性计算。没有任何可定义的 validation 排序指标，或某个训练端点缺乏两类时，完整四端点实验拒绝完成，不生成伪预测。

所有 v3 输出均为**未拟合校准的模型分数**。测量 Brier/ECE 不等于完成校准；低 Brier/ECE 也不能单独证明少数阳性的识别能力。此 validation 用于检查点及方案选择，因此其指标不能作为无偏最终性能、前瞻表现或人体特定剂量风险。这里没有阈值效用研究、配对显著性检验、骨架簇 bootstrap、概率校准认证或适用域认证。

MoLFormer 固定来源 `ibm-research/MoLFormer-XL-both-10pct`、revision `7b12d946c181a37f6012b9dc3b002275de070314`。实际冻结编码器参数为 44,375,040；编码器预训练不证明其掌握可靠毒性机制。标准 Morgan 指纹不编码同位素质量，两个同位素身份不同的分子可能得到相同 B0 指纹；预训练来源的立体信息限制见 [TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md)。

## 实际运行结果

本轮真实 B0/B1/B2 的 42、43、44 三个种子共 **9 次训练与 validation 评价已完成**。下面逐端点指标直接读取独立目录 [experiment.json](artifacts/v3/baselines-20261013/experiment.json) 的 `aggregate`，对应各次真实运行的 `evaluation.json`，保留六位小数。所有值是同一开发 validation 上的三种子均值，尚无新盲测、外部或前瞻效能结论。单元测试中的合成标签、特征和夹具训练不进入此表。

| 模型 | 端点 | validation 总数 / 阳性 | AUROC 均值 | AP 均值 | Brier 均值 | ECE（10 箱）均值 |
|---|---|---:|---:|---:|---:|---:|
| B0 | hERG | 97 / 67 | 0.748756 | 0.841513 | 0.198686 | 0.202320 |
| B0 | DILI | 55 / 26 | 0.842175 | 0.862051 | 0.186422 | 0.160103 |
| B0 | Ames | 772 / 475 | 0.764905 | 0.826583 | 0.206911 | 0.125184 |
| B0 | ClinTox | 176 / 12 | 0.713415 | 0.132158 | 0.066361 | 0.059724 |
| B1 | hERG | 97 / 67 | 0.785075 | 0.887739 | 0.255525 | 0.273803 |
| B1 | DILI | 55 / 26 | 0.754642 | 0.775224 | 0.317043 | 0.321863 |
| B1 | Ames | 772 / 475 | 0.703824 | 0.790410 | 0.258695 | 0.203763 |
| B1 | ClinTox | 176 / 12 | 0.721545 | 0.300465 | 0.059581 | 0.059071 |
| B2 | hERG | 97 / 67 | 0.793201 | 0.889179 | 0.174629 | 0.109328 |
| B2 | DILI | 55 / 26 | 0.809019 | 0.794441 | 0.224186 | 0.228316 |
| B2 | Ames | 772 / 475 | 0.780625 | 0.837838 | 0.192616 | 0.092802 |
| B2 | ClinTox | 176 / 12 | 0.733740 | 0.279477 | 0.060221 | 0.047542 |

B0 和 B1 的三次确定性拟合预测相同，标准差在六位小数精度下均为零；汇总浮点运算可能留下约 `1e-16` 的数值误差。B2 的三种子样本标准差如下（`ddof=1`）；它只描述同一划分上初始化和训练的变化，不是泛化置信区间。

| 端点 | B2 AUROC 标准差 | B2 AP 标准差 | B2 Brier 标准差 | B2 ECE 标准差 |
|---|---:|---:|---:|---:|
| hERG | 0.006181 | 0.009529 | 0.015007 | 0.032208 |
| DILI | 0.025580 | 0.035495 | 0.024867 | 0.031950 |
| Ames | 0.002105 | 0.008820 | 0.006785 | 0.013910 |
| ClinTox | 0.025187 | 0.051070 | 0.003077 | 0.020392 |

在这个开发 validation 上，B2 的 hERG、Ames、ClinTox 排序均值高于 B0，但 DILI 的 AUROC/AP 仍以 B0 较高；B1 在 DILI、Ames 的排序和概率误差都没有优于 B0，且 hERG 的排序提高同时伴随较差 Brier/ECE。**预训练表征没有在本轮全面胜过 Morgan 基线。**ClinTox 只有 12 个 validation 阳性，B2 AP 的种子变化明显；没有配对显著性检验或独立验收，不应据此挑出个别优势指标声称领先。

实际运行使用 Windows 本机 CPU、6 线程，Python 3.13.2、RDKit 2026.09.1、PyTorch 包版本 2.14.1（运行时 `2.14.1+cpu`）、NumPy 2.5.4、scikit-learn 1.9.1、Transformers 4.57.6、Tokenizers 0.22.2、Safetensors 0.6.2、Hugging Face Hub 0.36.2。实查 `torch.version.cuda=None`、`torch.cuda.is_available()=False`，未使用 CUDA。B0 可训练参数为 8,196，B1 为 3,076；B2 为 98,948，另有 44,375,040 个冻结编码器参数，不能将冻结参数计作从毒性标签训练的参数。

| B2 种子 | 实际执行轮数 | validation 选择轮次 | 实际权重 SHA256 |
|---|---:|---:|---|
| 42 | 11 | 3 | `52331e925f9f3a883b585ae71b1f03ce9b3fe374adf00867c5dab8797ff0c1b9` |
| 43 | 10 | 2 | `95c2598d62a58fb21ba158c1bad94246467cfb4c2f8c7fffb0bdc5fe8d6efffa` |
| 44 | 14 | 6 | `73266d0b00befa0b7a02f3afd8983f58c4dcadaad766b9f7f8f1b250464a2602` |

MLP 最大 40 轮、patience=8，提前停止不是未完成训练；每次保存并恢复真实最佳 validation 检查点。LR 的非空训练历史保存实际迭代数、训练标签计数与固定求解参数。数据清单 SHA256 为 `1fffa35d1da6e4f58e269a0d74d6dc3473d98251ae419b8c8c762a7bbe4b4bf6`；整体实验记录 SHA256 为 `e99df03b28b9815cb2091e79c986aed10adc390b7222bf3684f76be60d826625`。编码器 safetensors SHA256 为 `0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。

完成工件应同时包含实际模型权重、输入变换、非空训练历史、validation 预测、真实训练参考结构、SHA256 和 `manifest.json`；最后才写入 `status=trained-completed`。总体过程发生错误时为 `incomplete`。训练完成后的模型仍为 `formalAvailability=false`、`evaluationStatus=evaluated-validation-only`，只能显式选择研究加载，不能自动替换正式 v2 供应商。

本次运行记录的 `featureExtractionSeconds=70.514` 秒是编码器加载及 embedding 提取阶段，`durationSeconds=3.936` 秒是随后九次实验头训练、评价和写工件阶段，均不包含依赖安装、下载与前置数据协议准备。缓存命中和首次提取的耗时不能混为一次完整模型推理基准；本轮不据此报告 API p95、GPU 峰值显存或并发吞吐量。

本轮尚未执行项目及原因：

| 项目 | 当前状态 | 具体原因 |
|---|---|---|
| F1 冻结融合及分支消融训练 | 实现与特征维度测试可用，真实性能待评估 | 本轮优先完成同划分的 B0/B1/B2；F1 应另建输出目录、保留基线和消融记录 |
| RTX 4060 / CUDA 运行与显存峰值 | 未运行 | 当前隔离环境安装 CPU PyTorch；需明确安装兼容 CUDA 构建并实测驱动和 8GB 峰值 |
| 完整预训练编码器微调 | 未实现可执行训练，CLI 明确拒绝 | 尚无独立微调协议、可审计优化与 8GB 资源控制；当前编码器保持全冻结 |
| 独立概率校准 | 未拟合 | 当前 validation 已用于模型选择，没有独立合法校准集合 |
| 新盲测、前瞻实验及适用域认证 | 未建立 | 旧 test 和 hERG_Karim 已曝光；需要另取来源明确、未用于选择的新数据并冻结验收协议 |

软件验证最终实际通过全项目 **182 项 Python 测试、41 项 JavaScript 测试**，包括 20 项实验模块和 17 项版本化加载器回归；原运行环境与隔离 v3 环境的 `pip check` 均通过。九个真实 head 的全部观察 validation 预测已复算指标、重新加载权重并比对离线推理；正式 v2 的五组等价 SMILES HTTP 也以新的核验文件重新通过。CPU 夹具测试不下载大型模型、不要求 GPU，实际大型快照加载和九次真实训练另有上述工件证明。软件验证不会提高模型的独立效能证据等级。

## Windows PowerShell 复核

安装、固定修订下载、国内网络失败和离线快照导入流程见 [TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md)。下列命令从仓库根目录运行，复用经过验证的本地权重；CPU 可运行全部冻结实验，CUDA 必须先核验当前环境的 PyTorch 构建及驱动。

```powershell
$toxnetV3Python = '.\.venv-v3\Scripts\python.exe'
& $toxnetV3Python -c "import torch; print(torch.__version__); print('CUDA available:', torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU-only environment')"
& $toxnetV3Python -m pip check
& $toxnetV3Python scripts\extract_embeddings.py --data-dir data\v3-development --snapshot models\pretrained\molformer --output data\v3-development\molformer-embeddings.npz --device cpu --batch-size 8
& $toxnetV3Python scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache data\v3-development\molformer-embeddings.npz --models B0 B1 B2 --seeds 42 43 44 --device cpu --threads 6 --epochs 40 --patience 8 --batch-size 64 --encoder-batch-size 8 --output artifacts\v3\baselines-reproduction-01
```

每次复现必须指定不存在的新输出目录，拒绝覆盖以保留完整历史。缓存校验包括文件 SHA256、完整输入顺序、化学版本、数据清单哈希、模型修订、权重 SHA256、维度和精度；更换数据、模型修订或化学处理后必须建立新缓存。没有合法缓存或真实权重时明确报错，不产生替代 embedding。

核验 SHA256 和每次运行的实际指标：

```powershell
Get-FileHash artifacts\v3\baselines-reproduction-01\B0-seed-42\weights.npz -Algorithm SHA256
Get-FileHash artifacts\v3\baselines-reproduction-01\B2-seed-42\weights.pt -Algorithm SHA256
Get-Content artifacts\v3\baselines-reproduction-01\experiment.json -Encoding UTF8
Get-Content artifacts\v3\baselines-reproduction-01\B2-seed-42\evaluation.json -Encoding UTF8
```

## 历史证据与下一阶段

v2 正式版本仍为 `aidd-toxnet-v2-5c22f6cc5dd3`、2,374,390 参数；四端点原 Morgan 基线 AUROC/AP 全面超过 v2 的事实见 [MODEL_CARD.md](MODEL_CARD.md)。本轮不把新的开发 validation 指标与原 test 指标横向宣称为提升，也不调整或重算历史评价。9,978 个 hERG_Karim 分子的 v2 AUROC 0.643476 属于 **retrospective / previously exposed comparison**，不能重新标记为全新盲测。

下一阶段需要先冻结预处理、架构、种子、校准协议和验收条件，再取得未用于选择的新来源数据或前瞻实验。MoLFormer 的公开预训练集合可能与下游分子结构重叠；本轮不能证明全部预训练来源无重叠，也没有把“结构排除已核验”等同于实验来源、人群或文献独立。

Tox21 扩展应另建十二通路任务合同和独立数据版本，从 `backend/v3_data.py` 的候选角色/身份审计衔接到独立的样本标签字典与 mask。当前 `FrozenFeatureMLP`、四端点标签/指标函数和版本化加载器要求四个输出，不能直接伪装成十二任务；`masked_binary_loss` 可按匹配张量 shape 复用。新增十二输出架构和 artifact schema/endpoint contract，保留四端点推理协议。训练时仍只有实测 mask=1 的通路参与损失，并在新的共同结构划分下重新建立基线、validation 选择与未曝验收集。
