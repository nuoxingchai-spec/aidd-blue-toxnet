# 第六阶段交付记录

2026-10-15：科研验证基础设施已完成，**独立科学验收未完成**。本记录与旧验收文档分开保存。聚合回执为 `artifacts/independent-validation/stage6-acceptance-20261015.json`；GitHub研究分支的实时CI应以对应head SHA为准。

## Git与保护

[PR #5](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/5) 已在前轮完成必要审查，并用普通merge合并。再次读取GitHub确认 merged=true；main为 `3a7e5138398f8eea5f07ae14a083135bbc94d26b`，两个父提交为 `3e0cbe4` 与 `01daeaa`。没有绕过保护/强推/改写main。新代码在 `feat/toxnet-independent-validation`，交付新Draft PR，保持未合并。

保护脚本重新核验 **50份历史科研文件、4份原Compare回执 unchanged**。v2权重与API、B16、MoLFormer、split、历史test/Karim结果及旧验收文档不变。B16 weights SHA为 `2229faa18f2dd3240b726efaf2653b163f9b867fb6844aea9f71ab4b6fb4e3b3`；manifest为 `d8936e9d009e87cff4ae0695ff7cdee011a81f338b3b38a7927ace5c3125864c`。本轮未训练、未下载新模型/大数据、未调用付费API。

## 真实科学证据

新的标签盲inventory核验历史参考完整：Karim原缓存13,445结构中13,385存在多层历史重叠，60个结构侧不重叠仍因来源已曝光/权利与独立性不足而不合格；既有回顾评价9978分子的历史不改。Tox21全部7822结构命中历史参考（包含已用于开发的Tox21自己），不能冒充新盲测。FDA1336/1279个drug记录原始SHA核验成功，但没有已审结构映射或完整权限。

ChEMBL37固定LICENSE已实际读取并确认CC-BY-SA3.0条件；这只是合法使用的候选条件，不证明任意assay与旧任务定义/来源独立。尚无ChEMBL数据下载/登记。ToxCast包含Tox21，独立性不可默认。**没有候选达到新独立评价资格。** 详见数据审计与许可审查。

A4/B16/Morgan包装器在两个公开基础分子上通过真实CPU parity：与原 `predict_all` 最大差分别 `5.18e-7/2.97e-7/0`，权重/清单/头顺序一致，没有读取实验标签。这证明调用一致性，不证明毒性效能。新评价器可在合法封存数据下按同成员/同语义公平比较，缺失标签和失败成员保留分母；目前未生成独立指标。

真实开发记录三种子重算四端点AUROC/AP/Brier/ECE和配对骨架bootstrap。ClinTox81观测、4阳性，A4 AP均值 `0.471599`、B16 `0.309444`，差 `−0.162155`；区间支持不足。A4/B16训练行数与辅助任务同时不同，种子不是独立数据，未测梯度干扰，因此只能提示开发风险，不能判定因果负迁移。新500次分析独立存档，原100次文件不覆盖。

校准的Temperature/Platt/Isotonic及训练/校准/评价隔离检查已实现，没有合格真实校准集，**未拟合真实校准器**。AD的Tanimoto、骨架新颖性、描述符范围/z距离与大小提示已实现，但独立域内/域外误差及拒绝效能未知，**未验证适用域**。所有v3保持 `formalAvailability=false`，没有科学准确率排名或临床概率声明。

## 实际工程验证

| 执行环境 | 实际结果 |
|---|---|
| Windows完整CPU `.venv-v3` | 673项，671通过，2跳过，0失败 |
| WindowsCUDA `.venv-v3-cuda` 软件套件 | 673项，671通过，2跳过，0失败；不等于673次GPU推理 |
| JavaScript | 135/135通过 |
| 三个环境pip check、六个JS语法、Pythoncompile、diff、保护检查 | 通过 |
| 基础 `.venv`，缺可选Transformers | 673项，668通过、3跳过、2旧TxGemma依赖测试失败；原记录保留，不能当完整研究环境 |

新增软件测试72项，覆盖严格数据/源码/SHA/角色/来源/结构/标签合同、单类/少样本/配对/覆盖率、校准输入绑定、AD未知、注册与seal交叉绑定、立即关闭多个排队任务、并发取消、清理等待、重启隔离、聚合隐私与原HTTP合同。软件fixture不当成真实实验。CI不下载大模型或原始数据，不调用付费API。

真实 `/validation` 浏览器完成标签盲审计（CPU，清理确认）、历史只读恢复、切换数据失效、评价默认禁用、聚合JSON下载与API字段完全一致。另一真实审计取消后清理确认，没有未完成指标。实际API拒绝路径/额外字段422、跨源403、非JSON415、未登记候选/不合格评价409；原health、rules、Compare页面200，v2版本仍 `aidd-toxnet-v2-5c22f6cc5dd3`。

原Compare链接与页面已检查；本轮没有重跑B16→TxGemma→Qwen/DeepSeek。真实独立数据、校准拟合、AD验证、新梯度消融与收费API均未执行，具体原因是无合格数据/权限或本轮明确不授权训练与付费调用。

## 文件与入口

| 新增/调整 | 职责 |
|---|---|
| `backend/dataset_overlap_audit.py`、`independent_validation_data.py` | 历史禁入、严格元数据、协议、标签盲审计、封存与许可 |
| `backend/validation_predictors.py`、`independent_validation_metrics.py` | 复用原权重、公平批量预测、同成员指标与区间 |
| `backend/calibration_research.py`、`applicability_domain.py`、`negative_transfer.py` | 独立可靠性研究与已曝光开发观察 |
| `backend/validation_service.py`、`validation_routes.py`、`validation_worker.py` | 登记ID、CPU任务、异步API、取消/恢复/SHA聚合报告 |
| `scripts/audit_independent_dataset.py`、`register_independent_dataset.py`、`evaluate_independent_models.py`、`analyze_negative_transfer.py` | 只读审计、操作者登记、离线评测/分析CLI |
| `validation.html`、`validation-app.js`、`validation-core.js`、`validation.css` | 独立浅蓝科研验证界面与导出 |
| `backend/main.py`、`index.html`、`compare.html` | 新路由/入口、生命周期和同源安全集成 |
| `test/test_*validation*.py`、相关metrics/calibration/AD/negative测试与两个validation JS测试 | CPU回归、状态机与合同 |
| `.github/workflows/compare-cpu.yml`、`.gitignore`、`.env.example` | CI新分支/main触发、仅聚合放行、标签默认禁止 |

用户要求的六份文档已补齐：[协议](INDEPENDENT_VALIDATION_PROTOCOL.md)、[数据审计](DATASET_INDEPENDENCE_AUDIT.md)、[模型比较](TOXNET_MODEL_COMPARISON.md)、[校准](TOXNET_CALIBRATION_RESEARCH.md)、[适用域](TOXNET_APPLICABILITY_DOMAIN.md)、[科学限制](TOXNET_SCIENTIFIC_LIMITATIONS.md)。另补[许可审查](LICENSE_REVIEW.md)，README与API合同仅新增入口说明。

启动与可复现PowerShell命令见协议文档。优先下一步：取得一个合法、实验定义一致且来源新独立的数据源，由外部保管人冻结标签，完成保守结构/骨架/来源排除后按当前协议评价；校准使用另一个隔离角色。独立封存评价不能成为后续任务权重或模型选择的调参池。
