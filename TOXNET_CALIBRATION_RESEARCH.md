# ToxNet 概率校准研究

## 当前状态

真实新校准器为 `not-fitted`。没有新获准、来源和结构隔离的校准数据，本轮不生成真实拟合参数、不改变 B16 工件或原始分数。单元测试中的拟合仅使用明确标注的合成标签，不能作为科研校准验收。

需明确区分原神经网络 `uncalibrated_score`、校准后的**特定实验合同标签概率估计**和人体/临床风险。即使校准充分，前者也不能自动转变为后者；所有新校准工件 `certified=false`、`formalAvailability=false`。

## 已实现接口

`backend/calibration_research.py` 提供：

```python
audit_role_isolation(identity_roles)
fit_calibrators(scores, labels, masks, *, tasks, method, model_binding,
                role_binding, identity_roles, minimum_per_class=20)
apply_calibrators(scores, artifact, model_binding, *, tasks)
calibration_diagnostics(raw, labels, masks, artifact, model_binding,
                        *, tasks, role_binding)
```

模块是离线研究接口，浏览器没有上传原始标签或拟合的 HTTP 端点。调用者必须使用实际原模型绑定及经过审核的角色数据，不得自己将模型生成标签登记为实验观察。

角色必须分别包含 train、calibration、evaluation；跨角色 canonical、母体、立体/同位素近等价、互变异构体和骨架重叠均拒绝。现有保守身份合同把无环结构分为同一组，可能使合法独立校准成员为空；本轮不降低该限制。来源记录 ID 及已声明的文献/队列/实验 ID 也必须隔离，缺失来源不会被程序补造。

train 的 canonical 顺序摘要必须等于 SHA 校验原模型 training references；校准角色固定校准成员和来源顺序 SHA、任务语义摘要、最低支持量、数据清单/协议 SHA、明确标签权限与预先选择的方法。不得使用独立评价标签选择方法或拟合参数。

## 方法与样本保护

| 方法 | 本实现 | 保护条件 |
|---|---|---|
| Platt | 对截断 logit 拟合有截距的 LR，C=1 | 每类默认至少 20；不收敛则拒绝 |
| Temperature | 在 log T ∈ [−4,4] 最小化 BCE | 每类默认至少 20；优化失败则拒绝 |
| Isotonic | 单调回归、域外 score clip | 至少每类 100 且至少 20 个不同分数 |

最低支持量必须事先写入角色合同；测试为了验证方法采用显式合成夹具，不能降低真实角色协议。缺失标签只由 mask=1 参与；缺失任务、单类别、支持不足保持参数 null，应用时不补成原始分数或伪造概率。

工件包括逐端点观测/正负数量、有效支持条件、真实拟合参数（仅合格时）、模型/数据/协议/角色/输入 SHA 和整体摘要。应用要求相同任务顺序及原模型绑定；参数、权重、预处理、编码器、角色或任务更改均不能悄悄复用。

## Brier、ECE 与可靠性曲线

`calibration_diagnostics` 对**实际拟合输入的相同校准成员**输出拟合前后 Brier/ECE 和十个固定等宽 bin 的计数、平均分数、观察阳性率。它复核输入/标签 mask/成员摘要，并标记 `role=calibration-resubstitution`、`independentTest=false`、`methodSelectionPermitted=false`。这类在样本内的图和改善通常偏乐观，不能作为外部校准证据。

真正校准评价需预先固定方法、在单独隔离校准集拟合后，再在已封存且未用于选择的新独立数据上一次性计算对应评价。当前没有这样的数据，真实拟合前后数值与可靠性曲线不填写；不会用旧 test 或 hERG_Karim 填补。

## Windows PowerShell 验证

```powershell
Set-Location 'C:\Projects\aidd-blue-toxnet' # 替换为自己的仓库目录
$validationPython = '.\.venv-v3\Scripts\python.exe'
& $validationPython -m pip check
& $validationPython -m unittest test.test_calibration_research -v
```

方法依赖现有 NumPy、SciPy 和 scikit-learn；无 GPU、无模型下载、无外部 API。后续数据准备和封存遵守 `DATASET_INDEPENDENCE_AUDIT.md`，`purpose=calibrate` 与独立 `purpose=evaluate` 严格分开。真实获准角色资料出现之前，不提供“生成校准参数”的演示命令，避免混淆测试夹具和研究结果。
