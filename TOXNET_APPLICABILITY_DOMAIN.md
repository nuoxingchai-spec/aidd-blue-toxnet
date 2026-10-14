# ToxNet 适用域与结构覆盖研究

## 当前状态

本轮实现可运行的训练参考覆盖诊断，尚无独立域内/域外性能验证。返回 `status=not-validated`、`validated=false`、`confidence=null`；不存在“高置信度”或 `evaluated-in-domain/out-of-domain` 声明。Tanimoto 最大值、已见骨架和描述符距离都是结构信息，不是预测正确率或人体风险概率。

## 数据与真实绑定

`backend/applicability_domain.py` 的 `build_reference` 只接受 SHA 核验原模型的训练参考和对应模型权重绑定。训练 canonical 列表完整顺序摘要必须匹配绑定值；成员删除、重排或引用另一模型的训练集合被拒绝。模型 train-only 参考缺失时，不以候选或验证数据临时替代。

私有运行时索引保存 Morgan 指纹、骨架集合与描述符均值、标准差、最小值和最大值。不得把该对象、原训练结构或指纹索引序列化到 API 或公开 GitHub；公共查询输出只含数值范围、计数、合同摘要和限制。

## 已实现诊断

- Morgan radius=2、2048-bit、includeChirality=true；返回查询与原训练参考的最大 Tanimoto。
- 去立体 Bemis–Murcko 骨架新颖性，`scaffoldSeenInTraining` 独立报告。无环查询属于显式 `[ACYCLIC]` 组，不能把它解释成相同骨架具有同等机制。
- 训练分布描述符：MolWt、MolLogP、TPSA、HBD、HBA、RotatableBonds、HeavyAtomCount。
- 各描述符训练 min/max、查询值、是否超范围及最大绝对 z；零方差描述符的变化单独报警，不能除零产生虚假稳定距离。
- 原子规模超训练范围 `atomCountOutsideTrainingRange`；非法 SMILES 或依赖/参考缺失返回 `insufficient-evidence`。

固定探索性阈值为 Tanimoto 0.4、最大描述符 |z| 3.0；任一不满足或常数描述符发生变化时，`coverageStatus=outside-training-coverage-heuristic`，否则 `within-training-coverage-heuristic`。这些阈值没有由独立数据验证，接口明确分开 coverageStatus 与 validated 状态。骨架是否已见作为独立事实展示，不偷偷加入阈值或调成更好的测试表现。

## 后续独立验证合同

获得真正合法独立数据后，应在读取其标签之前预先登记结构参考、阈值、相似度分箱、骨架分层、覆盖定义和拟采用的拒绝预测规则，再报告域内/域外每端点的观测数、阳性数、覆盖率、AUROC/AP/Brier/ECE 与骨架簇区间。

覆盖策略不能隐藏失败或域外成员；必须保留整个新评测成员分母及各被拒绝成员计数。阈值只允许在开发数据固定，不能看过独立测试表现后再“验证”同一阈值。现阶段没有独立分层成绩、拒绝策略效能或分布外检出能力证明，全部保持待评估。

## 离线接口与 Windows PowerShell

```python
from backend.validation_predictors import FrozenResearchPredictor
from backend.applicability_domain import build_reference, query_domain

predictor = FrozenResearchPredictor(model_dir, encoder_dir=encoder_dir, device="cpu")
binding = predictor.binding()  # 复用原模型权重/清单/参考 SHA 检查
reference = build_reference(
    predictor.training_references(), model_binding=binding,
    training_reference_sha256=binding["trainingReferenceSha256"],
)
result = query_domain("CCO", reference)
# 只保存/展示 result，不能输出或提交 reference 中的训练结构。
```

以上基本分子用于功能查询，不带实验标签或性能验证含义。当前本机依赖检查与 CPU 单元测试：

```powershell
Set-Location 'C:\Projects\aidd-blue-toxnet' # 替换为自己的仓库目录
$validationPython = '.\.venv-v3\Scripts\python.exe'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $validationPython -m pip check
& $validationPython -m unittest test.test_applicability_domain -v
```

模块依赖已有 RDKit 和 NumPy；测试不加载大模型或 GPU。真实原模型参考的读取只发生在操作者离线诊断；尚未提供经独立验证的适用域服务，不改原 v2/v3 公共推理结果。
