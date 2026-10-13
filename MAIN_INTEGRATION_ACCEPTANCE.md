# 蓝研 ToxNet 四轮 main 整合验收

2026-10-13，按照用户明确授权，PR #1 → #2 → #3 → #4 已依次以普通 merge commit 合入 GitHub main。四个研发分支与原始提交均保留，未使用 squash、rebase、force push 或自动删除分支。此次交付完成代码整合，不发布经过独立科学验证的 v3 产品。

## 合并记录

| PR | 原 head SHA | 实际普通 merge commit | 最终状态 |
|---|---|---|---|
| [#1](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/1) | `ebfa7e44f5c7c737f51b907f7f1af5ea766d1613` | `52b27b92ac7d4cbc9d7de066c69ead2423c20a1c` | MERGED |
| [#2](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/2) | `bb35137fe805ac69aa1343c119386f3a9567ea43` | `a489d2a3c3d04f5ae02bcc5f7a823e7e9e0d031d` | MERGED |
| [#3](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/3) | `c997ae774d40190002f3ec20b807d1c981f1fb4b` | `66f34041638f81a33ae0abb0dd815a6a3e2feff2` | MERGED |
| [#4](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/4) | `3780e7e111c8f816d19cc63b5a3df477308b68c7` | `02a339c7ee2e3e7c738842901922f5f93eca5b10` | MERGED |

合并前 main 为 `51f683e8aba729a2af6221a6b92f8fb5ab4d4ef8`。各次 merge 的两个父提交均核对为“上一 main + 本轮原 head”。后续 PR 改 base 为 main 后，GitHub 文件列表与原增量一致：PR #2 为 23 个文件，#3 为 27 个，#4 为 41 个，没有把前轮内容重复复制。

四轮合并后的源码 main 为 `02a339c7ee2e3e7c738842901922f5f93eca5b10`，其文件树与已验收的 PR #4 head 完全一致。本机检出 main、origin/main 以及实际验收进程均绑定此提交。随后验收记录和文档提交只增加聚合证据、提交范围规则、文档与新 clone 命令说明；后端、前端、测试源码和正式模型字节不变。最新承载提交可查看 [main 历史](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/commits/main)。

## 实际验收

合并前对 PR #4 完整继承、109 个累积变更文件、固定源哈希、安全范围及保护文件重新审查。29 份新增聚合 JSON 无原始实验行、分子结构、标签数据库、embedding 数组或密钥；无新增大型模型权重、虚拟环境、下载缓存和安装包。权限允许普通合并，无必需审批或分支保护阻塞。所有 PR 的 CI 检查为空，没有声称 GitHub Actions 通过。

合并后新执行的结果如下，独立于上一轮 CUDA/浏览器报告：

| 检查 | 实际结果 |
|---|---|
| main 的 Python unittest，`.venv-v3` CPU | 326 项通过，17.963 秒 |
| main 的 Python unittest，`.venv-v3-cuda` | 326 项通过，17.941 秒 |
| main 的 `npm test` | 52 项通过，0 失败、0 跳过 |
| 两套 `pip check` | 通过 |
| `node --check app.js`、`toxicity-core.js` | 通过 |
| 独立 TestClient 真实模型/API 验收 | passed，219.413 秒；从当前 main 新建 app，不使用旧 4183 服务充当证据 |
| 真实研究工件审计 | 42 个运行、252 个工件文件和 3 个固定训练提交核验通过 |
| v2/历史保护 | 10 个文件全部未变；9 个 tracked 文件匹配 main，未分发进度文件匹配旧本机快照 |

真实模型验收覆盖原 v2 四端点、模型信息、联合图嵌入/Morgan Integrated Gradients、中文来源报告、解释前后分数/参数/梯度不变，以及 CUDA B16 的 16 任务与独立十二通路接口。非法 SMILES 返回 422，预期权重 SHA 错配返回 409，缺编码器或损坏复制权重返回 503，不生成替代预测。研究模型未配置的不可用行为也有完整单元/API 回归。

本轮没有重新执行 batch 1/2/4/8 容量探针或完整训练；核验了上一轮真实 CUDA 报告的字节与源码哈希，并实际执行本次 CUDA 研究推理。网络中断期间仅 PR #1 已合并，恢复后继续 #2–#4。首次本机同步失败后误跑的旧 main 测试已明确排除，未计入上述新 main 验收结果。

聚合证据：

- [最终合并与测试汇总](artifacts/main-merge/main-acceptance-20261013.json)
- [合并前安全审查](artifacts/main-merge/premerge-security-audit.json)、[真实 HTTP 预检](artifacts/main-merge/premerge-live-api.json)
- [新 main 真实运行验收](artifacts/main-merge/post-main-runtime.json)
- [新 main 工件与不可变训练来源审计](artifacts/main-merge/post-main-artifact-audit.json)

## 正式模型与科学边界

正式本机模型仍为 `aidd-toxnet-v2-5c22f6cc5dd3`，参数量 2,374,390。原 `/api/analyze` 和 `/api/local-model/info` 保持 v2 行为；v3 不替代正式路径。

| 文件 | 合并前后相同的 SHA256 |
|---|---|
| `models/aidd-toxnet/weights.pt` | `5c22f6cc5dd3e388086f6be75f559277b1dc6a3e155f9067a29b4ac0517c1398` |
| `models/aidd-toxnet/manifest.json` | `6225acc5414233756159bb1015489badb5f4bc5ba484c72367da5bac678af850` |
| `models/aidd-toxnet/evaluation.json` | `e6f60ceb3ca2d2f125e7df74ccdfabc709bf72d6f46ecb9ff49c6f3696f0e6c1` |
| `models/aidd-toxnet/split.json` | `39be266ac00d9af84f858ccdc2471c9fa13972fba326dc1838913ab942440193` |

当前 B16 版本为 `aidd-toxnet-multitask-b16-42-2229faa18f2d`，真实权重 SHA 为 `2229faa18f2dd3240b726efaf2653b163f9b867fb6844aea9f71ab4b6fb4e3b3`。原先训练源码提交 `ebfa7e4`、`bb35137`、`998afbf` 的完整 Git blob 仍可访问；只读核验，不执行历史 Python，不改历史训练清单。

上一轮 RTX 4060 Laptop GPU / 8188 MiB / torch `2.14.1+cu126` 的冻结 MoLFormer 与 B16 batch 1/2/4/8 成功证据完整保留。其容量不能外推完整编码器或生成模型微调。当前没有新独立盲测、独立校准、前瞻评价或已验证适用域；ClinTox 多任务 validation 仅 4 个阳性，B16 存在负迁移，并非所有端点优于 Morgan。

旧 test 和 hERG_Karim 9,978 分子已曝光，不能用于新调参、选择或校准。Tox21 通路活性不冒充四毒性标签，最大 Tanimoto 不等于置信度，归因不等于毒性因果机制，语言模型整理不等于科学验证。所有 v3 仍为 `formalAvailability=false`，合并代码不构成临床或生产发布。

## 本机与新电脑

本机完整 v3 工件分别位于 `artifacts/v3/baselines-20261013`、`artifacts/v3-multitask/baselines-20261013`、`artifacts/integration/fourtask-cuda-20261013`、`artifacts/integration/multitask-cuda-20261013`。审计数据、实验检索索引、embedding 缓存、预训练权重及虚拟环境也保留在本机。这些完整研究工件、分子/标签行、API 密钥和下载缓存没有上传。

新电脑克隆 main 后，正式 v2 按 README 安装运行依赖。研究功能还需要官方匹配 CUDA wheel、显式下载或离线导入固定 MoLFormer 修订 `7b12d946c181a37f6012b9dc3b002275de070314`、合法取得并审计真实数据、复现冻结预测头与检索索引。固定预训练权重 SHA 为 `0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。缺少这些条件时返回不可用，不构造数据或模拟预测。

`--previous-development-dir` 是已曝光历史 v3 参考，不是新生成的四任务数据。新 clone 没有固定历史缓存时，应指定不存在的参考目录，让协议如实记录 `unknownStructureReuse=true`、历史交集不可评估、不能与旧指标声称同划分比较。它不取消 v2 heldout/Karim 的严格隔离。

```powershell
$toxnetMainResearchPy = '.\.venv-v3\Scripts\python.exe'
# 先合法准备原始来源；已有审核缓存时下载准备可用 --offline
& $toxnetMainResearchPy scripts\prepare_toxicity_data.py
& $toxnetMainResearchPy scripts\prepare_candidate_data.py
& $toxnetMainResearchPy scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
& $toxnetMainResearchPy scripts\prepare_v3_data.py --output data\v3-development-new
$toxnetAbsentPriorDevelopment = 'data\historical-v3-development-not-distributed'
if (Test-Path -LiteralPath $toxnetAbsentPriorDevelopment) { throw '请使用不存在的路径，或 SHA 匹配的真实历史参考。' }
& $toxnetMainResearchPy scripts\prepare_multitask_data.py --previous-development-dir $toxnetAbsentPriorDevelopment --output data\v3-multitask-new
```

上面是复现说明，本轮没有再次下载或训练。独立环境、离线导入、固定种子训练及本机已完成工件启动命令见 [CUDA_VALIDATION.md](CUDA_VALIDATION.md)、[TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md)、[TOXNET_V3_MULTITASK.md](TOXNET_V3_MULTITASK.md)。

## 下一阶段建议

推荐分支 `feat/ai-aidd-txgemma`。预计依次实现固定修订生成模型适配器、结构与证据输入合同、来源约束报告、生成分子合法性/事实性/引用真实性评测、独立研究路由及审计日志。复用现有计算、规则、实验与机制假设分开的结果，生成模型不能改写 ToxNet 分数或将生成标签当实验标签。

开始前核查具体模型修订、许可证/访问/商业与再分发条款、数据训练权利、8GB 硬件的实测容量、受控量化/LoRA/服务方案及新的独立评估划分。MoLFormer 许可与容量测试不替代 TxGemma 自身验收。本次任务结束于四轮代码整合，不自动开始下一阶段分支、模型下载、训练或收费 API 调用。
