# 数据、代码与模型许可审查

审查日期：2026-10-15。该清单记录可核实的声明与待核事项，不代权利人授权。没有下载大型数据库或新模型权重；官方页/版本LICENSE为只读元数据核查。

| 对象 | 核实内容 | 待办及本轮处理 |
|---|---|---|
| 本项目自编写代码 | 根目录没有独立LICENSE声明 | 仓库公开不等于通用开源授权；由实际权利人确定代码许可，本轮不擅自补MIT/Apache |
| MoLFormer | IBM模型卡Apache-2.0，revision `7b12d946c181a37f6012b9dc3b002275de070314`；审核本地源码/缓存SHA | 代码/编码器条款与训练数据及派生毒性权重权利分别审核，不上传缓存/大型权重 |
| Google TxGemma | 官方模型访问控制与Google/Gemma使用条款；授权为操作者声明 | 不把其当通用Apache权重，不新增下载或扩大允许任务 |
| 第三方TxGemma GGUF | matrixportalx来源，量化revision `e93f94fc209e1b5971fa96582f346fc7a4d7bec9`，既有GGUF/file/manifest绑定 | 上游Google revision及转换器版本未发布；许可授权不能弥补来源缺项，不上传GGUF |
| TDC工具/四任务/Tox21/Karim | 固定缓存与来源哈希，TDC封装和原始论文/实验数据不同对象 | underlying rights未完整确认；旧数据已曝光，不自动推定商业/再分发权 |
| FDA DILIrank2.0/DILIst | 官方公开页面与已固定文件；无已审结构映射 | 来源独立/实验定义/许可未完全确认，audit-only，不上传行 |
| ChEMBL37 | 官方固定版本LICENSE返回200，CC Attribution-ShareAlike3.0 Unported | 需遵守署名/相同方式共享及EBI条款，具体assay/标签/文献独立性仍未审核；本轮无数据登记或下载 |
| EPA ToxCast invitrodb4.3 | EPA页说明2025年8月版本并含Tox21 | 具体文件/实验/权利尚未审核，不默认来源独立，不下载 |
| 已公开历史派生评价/模型工件 | 本轮保护检查确认历史文件不变 | 原始权利/派生工件再分发范围仍待独立审核；保持历史不动并记录问题，不能补造许可 |

官方依据：

- [MoLFormer固定模型卡](https://huggingface.co/ibm-research/MoLFormer-XL-both-10pct/blob/7b12d946c181a37f6012b9dc3b002275de070314/README.md)
- [Google TxGemma官方模型卡](https://huggingface.co/google/txgemma-2b-predict)
- [GGUF已知来源](https://huggingface.co/matrixportalx/txgemma-2b-predict-GGUF)
- [ChEMBL37固定LICENSE](https://ftp.ebi.ac.uk/pub/databases/chembl/ChEMBLdb/releases/chembl_37/LICENSE)，文件SHA256 `f0c22ca33207ad8a85129a71f1454fcf8f2f71c99e68da5da01538210fa3ffba`。
- [ChEMBL官方许可说明](https://chembl.gitbook.io/chembl-interface-documentation/about)
- [EPA ToxCast来源说明](https://www.epa.gov/comptox-tools/exploring-toxcast-data)

候选合同的 rights/independence/comparison evidence SHA只接受真实本机审核记录，缺失则不合格。下载日期、测量方法、物种、条件、商业/再分发权未知时保留null/unknown/false，不推测填充。默认不允许云端传输数据；本轮未调用DeepSeek或其他收费API。

优先准备一个定义与现有任务一致且源实验新独立的候选，小规模本地结构/来源审计后再考虑封存。ChEMBL许可证有明确允许条件，但并非本项目已获独立评测资格的证明。候选审计详见 `DATASET_INDEPENDENCE_AUDIT.md`。
