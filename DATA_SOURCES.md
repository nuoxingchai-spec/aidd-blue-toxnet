# 自训练毒性模型的数据来源

核验日期：2026-10-11。训练标签来自真实公开数据文件，未使用页面演示分数、SMARTS 规则分数或通用模型生成的标签。

四个数据集由 [Therapeutics Data Commons（TDC）毒性任务页面](https://tdcommons.ai/single_pred_tasks/tox/) 定义，通过其官方代码指定的 Harvard Dataverse 文件编号下载。文件编号来自固定版本的 [TDC metadata.py](https://raw.githubusercontent.com/mims-harvard/TDC/e70285e5ace3bb1d72eb3c57bad2e73d750f5d42/tdc/metadata.py)，不是自行搜索后拼接的未知数据镜像。

| 端点 | 标签 1 的含义 | 文档记载数量 | 实际下载行数 | 原始阳性 / 阴性 | 规范与清洗后 | 有效阳性 / 阴性 |
|---|---|---:|---:|---:|---:|---:|
| hERG | hERG 通道阻断 | 648 | 655 | 451 / 204 | 645 | 444 / 201 |
| DILI | 药物性肝损伤标签阳性 | 475 | 475 | 236 / 239 | 475 | 236 / 239 |
| Ames | 细菌回复突变试验致突变阳性 | 7,255 | 7,278 | 3,974 / 3,304 | 7,255 | 3,951 / 3,304 |
| ClinTox | 因毒性导致临床试验失败，即 CT_TOX | 1,484 | 1,478 | 112 / 1,366 | 1,439 | 93 / 1,346 |

实际数据量与网页摘要存在差异，因此使用实际文件与清洗统计记录模型版本。总计保留 **9,814 条结构—端点标签记录**，不是 9,814 个互不相同的分子；同一分子可以具有多个实测端点。

## 来源与原始文献

- [hERG 数据文件 4259588](https://dataverse.harvard.edu/api/access/datafile/4259588?format=original)。TDC 引用 Wang, Shuangquan, et al. “ADMET evaluation in drug discovery. 16. Predicting hERG blockers by combining multiple pharmacophores and machine learning approaches.” *Molecular Pharmaceutics* 13.8 (2016): 2855–2866。沿用来源标签，未把不同实验重设为统一的浓度阈值。
- [DILI 数据文件 4259585](https://dataverse.harvard.edu/api/access/datafile/4259585?format=original)。TDC 说明数据由美国 FDA National Center for Toxicological Research 信息汇集，并引用 Xu, Youjun, et al. “Deep learning for drug-induced liver injury.” *Journal of Chemical Information and Modeling* 55.10 (2015): 2085–2093。
- [Ames 数据文件 4259564](https://dataverse.harvard.edu/api/access/datafile/4259564?format=original)。TDC 说明汇集四篇论文，并引用 Xu, Congying, et al. “In silico prediction of chemical Ames mutagenicity.” *Journal of Chemical Information and Modeling* 52.11 (2012): 2840–2847。
- [ClinTox 数据文件 4259572](https://dataverse.harvard.edu/api/access/datafile/4259572?format=original)。TDC 引用 Gayvert, Kaitlyn M., Neel S. Madhukar, and Olivier Elemento. “A data-driven approach to predicting successes and failures of clinical trials.” *Cell Chemical Biology* 23.10 (2016): 1294–1301。

ClinTox 的 `Y` 与 [MoleculeNet / DeepChem 发布的原始 ClinTox 文件](https://deepchemdata.s3-us-west-1.amazonaws.com/datasets/clintox.csv.gz) 中 **CT_TOX** 列按规范结构核对，未把 FDA_APPROVED 列当成临床毒性。1,478 行均能匹配原始结构，1,440 行标签唯一且一致，38 行属于原始结构标签冲突组；冲突组已全部隔离。额外的原始文件只用于标签核验，不补造新的训练标签。

## 清洗、审计与版本

所有结构使用与工作台相同的严格 RDKit 校验和 canonical isomeric SMILES。保留盐组分、形式电荷、同位素和立体化学，移除原子映射号；不合并互变异构体，也不猜测缺失标签。

- hERG：移除 4 行相同标签的等价结构重复；3 个结构的 6 行冲突标签全部隔离。
- Ames：移除 23 行相同标签的等价结构重复。
- ClinTox：19 个结构的 38 行冲突标签全部隔离；另有 1 行含未定义原子 `*`，不支持可靠分子分析，因此隔离。
- DILI：475 行全部保留。

输出 `data/processed/toxicity.csv` 的 SHA256 为 `75e5f2c8766efe24cf6cbca57ce1e72bc9028705303b29fff8ad6016c6b0bd6b`，数据版本为 `aidd-toxicity-75e5f2c8766efe24`。原始文件、清洗 CSV、隔离记录、重复记录及元信息都保存在本地数据目录中。原始数据不随源代码提交。

| 原始文件 | SHA256 |
|---|---|
| herg.csv | f2b00c05f12763c9da91d46246800e30925a626d5038e15eec366d633bdffe54 |
| dili.csv | f977be424b9e509dc721e24106dd51cfb5a63ad8df0104ae51d9ab51cef7be09 |
| ames.csv | a17d2d6b9aa17f1cfaa1e3e4137788b2ef2700404042e285102f18543ca954b5 |
| clintox.csv | 32f8542b5ed007ca76506b7dbdaaed32c9bbe864ea1ce52d4d49f176d5c63455 |
| clintox_moleculenet.csv.gz（核验用） | 7324a84a180ad9068d3847677c85d58e8432e82b3ae6ac95cf0e98b3a58d9697 |

本次下载后本地文件写入时间（UTC；依据首次下载产生的文件时间记录）：

| 文件 | 时间 |
|---|---|
| herg.csv | 2026-10-11T14:43:48.713254+00:00 |
| dili.csv | 2026-10-11T14:43:48.686755+00:00 |
| ames.csv | 2026-10-11T14:43:48.876643+00:00 |
| clintox.csv | 2026-10-11T14:43:48.938323+00:00 |
| clintox_moleculenet.csv.gz | 2026-10-11T14:44:59.027598+00:00 |

复现下载和预处理：

```powershell
.\.venv\Scripts\python.exe scripts/prepare_toxicity_data.py
# 已有原始文件时，离线重新校验和清洗：
.\.venv\Scripts\python.exe scripts/prepare_toxicity_data.py --offline
```

脚本拒绝原始文件的校验值变化，避免在不改变版本的情况下训练另一份数据。`manifest.json` 记录来源链接、正类定义、原始/有效行数、阳阴性统计、RDKit 版本和审计文件的校验值。

## 固定骨架划分核查

对全部 9,814 条记录应用现有 `scaffold_split`，固定 seed=42，先在所有端点之间统一按结构与 Bemis–Murcko 骨架分组；无环结构保守地属于同一个组。相同结构或骨架不会因端点不同而落入不同数据集合。没有搜索种子或依据测试表现选择划分。

实际集合数为训练 7,851、验证 982、测试 981。每个集合的每个端点均包含正负两类：

| 端点 | 训练阳性 / 阴性 | 验证阳性 / 阴性 | 测试阳性 / 阴性 |
|---|---:|---:|---:|
| hERG | 274 / 142 | 92 / 24 | 78 / 35 |
| DILI | 149 / 191 | 45 / 25 | 42 / 23 |
| Ames | 3,358 / 2,762 | 308 / 261 | 285 / 281 |
| ClinTox | 54 / 921 | 23 / 204 | 16 / 221 |

统一骨架划分不是每个端点分别保证 80/10/10 的分层划分。ClinTox 测试集合只有 16 个阳性；训练集合阳性率约 5.5%，验证约 10.1%，测试约 6.8%。后续性能和概率校准应明确这些限制，不能仅凭一次小样本测试宣称广泛有效。

## 使用范围与许可

这些标签混合了实验与临床结局证据；模型学习的是特定来源标签的分布。ClinTox 阴性不代表所有条件下都无毒，DILI 等标签也不能脱离暴露、剂量和研究人群解释。小规模端点、类不平衡及结构覆盖不足会限制模型的适用范围。

核验时，TDC 四个数据集页面的许可文本均为 **“Not Specified. CC BY 4.0.”**，同时给出 [CC BY 4.0 链接](https://creativecommons.org/licenses/by/4.0/)。这段文字存在不一致，原始文献与数据的特定许可及商业再分发权利尚未独立解决。TDC 软件的 MIT 许可也不等于其所有底层数据均为 MIT。项目保留来源与作者归属，未声称拥有全部原始数据的商业再分发许可。
