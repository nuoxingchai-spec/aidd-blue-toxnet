# 蓝研 ToxNet v3：四毒性端点与十二通路任务

本轮扩展沿用已核验的冻结 MoLFormer 编码器，建立独立的 16 任务研究训练流程。正式网站仍使用原四端点 v2。Tox21 的阳性表示相应体外实验通路活性，不是 DILI、hERG、Ames、ClinTox 的阳性，也不是临床毒性概率。模型规模和任务数量本身不能证明泛化改善。

## 实际改动文件与流程

| 文件 | 职责 |
|---|---|
| `backend/multitask_data.py`、`scripts/prepare_multitask_data.py` | 真实来源重建、十六任务合同、共享结构隔离及版本化划分 |
| `backend/multitask_experiments.py`、`scripts/train_multitask.py` | A4/B16/Morgan/零通路监督消融、masked 优化、三种子对照与负迁移指标 |
| `backend/multitask_model.py`、`backend/research_routes.py`、`backend/main.py` | 严格工件加载和独立研究路由；原正式 API 路径保持 |
| `scripts/predict_multitask.py`、`scripts/verify_multitask_run.py` | 显式离线研究推理、逐观察成员及实际评价/加载推理核验 |
| `test/test_multitask_data.py`、`test/test_multitask_experiments.py`、`test/test_multitask_service.py` | 61 项无需大模型/GPU 的新增软件回归 |
| `README.md`、`MODEL_API_CONTRACT.md`、本文 | 入口、准确 HTTP 合同、真实实验与 Windows 复现 |
| `.gitignore`、`.gitattributes` | 禁止发布数据/缓存/新权重，只允许汇总证据并保留哈希文件的原始字节 |
| `artifacts/v3-multitask/` 下的公开 JSON | 实验汇总、数据审计摘要、最终核验、真实研究 HTTP 核验和 v2 保留证明 |

数据流程为：核验过的旧 TRAIN 四标签 + 固定 Tox21 十二列 → 标签盲编码器兼容检查 → 跨任务结构/母体/骨架连通组禁入与共享 split → 冻结 768 维表征或 2048-bit 指纹 → train-only 预处理及拟合 → 原四任务受支持 validation 选择 → 独立、默认不正式可用的研究工件。没有向原 `toxnet.py` 或 `local_model.py` 引入十六任务行为。

## 数据与任务合同

`backend/multitask_data.py` 复用版本化的 canonical、去立体/同位素等价、母体、互变异构母体和完整/母体骨架审计标识。真实模型输入保持完整 canonical isomeric SMILES，审计标准化不替换输入。所有任务的结构在同一个 connected component 中分组；无环结构保守地共用一个不可拆组。

四端点只从核验过的原冻结 TRAIN 读取实验标签，ClinTox 保持 CT_TOX；Tox21 从固定 SHA256 原始快照重新清洗，核对候选缓存。十二列分别是 NR-AR、NR-AR-LBD、NR-AhR、NR-Aromatase、NR-ER、NR-ER-LBD、NR-PPAR-gamma、SR-ARE、SR-ATAD5、SR-HSE、SR-MMP、SR-p53。缺失值保留独立 mask，不补零标签、不推断新标签。

旧冻结 validation/test 和此前曝光的 9,978 分子 hERG_Karim 池只提供禁入结构标识；交集所在的整个连通分组被隔离。旧 test 已曝光，只能作历史参考。上一轮 v3 development validation 也是已用于研究选择的开发数据，不能称首次盲测；本轮同 split 对照重新训练 A/B/C，不把旧验证成绩当作新对照。来源审计单独保留 TDC/MoleculeNet、Tox21/PubChem/EPA 与 Karim/ChEMBL/文献的上游交集限制：结构互斥不等于实验来源独立。

数据文件、split、隔离清单、禁入标识、source/hash、许可记录和合同使用新的版本。FDA 仍缺审核后的药物—结构映射，不并入训练；数据权利未解决的部分仅用于本地研究，不随仓库再分发。缺少任何必须的真实源文件时准备流程明确报错，不生成替代数据。

## 模型与公平对照

| 标识 | 特征及训练 | 任务 |
|---|---|---|
| A4 | 冻结 MoLFormer → train-only 标准化 → 共享 MLP → 独立输出头 | 四原端点 |
| B16 | 同冻结特征架构及预算，加入十二任务的 masked 监督 | 四原端点 + 十二通路 |
| C_Morgan | radius=2、2048-bit、含 chirality 的 Morgan → 每端点 LogisticRegression | 十六独立任务 |
| B16_noTox21（消融） | 十二通路损失权重设零，仅四端点参与优化和预处理拟合 | 十二头标记 disabled，不作为通路推理工件 |

A4/B16/C_Morgan 使用同一份新的结构分组划分；四端点评价成员完全相同。A4 仅使用有四端点观测标签的 train 行；B16 可使用 Tox21-only train 行。所有特征变换、阳性类别权重和参数拟合只在各模型允许的 train 行计算。

损失对每个有观测标签的任务计算 BCEWithLogitsLoss，再按任务权重归一化。batch 中没有标签的任务跳过；完全没有启用任务观测的 batch 跳过优化。训练数据缺少某端点双类时标记不能完整训练，不伪造有效输出。阳性类别权重不能被当作已校准概率。

本轮固定原四任务权重 1、各 Tox21 任务权重 0.25；类别权重采用仅 train 计算的 `sqrt(negative/positive)`，限制在 [0.5,4]。这些是事先确定的设置，不依据旧 test、Karim 或本轮结果调参。MLP 是 768→128 共享层加各任务独立线性头，A4 共 98,948 参数，B16 共 100,496 参数；消融的有效可训练参数仍为 98,948。冻结编码器 44,375,040 参数。Morgan 十六头共有 32,784 参数。

模型选择只使用原四端点中达到预注册最小阳性/阴性数要求的 validation 任务。低样本任务仍报告指标，但不驱动 checkpoint；Tox21 的十二个任务不主导选择目标。验证仍是研究模型选择数据，不能代替新的独立测试。固定种子 42、43、44 的所有结果均保留，并报告 B16−A4 的逐种子差值，避免只挑最好的种子。负迁移表是描述性开发验证比较，不是因果或独立泛化证明。

## Windows 11 PowerShell 复现

在仓库根目录执行。已有 `.venv-v3` 可复用；新 clone 按 [TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md) 创建完整独立环境、准备固定哈希真实数据与 MoLFormer。不要覆盖 `.venv`、原正式模型或此前训练目录。

新 clone 若按上一阶段文档把来源放在 `data/v3-sources`，应明确使用对应路径：

```powershell
$taskMultiPy = '.\.venv-v3\Scripts\python.exe'
& $taskMultiPy scripts\prepare_toxicity_data.py --data-dir data\v3-sources\four-endpoint
& $taskMultiPy scripts\prepare_candidate_data.py --candidate-dir data\v3-sources\candidates --metadata data\v3-sources\tdc-metadata.py
& $taskMultiPy scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
& $taskMultiPy scripts\prepare_multitask_data.py --data-dir data\v3-sources\four-endpoint --candidate-dir data\v3-sources\candidates --output data\v3-multitask-new
```

此新 clone 准备命令代替下面使用本机现有 `data/` 来源的 prepare 命令；训练/验证命令继续使用同一个 `data/v3-multitask-new`。若已生成目标目录，换新的 output，不能覆盖。离线已有原始源时，前两个下载命令加 `--offline`；原始下载失败时保留明确缺失报错，不能用替代标签。

```powershell
git switch codex/toxnet-v3-multitask
$taskMultiPy = '.\.venv-v3\Scripts\python.exe'
& $taskMultiPy -m pip check
& $taskMultiPy -c "import torch; print(torch.__version__); print('CUDA:',torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
nvidia-smi

# 完全离线；准备只读真实缓存，缺文件时明确失败
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $taskMultiPy scripts\prepare_multitask_data.py --output data\v3-multitask-new

& $taskMultiPy scripts\train_multitask.py --data-dir data\v3-multitask-new --encoder-dir models\pretrained\molformer --embedding-cache artifacts\v3-multitask\cache-new.npz --output artifacts\v3-multitask\baselines-new --models A4 B16 C_Morgan B16_noTox21 --seeds 42 43 44 --device cpu --threads 6 --encoder-batch-size 8 --epochs 40 --patience 8 --batch-size 64 --minimum-selection-per-class 10 --tox21-weight 0.25

& $taskMultiPy scripts\verify_multitask_run.py --data-dir data\v3-multitask-new --experiment artifacts\v3-multitask\baselines-new --snapshot models\pretrained\molformer --output artifacts\v3-multitask\verification-new.json
& $taskMultiPy scripts\predict_multitask.py --model artifacts\v3-multitask\baselines-new\B16-seed-42 --encoder models\pretrained\molformer --smiles 'CCO' --allow-research
& $taskMultiPy -m unittest discover -s test -p 'test_*.py'
npm test
```

本机本轮使用 `data/v3-multitask-audited` 和 `artifacts/v3-multitask/baselines-20261013`。默认准备/训练脚本指向 audited 目录；已存在目录一律拒绝覆盖。开始源码强化前的真实初版准备快照 `data/v3-multitask` 未用于训练。新 clone 的旧四任务 v3 开发结构清单未再分发，默认不存在时记录 `not-assessable` 和未知交集，而不是虚报零交集；先前开发已曝光的事实继续保留。提供历史目录时必须精确匹配既有版本/hash，不接受刚重新生成但声称历史原版的清单。

数据使用 ledger 同时保留 inherited `role`（原固定来源核验角色）及 `useInThisProtocol`。Tox21 本轮明确是多任务实验标签源，Karim 是结构禁入来源，FDA 是待映射审计来源。加载时从原冻结 val/test 和固定 Karim 结构重建禁入索引；即使有人清空禁入文件并重算自填哈希，也会被拒绝。

RTX 4060 使用经官方确认兼容本机 Python/驱动的 CUDA wheel，安装在独立环境；默认从 encoder batch=8 开始，OOM 时减至 4/2/1。CPU 与 CUDA 使用不同 cache/output 路径；本轮未执行的 GPU 验证不会被记录为通过。冻结编码器不全量微调，不运行浮动远程代码。网络下载失败和离线权重导入步骤沿用 [固定修订说明](TOXNET_V3_TRAINING.md#3-固定预训练权重与离线加载)。

## 独立研究服务

工件 schema 为 `aidd.toxnet.multitask.artifact.v1`，和四端点 v3/v2 工件区分。所有训练产物保持 `formalAvailability=false`，校准和适用域未验证。权重、split、输入数据版本、任务合同、编码器固定修订及 SHA256、真实参数量、完成状态与评价均核验后才允许研究加载。

`POST /api/research/tox21` 接收 `{"smiles":"CCO"}`；响应只包含十二个独立 `pathways`、标签语义和可追溯来源，不产生整体临床毒性分数。`GET /api/research/tox21/status` 查询可用性。默认无研究启用或完整工件时预测返回 503；非法或不支持输入返回 422。A4 或关闭通路监督的消融工件不能充当通路服务。现有 `/api/analyze` 与四端点正式语义保留。

完成自己本机复现后，可在新的 PowerShell 进程显式启用研究服务（此步骤不作为自动正式部署）：

```powershell
$taskMultiPy = '.\.venv-v3\Scripts\python.exe'
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts\v3-multitask\baselines-new\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cpu'
& $taskMultiPy -m uvicorn backend.main:app --host 127.0.0.1 --port 4183
# 另一 PowerShell 窗口调用：
Invoke-RestMethod 'http://127.0.0.1:4183/api/research/tox21/status'
Invoke-RestMethod 'http://127.0.0.1:4183/api/research/tox21' -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO"}'
```

配置任一 seed 的真实完成工件是研究推理选项，不表示已选出生产最优 seed；三种子实验均保留。准确字段、状态码及限制见 [MODEL_API_CONTRACT.md](MODEL_API_CONTRACT.md#独立-tox21-研究接口-v1)。

## 实际验证与结果

本轮实际完成 **9 个完整训练工件（A4/B16/C_Morgan × 3 种子）和 3 个零通路损失消融工件**。消融为 `trained-ablation`，十二通路头 disabled，不是完整通路模型。全部 CPU，未使用旧 test 或 Karim 标签优化、选择或校准。

数据版本 `aidd-toxnet-16task-development-aab05727cde8e47b`；manifest SHA256 `ff4837c1bcb0e568c506e503cab9321f23be328f5c432d495be3606f15d7ceae`；split SHA256 `df0dfa7195f714df81e93f3795d1623c81d1d5a32433a59388421cf7d2b082f8`。

train 4,804 / validation 1,462 分子，原四任务有标签分子分别 3,030 / 839；保留 33,168 个观察标签，其中原四任务 4,049、Tox21 29,119。隔离 7,077 个结构及 52,536 条标签，所有任务共享 1,978 个可用分组，最大分组 154 个分子。无环连通分组因与禁入参考关联被整体隔离，未为类别平衡拆组。

本机旧四任务 v3 开发集交集已逐 split 审计：旧 validation 657 个结构进入新 train、378 个进入新 validation；旧 train 对应 2,373 / 461。它们仍是已曝光开发数据；不能把这个新划分称首次盲测，也不能与旧四任务 v3 数字直接作同划分模型比较。

| 任务 | Train 阳性 / 阴性 | Validation 阳性 / 阴性 |
|---|---:|---:|
| herg | 123 / 72 | 89 / 22 |
| dili | 84 / 81 | 16 / 10 |
| ames | 1450 / 923 | 444 / 190 |
| clintox | 26 / 438 | 4 / 77 |
| tox21_nr_ar | 175 / 2062 | 21 / 564 |
| tox21_nr_ar_lbd | 134 / 1927 | 16 / 493 |
| tox21_nr_ahr | 326 / 1648 | 71 / 433 |
| tox21_nr_aromatase | 149 / 1541 | 41 / 363 |
| tox21_nr_er | 340 / 1498 | 54 / 386 |
| tox21_nr_er_lbd | 170 / 1928 | 22 / 513 |
| tox21_nr_ppar_gamma | 69 / 1848 | 28 / 425 |
| tox21_sr_are | 353 / 1331 | 98 / 294 |
| tox21_sr_atad5 | 113 / 2061 | 21 / 523 |
| tox21_sr_hse | 121 / 1777 | 30 / 446 |
| tox21_sr_mmp | 403 / 1335 | 93 / 336 |
| tox21_sr_p53 | 172 / 1856 | 50 / 461 |

以下为三种子 validation 均值；± 为种子间样本标准差，**不是置信区间**。Morgan 的确定性 LR 结果各 seed 相同，SD=0 不能表示没有数据不确定性。全部数据是用于选择检查点的开发 validation，概率未校准、适用域未验证。

### 原四毒性任务：同成员比较

| 任务 | 模型 | N / 阳性 | AUROC ± SD | AP ± SD | Brier ± SD | ECE10 ± SD |
|---|---|---:|---:|---:|---:|---:|
| herg | A4 | 111 / 89 | 0.772557 ± 0.018954 | 0.903013 ± 0.005866 | 0.192076 ± 0.012198 | 0.211069 ± 0.008631 |
| herg | B16 | 111 / 89 | 0.772387 ± 0.021844 | 0.910155 ± 0.007781 | 0.160718 ± 0.004767 | 0.163451 ± 0.030417 |
| herg | C_Morgan | 111 / 89 | 0.763534 ± 0.000000 | 0.895367 ± 0.000000 | 0.144179 ± 0.000000 | 0.149220 ± 0.000000 |
| dili | A4 | 26 / 16 | 0.931250 ± 0.010825 | 0.964621 ± 0.008512 | 0.113868 ± 0.018353 | 0.145130 ± 0.022531 |
| dili | B16 | 26 / 16 | 0.933333 ± 0.018042 | 0.964357 ± 0.010497 | 0.106349 ± 0.014123 | 0.162536 ± 0.028044 |
| dili | C_Morgan | 26 / 16 | 0.868750 ± 0.000000 | 0.928061 ± 0.000000 | 0.147111 ± 0.000000 | 0.077189 ± 0.000000 |
| ames | A4 | 634 / 444 | 0.787952 ± 0.010316 | 0.888348 ± 0.005777 | 0.169033 ± 0.004135 | 0.091251 ± 0.007352 |
| ames | B16 | 634 / 444 | 0.792994 ± 0.009014 | 0.889600 ± 0.008019 | 0.164424 ± 0.004773 | 0.073913 ± 0.018445 |
| ames | C_Morgan | 634 / 444 | 0.803497 ± 0.000000 | 0.898918 ± 0.000000 | 0.182049 ± 0.000000 | 0.120580 ± 0.000000 |
| clintox | A4 | 81 / 4 | 0.869048 ± 0.043600 | 0.462202 ± 0.071060 | 0.043458 ± 0.008137 | 0.053994 ± 0.015064 |
| clintox | B16 | 81 / 4 | 0.858225 ± 0.020874 | 0.351114 ± 0.074296 | 0.060422 ± 0.009636 | 0.069915 ± 0.011891 |
| clintox | C_Morgan | 81 / 4 | 0.844156 ± 0.000000 | 0.221983 ± 0.000000 | 0.046082 ± 0.000000 | 0.034848 ± 0.000000 |

### 十二 Tox21 独立通路

| 任务 | 模型 | N / 阳性 | AUROC ± SD | AP ± SD | Brier ± SD | ECE10 ± SD |
|---|---|---:|---:|---:|---:|---:|
| tox21_nr_ar | B16 | 585 / 21 | 0.637988 ± 0.026113 | 0.213306 ± 0.022910 | 0.053871 ± 0.000713 | 0.095614 ± 0.005481 |
| tox21_nr_ar | C_Morgan | 585 / 21 | 0.599882 ± 0.000000 | 0.229505 ± 0.000000 | 0.032611 ± 0.000000 | 0.024944 ± 0.000000 |
| tox21_nr_ar_lbd | B16 | 509 / 16 | 0.695656 ± 0.038890 | 0.209830 ± 0.043585 | 0.044061 ± 0.002102 | 0.053629 ± 0.011861 |
| tox21_nr_ar_lbd | C_Morgan | 509 / 16 | 0.653398 ± 0.000000 | 0.238160 ± 0.000000 | 0.030420 ± 0.000000 | 0.024684 ± 0.000000 |
| tox21_nr_ahr | B16 | 504 / 71 | 0.842761 ± 0.006567 | 0.529925 ± 0.029426 | 0.114041 ± 0.003260 | 0.124704 ± 0.011085 |
| tox21_nr_ahr | C_Morgan | 504 / 71 | 0.784569 ± 0.000000 | 0.419565 ± 0.000000 | 0.111520 ± 0.000000 | 0.076278 ± 0.000000 |
| tox21_nr_aromatase | B16 | 404 / 41 | 0.719344 ± 0.007758 | 0.244549 ± 0.021408 | 0.105346 ± 0.007803 | 0.090741 ± 0.029632 |
| tox21_nr_aromatase | C_Morgan | 404 / 41 | 0.701203 ± 0.000000 | 0.294311 ± 0.000000 | 0.085356 ± 0.000000 | 0.059467 ± 0.000000 |
| tox21_nr_er | B16 | 440 / 54 | 0.684226 ± 0.030588 | 0.283902 ± 0.016532 | 0.125307 ± 0.007285 | 0.113313 ± 0.011650 |
| tox21_nr_er | C_Morgan | 440 / 54 | 0.679260 ± 0.000000 | 0.282724 ± 0.000000 | 0.115152 ± 0.000000 | 0.085314 ± 0.000000 |
| tox21_nr_er_lbd | B16 | 535 / 22 | 0.705269 ± 0.011155 | 0.270705 ± 0.033924 | 0.058577 ± 0.003757 | 0.086024 ± 0.013421 |
| tox21_nr_er_lbd | C_Morgan | 535 / 22 | 0.715843 ± 0.000000 | 0.214833 ± 0.000000 | 0.039447 ± 0.000000 | 0.034311 ± 0.000000 |
| tox21_nr_ppar_gamma | B16 | 453 / 28 | 0.712381 ± 0.006293 | 0.271917 ± 0.032095 | 0.058530 ± 0.001595 | 0.047232 ± 0.002112 |
| tox21_nr_ppar_gamma | C_Morgan | 453 / 28 | 0.729580 ± 0.000000 | 0.225187 ± 0.000000 | 0.056119 ± 0.000000 | 0.043417 ± 0.000000 |
| tox21_sr_are | B16 | 392 / 98 | 0.698054 ± 0.019997 | 0.468326 ± 0.044182 | 0.170428 ± 0.006725 | 0.061746 ± 0.009857 |
| tox21_sr_are | C_Morgan | 392 / 98 | 0.656723 ± 0.000000 | 0.395607 ± 0.000000 | 0.199818 ± 0.000000 | 0.149929 ± 0.000000 |
| tox21_sr_atad5 | B16 | 544 / 21 | 0.776078 ± 0.011853 | 0.212372 ± 0.080463 | 0.054027 ± 0.002069 | 0.103698 ± 0.005474 |
| tox21_sr_atad5 | C_Morgan | 544 / 21 | 0.735956 ± 0.000000 | 0.145886 ± 0.000000 | 0.038060 ± 0.000000 | 0.028401 ± 0.000000 |
| tox21_sr_hse | B16 | 476 / 30 | 0.694121 ± 0.018742 | 0.130294 ± 0.020056 | 0.073879 ± 0.004693 | 0.086528 ± 0.021740 |
| tox21_sr_hse | C_Morgan | 476 / 30 | 0.775486 ± 0.000000 | 0.192418 ± 0.000000 | 0.060402 ± 0.000000 | 0.052480 ± 0.000000 |
| tox21_sr_mmp | B16 | 429 / 93 | 0.820490 ± 0.011646 | 0.593167 ± 0.042501 | 0.133025 ± 0.006126 | 0.060609 ± 0.009177 |
| tox21_sr_mmp | C_Morgan | 429 / 93 | 0.731759 ± 0.000000 | 0.482790 ± 0.000000 | 0.162863 ± 0.000000 | 0.119881 ± 0.000000 |
| tox21_sr_p53 | B16 | 511 / 50 | 0.753435 ± 0.038335 | 0.297917 ± 0.040020 | 0.094505 ± 0.004639 | 0.087659 ± 0.022211 |
| tox21_sr_p53 | C_Morgan | 511 / 50 | 0.710672 ± 0.000000 | 0.231934 ± 0.000000 | 0.091584 ± 0.000000 | 0.068944 ± 0.000000 |

### 负迁移与消融

| 原任务 | B16−A4 AUROC | B16−A4 AP | B16−A4 Brier | B16−A4 ECE10 |
|---|---:|---:|---:|---:|
| herg | -0.000170 | +0.007142 | -0.031358 | -0.047618 |
| dili | +0.002083 | -0.000264 | -0.007520 | +0.017406 |
| ames | +0.005042 | +0.001252 | -0.004609 | -0.017337 |
| clintox | -0.010823 | -0.111089 | +0.016965 | +0.015921 |

hERG 的 AUROC 略降、AP 和概率误差有所改善；DILI 的 AUROC 略升，但 AP 微降、ECE 上升；Ames 略有改善，但 Morgan AUROC 0.803497 仍高于 B16 的 0.792994。ClinTox 的 AUROC/AP 下降、Brier/ECE 上升，呈现描述性负迁移；validation 仅 4 个阳性，结论高度不稳定，不能宣称显著或已证实因果。

B16_noTox21 三种子的四任务 AUROC/AP、所选 epoch 与 A4 相同，完整数据上的共享层/前四头权重最大绝对差为 1.4901161193847656e-8，Brier/ECE 最大差分别为 7.028200077474622e-9 / 8.677730978234877e-9。应解释为 Float32 数值容差内一致，而不是完整数据的逐位相等；小样本单元夹具的逐位相等不替代真实运行记录。这支持 loss mask、训练成员、标准化和随机路径消融正确。B16 加入辅助任务同时改变可用训练行、train-only 标准化统计和每 epoch 的优化步数；本对照衡量整套多任务扩展，不把差值全部归因于通路损失。未根据此次 validation 指标再搜索任务权重或最佳种子。

| 模型 | Seed | 实际所选 epoch / 执行 epoch | 工件状态 |
|---|---:|---:|---|
| A4 | 42 | 3 / 11 | trained-completed |
| A4 | 43 | 2 / 10 | trained-completed |
| A4 | 44 | 6 / 14 | trained-completed |
| B16 | 42 | 2 / 10 | trained-completed |
| B16 | 43 | 5 / 13 | trained-completed |
| B16 | 44 | 4 / 12 | trained-completed |
| C_Morgan | 42 | LR，无 epoch / 0 | trained-completed |
| C_Morgan | 43 | LR，无 epoch / 0 | trained-completed |
| C_Morgan | 44 | LR，无 epoch / 0 | trained-completed |
| B16_noTox21 | 42 | 3 / 11 | trained-ablation |
| B16_noTox21 | 43 | 2 / 10 | trained-ablation |
| B16_noTox21 | 44 | 6 / 14 | trained-ablation |

冻结提取/指纹阶段实际 100.211 秒；模型训练与评价阶段 20.168 秒（不含来源/结构审计加载）。不是 RTX 4060 吞吐/显存测量。实际使用 Python 3.13.2、torch 2.14.1+cpu，CUDA 不可用。

完整均值、各 seed 指标、配对差值及可追溯 run manifest hashes 保存在 `artifacts/v3-multitask/baselines-20261013/experiment.json`，SHA256 `2363fef3d0cda147b8a230cbee036ba62355de7a5edcc5b8150d493218a0858d`。公开仓库只提交汇总与校验记录，不提交原始/派生分子标签、预测行、head 权重、embedding 或预训练缓存。

### 软件验证与剩余边界

实际 Python 单元回归 **243 项通过**（含无需 GPU/下载的 61 项多任务新增测试及 CPU 小样本夹具训练），JavaScript **41 项通过**；原 `.venv` 与 `.venv-v3` 的 `pip check` 通过，前端两个 JS 文件语法检查通过。单元夹具明确是软件测试，以上真实模型指标来自独立的真实实验标签训练，二者不混用。

原 v2 真实 HTTP 五组等价 SMILES 回归通过，新文件保存到被 Git 忽略的 `v2-http-verification.json`。正式 v2 的 10 个模型/历史评估保护文件 SHA256 均保持一致；旧四任务 v3 汇总/模型与数据没有被覆盖。正式模型仍为 `aidd-toxnet-v2-5c22f6cc5dd3`、2,374,390 参数。

实际 B16 CPU 权重的 FastAPI 研究 HTTP 验证已通过：十二通路、等价 SMILES、与直接加载预测相等、非法/额外字段 422、跨来源 403、默认关闭 503，且无临床综合分数。证据为 `artifacts/v3-multitask/research-http-verification.json`，这是测试进程中的真实 HTTP 合同验证，没有重启或部署到 4173。

最终独立核验为 `artifacts/v3-multitask/integration-verification-final.json`：全部 12 个真实 head 校验通过，逐观察成员和标签完整匹配，指标同时与实际 `evaluation.json` 及汇总双重比对，检查模型/种子身份和训练源码 SHA256。加载推理与记录预测最大绝对差 `1.4142701398878188e-7`，在预注册容差内。逐种子消融误差记录为 `artifacts/v3-multitask/ablation-verification.json`。

本轮未运行 CUDA / RTX 4060 8GB 峰值显存验证（当前 CPU wheel）；未做完整编码器微调（冻结协议）；未拟合独立校准器、适用域认证或新前瞻/盲测（缺独立数据与协议）；FDA 结构映射和实验来源总体独立性仍未完成。没有自动部署到 4173，没有替换正式模型，也没有合并 Draft PR。
