# ToxNet A4、B16 与 Morgan 公平比较

## 当前科学状态

独立科学验收未完成。本机 A4、B16 和 C_Morgan 有已训练研究工件；新独立数据的实验来源、权利与结构资格尚未全部满足，因此没有新的独立 AUROC、AP、Brier、ECE 或准确率排名。全部研究模型继续 `formalAvailability=false`。

`backend/validation_predictors.py` 复用 `MultitaskModelClient` 的真实 SHA256、架构、任务合同和 MoLFormer 合同检查，不复制或改变原模型。批推理使用已有 `mean/scale` 和 `predict_head`，不拟合新的预处理、模型参数或校准器。原单分子 `predict_all` 和正式 v2 接口保持原行为。

| 比较模型 | 真实特征与头 | 可直接比较的任务 |
|---|---|---|
| A4 | 冻结 MoLFormer、共享 MLP、四端点头 | hERG、DILI、Ames、ClinTox |
| B16 | 冻结 MoLFormer、共享 MLP、四端点及十二 Tox21 头 | 合同一致的原四端点；通路需单独评价 |
| C_Morgan | radius-2、2048-bit、chirality Morgan 与逻辑回归 | 对应已训练头及其原实验合同 |

TxGemma `BBB_Martins` 是类别任务，不能作为以上四毒性端点的直接科学准确率对照。Qwen、DeepSeek 报告质量和生成速度也不等于毒性预测效能。

## 冻结、执行与标签权限

`scripts/evaluate_independent_models.py` 只接受资格合格的封存目录和预先核验的清单字节 SHA。执行顺序为：

1. 结构侧加载验证权利、历史重叠、任务合同、封存 SHA 与当前评估源码。
2. 比较 `evaluationSettings` 与固定 v1 合同，拒绝 CLI 替换设定。
3. 用原加载器核验 A4、B16、C_Morgan 权重、清单、任务语义和版本。
4. 在私有封存目录写不可覆盖的 `evaluation-exposure.json`，再执行全部模型预测。
5. 所有输出固定后才重新核验封存并读取新获准标签。
6. 计算同成员评价和配对差值，仅写聚合结果。

v1 固定种子 42、500 次 scaffold-cluster bootstrap、每类至少 10 个观测与至少 10 个簇，指标顺序固定为 AUROC、AP、Brier、10-bin ECE。代码字节摘要绑定评估脚本、批预测器和指标模块；代码改变即拒绝原封存加载，不默默采用默认值。暴露登记同时绑定数据、模型、协议、统计设定、实际设备/批量和评估源码；中断也保留登记。相同登记重跑标记 `previously-evaluated-same-frozen-protocol`，不再声称新盲测。

本机登记是保守的研究约束，不是不可篡改证明，也不能证明模型预训练时从未接触类似结构。

## 覆盖率、缺失标签与置信区间

`backend/independent_validation_metrics.py` 验证完整结构 ID 顺序、模型 SHA 和任务合同。不能为了得到更好的指标取成功预测的交集：

- 缺失标签保持缺失，不补为阴性；分别报告结构数、观测数、缺失数、阳性数、阴性数和阳性率。
- 任一观测成员无分数、NaN、越界或对应头缺失时，端点指标为 null，状态 `coverage-incomplete`；保留原分母、成功计数与成员哈希。
- 配对结果给出共同可评估成员计数/顺序 SHA 和覆盖率。覆盖不全不发布缩减成员后的配对优劣差值。
- 空标签集为 `no-data`，单类别 AUROC/AP 为 null。新独立评价中任一类别少于预先固定的 10 个观测时，两项排名指标也为 null，状态 `insufficient-class-support`；已曝光开发集保留描述性点估计。Brier/ECE 可在有实际观测时计算，但小样本警告不能据此消除。
- 每次重采样抽取完整骨架簇，两个模型使用同一抽样。少于每类 10 个样本或 10 个簇时，区间为 null；有效重复不足 80% 时拒绝相应区间。
- 同一成员的多种子运行不被视为独立分子样本；percentile 区间只标记探索性。每个真实来源独立分层报告。

输出记录模型权重、清单、训练来源、编码器版本、数据/划分/合同哈希、实际依赖、设备和时间。科学结果不包含原始行、SMILES 或实验标签。统计区间和微小点估计差值都不构成临床级验证。

## 开发集负迁移：真实已曝光观察

此前已曝光开发划分上的三种子（42、43、44）结果可复算；下表来自既有真实预测工件，不能用于封存新测试的模型选择。

| 端点 | 同成员观测数 | 阳性数 | A4 平均 AP | B16 平均 AP | B16 − A4 |
|---|---:|---:|---:|---:|---:|
| hERG | 111 | 89 | 0.905918 | 0.909528 | +0.003610 |
| DILI | 26 | 16 | 0.961769 | 0.962231 | +0.000462 |
| Ames | 634 | 444 | 0.887265 | 0.888882 | +0.001617 |
| ClinTox | 81 | 4 | 0.471599 | 0.309444 | −0.162155 |

ClinTox 开发 train 只有 26 个阳性和 438 个阴性；开发 validation 的 4 个阳性使 AP 对排序特别敏感。原 checkpoint 选择已因支持不足排除 ClinTox。B16 辅助 Tox21 权重为 0.25，原四端点权重为 1.0；这是保存的真实训练合同，不是本轮新调参结果。

`backend/negative_transfer.py` 核验全部保存工件及相同标签成员，记录各种子波动、原训练类别数量/损失权重/选择合同和配对 bootstrap。ClinTox 不满足区间保护条件，区间保持 null。现有预测文件没有梯度记录，因此梯度冲突为 `not-measured`；输入梯度 cosine 工具不等于实际运行过训练。下降是需研究的开发观察，不能证明辅助任务导致下降或架构失败。

可选消融配置目前仅列出 A4、B16、C_Morgan、B16_noTox21 × 3 种子共 12 个模型/种子运行，MLP 每次最多 40 epochs。未运行训练，运行时间估计为 null；完整重训应先确认计算规模和授权，并仅用开发 train/validation。

## Windows PowerShell

当前已有隔离环境可用；不下载模型、不更换现有 Torch wheel：

```powershell
Set-Location 'C:\Projects\aidd-blue-toxnet' # 替换为自己的仓库目录
$validationPython = '.\.venv-v3\Scripts\python.exe'
& $validationPython -m pip check
& $validationPython -c "import torch; print({'torch': torch.__version__, 'cudaAvailable': torch.cuda.is_available()})"
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'

# 冻结前取得实际源码摘要；不会读实验标签或加载权重。
& $validationPython -c "from backend.dataset_overlap_audit import evaluator_source_sha256; print(evaluator_source_sha256())"

# 真实已曝光开发分析；每次用新输出，原结果不可覆盖。
& $validationPython scripts/analyze_negative_transfer.py `
  --experiment-dir artifacts/integration/multitask-cuda-20261013 `
  --output artifacts/independent-validation/new-development-negative-transfer.json

# 仅在真实新数据已审计、封存且标签权限批准之后执行。
$sealedDataset = 'C:\PrivateResearch\qualified-seal\seal.json'
$reviewedManifestSha = '替换为事先人工核验的64位seal字节SHA256'
& $validationPython scripts/evaluate_independent_models.py `
  --sealed-dataset $sealedDataset --manifest-sha256 $reviewedManifestSha `
  --models-root artifacts/integration/multitask-cuda-20261013 `
  --encoder-dir models/pretrained/molformer --device cpu `
  --output artifacts/independent-validation/new-independent-evaluation.json
```

占位字符串不会通过验证；这些命令不创造合法独立数据。新机器需按照现有 `TOXNET_V3_TRAINING.md` 安装 `requirements-model.txt`、`requirements-v3.txt` 及受支持 CPU/CUDA Torch；预训练目录缺失时明确失败，不运行网络回退或随机权重。

## 本轮验证的证据类型

数值、SHA、标签掩码、来源/任务语义、覆盖分母、固定协议、先预测后读标签、暴露重跑和区间保护由 CPU 软件夹具验证。真实 A4/B16/Morgan 基础分子 batch/public `predict_all` 一致性单独记录在 `artifacts/independent-validation/public-model-parity-20261015.json`，其作用只验证真实工件调用和头顺序，不读取实验标签、不测科学准确率。独立效能仍为待评估。
