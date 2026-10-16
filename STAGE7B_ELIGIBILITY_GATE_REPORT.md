# Stage 7B 补充验证：资格门禁与交付

2026-10-16（Asia/Shanghai）。**`STAGE7C_BLOCKED`**。本轮扩大了真实无标签元数据采集，补充了历史来源结构覆盖和可复现的 EPA 映射合同；尚无已批准、来源独立且达到预定评价要求的候选集，不能进入 Stage 7C。

## 实际仓库与保护边界

开始核验结果与用户摘要一致：分支 `codex/stage7-labelblind-source-audit`，基点 `7e47c55d6e4a2f3580938c8f0eac237a7127c51b`，main 为 `3a7e5138398f8eea5f07ae14a083135bbc94d26b`。GitHub 实际读取确认 PR #6 为 Draft、open、未合并，其 head 仍为 `7604ad1e9fb4ea77c6fa8879892e9fe9aa879c9e`。本轮没有修改 main、合并或调整 PR 状态。

初始 `STAGE7A_DATA_SOURCE_REVIEW.md`、`STAGE7B_CANDIDATE_AUDIT.md` 与原三份聚合回执保持原字节，不将扩展结果覆盖原 200 条先导统计。正式 v2、A4/B16/Morgan/MoLFormer 相关工件和原四端点定义未修改，所有 v3 继续 `formalAvailability=false`。

## 四项交付与实际科学进展

| 任务 | 实际执行 | 仍未完成的科学条件 |
|---|---|---|
| A：hERG 标签合同 | 核对原训练/清洗/任务合同继承链，重核原数据文件 SHA；实际查阅 TDC、Crossref、Europe PMC、PubMed 和 ACS 可访问状态 | 仅可确认 1 blocker / 0 non-blocker；原阈值、比较符、边界、单位及具体 assay 构成仍为 `MANUAL_REVIEW_REQUIRED` |
| B：EPA 结构映射 | 审查官方 CTX OpenAPI，实际请求 3 个 DTXSID-only 成员接口；实现严格离线成员—DTXSID—DTXCID/结构审计 | 3 次请求均 401，无实际成员/结构；v4.3 绑定、许可、批次与来源尚缺，不将软件夹具当作映射成功 |
| C：ChEMBL 扩展 | 真实取得 2,000 条活动元数据、237 个 assay、161 个 document；冻结顺序/投影/预算，执行结构与来源排除 | 是 41,078 条目标活动元数据中的有界范围；不是全目标/全库筛查，原实验和队列独立性仍未确认 |
| D：盲态 | 未请求候选数值、关系符、结果质量标志或标签；冻结“不允许映射”的预审核政策 | 没有独立数据管理员及隔离证明，数值相关分类必须暂停；不能声称已完成隔离保管或批准标签政策 |
| E：工程 | CPU 全部 Python 测试发现、JS、依赖、编译及历史保护检查 | 软件通过不等于科学验收；未执行浏览器/GPU/真实候选效能验收 |

各阶段结构数量和排除原因以 [ChEMBL 扩展审计](STAGE7B_CHEMBL_EXPANDED_AUDIT.md) 及对应机器回执为准，不能将初始 17 个结构侧通过与扩大后的成员计数直接相加。EPA 的实际候选数、缺失结构数、歧义数和通过数均为 `null / not-run`，不能填写 0 冒充完成筛查。详细许可、成员路由和三端点实验定义见 [EPA 结构审计](STAGE7B_EPA_STRUCTURE_AUDIT.md)。

最终实际计数：

| 阶段 | ChEMBL 扩展 | EPA |
|---|---:|---|
| 活动/成员记录 | 2,000 条无结果字段活动元数据 | 成员请求 3 次 401，成员数量 null |
| 有效唯一 canonical 结构 | 1,638 | null，未取得结构 |
| 原固定参考命中历史的唯一结构 | 1,267 | not-run |
| 原固定参考下结构与重复筛查通过 | 325 | not-run |
| 追加完整历史源后观察到的历史重叠唯一结构 | 1,273 | not-run |
| 追加历史源后暂未命中且无重复 | 319，**仅暂定观察** | not-run |
| 完整历史覆盖下核验通过 | **null** | null |
| 全部独立资格通过 | **0** | null，尚无可审计集合 |

组合参考下的互斥行处置为：历史结构/骨架重叠 1,585、重复 96、等待任务/来源/权利/保管审核 319，合计 2,000。层级和来源命中数相交，不能重复扣除。

追加检查完整 Karim 原源 13,445 行，全部可建立身份；Tox21 原源 7,831 行中 7,822 可建立身份，**9 行在当前支持规则下身份未解决**。不擅自修复或丢弃这 9 行来宣布历史覆盖完整，故 319 不能写成核验通过，更不能写成独立合格。原固定参考下的 325 与追加参考下的 319 是不同覆盖范围，不覆盖原报告、也不改变原资格标准。

## 科学标签与实验端点门禁

[hERG 标签合同核验](STAGE7B_HERG_LABEL_CONTRACT_REVIEW.md) 给出了六项人工决策。当前只有原标签方向可确认，不能从 Karim 或 hERG_Central 的 10 μM 定义倒填原 TDC/Wang hERG。原代码没有把连续 IC50 按统一规则重新二值化。

| 端点或测量 | 本轮科学适用性结论 |
|---|---|
| 经验证的人类 hERG 功能电流 IC50 | 可优先做方案核查，尚未批准与原二分类合同直接比较 |
| EPA 686 / NVS_IC_hKhERGCh | 放射配体结合，不能自动代替功能电流阻断或原来源标签 |
| EPA 3184 / ERF_TW_IC_hKCNH2 | 放射配体结合，同样缺少可比性与来源/批次证据 |
| EPA 3210 / TOX21_hERG_U2OS_Antagonist | 功能报告读出与 binding 注释冲突需核查，并存在历史 Tox21 来源家族风险 |
| Ki/Kd、EC50/AC50、hitcall、单点 inhibition % | 不直接映射成原 hERG 标签；不与 IC50 混合，也不转换成模型概率 |

ChEMBL 文献/assay/target/activity ID 可追溯不等于原始实验已独立。汇编、综述、计算研究、跨库导入以及 Wang 别名尚需映射；结构互斥不能抵消同来源家族或未知实验队列。MoLFormer 预训练结构曝光仍为 unknown。

## 标签隔离和提前冻结

真实扩展获取前已写 `plan.json`，固定最多 2,000 条、按 activity_id 排序、全部投影字段、分页、预算、身份规则及预审核政策 SHA。政策内容摘要为 `720daf2db990068fc43f2ac70ff45a71a095ed40803a722e9bcc2030ce67ffe7`，阈值/比较符/边界均为 null，`labelMappingExecutable=false`。

这冻结的是审查限制，**不是已批准的标签映射或评价协议**。代码 `require_custodian_mapping()` 始终拒绝执行，无调用者开关能开启标签分类。不能以独立线程、子代理或同一开发环境中的另一个进程代替独立数据管理员。

精确 IC50、范围、不等式、缺失活性值和结果质量异常的数量均为 null / not-inspected。未读 `relation/standard_relation/value/standard_value/pchembl_value/activity_comment` 等字段，不将元数据 `IC50+nM` 写成精确测量，不把未知结果补成阴性。单位缺失和 `ucm` 等元数据异常可以记录，但不静默换算或修正。

开发流程的 `developerLabelBlindnessMaintained=true` 仅记录本次未接收候选结果，不是第三方保管审计或不可篡改证明。计划时间证据来自控制流和本机文件时间；没有外部可信时间戳。未来需要能核查身份、访问边界、签署时间与证据 SHA 的真实保管方。

机器门禁回执见 [eligibility-gate.json](artifacts/independent-validation/stage7b-supplement-20261016/eligibility-gate.json)。除主审计脚本的执行源码摘要外，它额外固定 9 份共享 helper 的字节摘要，并核对这些 helper 与 `7e47c55` 基点逐字节一致；这是本机复现依据，不是独立签署的执行证明。用户已确认尚未配置 CTX Key，因此本轮按阻塞流程交付，不扫描凭据或继续成员/结构请求。

## 评价规模与协议尚未获准

沿用 Stage 6 的最低区间支持规则：每类至少 10 个观测、至少 10 个骨架簇；500 次簇 bootstrap。它们是既有指标/区间计算的支持保护，**不是本轮数据已满足统计效能或样本量设计的证明**。

本轮未查看类别数量，不能宣布支持条件满足。还需要独立保管人提供不向开发者透露类别分布的支持条件审核结论，以及科学审核者预先批准的评价规模/效能要求。当前没有 approved label policy、独立 CandidateMetadata 资格批准、冻结的候选成员/模型绑定评价协议或标签许可。结构数量再多也不能将这些未知状态改成 ready。

## 许可、使用范围与数据发布

- ChEMBL 37 固定 LICENSE 为 CC BY-SA 3.0，有署名和相同方式共享等条件；底层论文全文、补充材料、具体提交来源及项目用途另行审核。不能把许可证字节 SHA 当成审核人已批准。
- EPA API 免费不等于无需个人授权，也不等于所有底层数据允许任意用途。固定 Figshare v11 本轮 403，不能声称已核验该发行许可。Live CTX 成员列表还需与 invitrodb 4.3 绑定。
- 本地科研、商业用途、再分发和云端外传分别处理；本轮没有批准这些扩展权利或调用付费模型。
- 私有源响应、结构/成员行、密钥和日志保留在私有/忽略目录。Git 仅精确放行聚合 JSON 与源码、测试和文档；SHA 回执不能自动授予原始数据库再分发权。

## 实际测试与未执行项

本轮在 `.venv-v3` CPU 环境真实执行：

| 命令/范围 | 实际结果 |
|---|---|
| `python -m unittest discover -s test -p 'test_*.py' -v` | **755 项：753 通过，2 跳过，0 失败/错误**，55.526 秒 |
| `npm test` | **135/135 通过**，0 失败/跳过 |
| `python -m pip check` | `No broken requirements found` |
| 10 份新增 Python 源码/测试 `py_compile` | 通过 |
| `verify_compare_preservation.py` | 50 份历史科研文件及 4 份原 Compare 回执 unchanged |

两项跳过是 Windows 环境不能创建 symlink、POSIX 文件模式检查不适用。755 项是 CPU 软件/接口/现有 v2 工件合同回归，包括明确标注的合成 provider、编码器、训练和保管投影夹具；不是 755 次候选模型推理，也不是独立科学验证。新增本轮软件测试为 46 项，最初 Stage 7 的 36 项及更早测试仍保留。

没有执行：真实 EPA 结构映射（缺成员/授权/版本证据）、候选结果限定符与质量分类（缺独立管理员及批准规则）、A4/B16/Morgan 在候选上的评价、类别分布/准确率排名、真实模型重训/校准、GPU 性能与浏览器交互、标签封存和 Stage 7C。当前没有修改 UI/HTTP/模型推理路径，本轮不把已有历史浏览器或 CUDA 结果算成新验收。

可复现命令和缓存缺失处理分别在前三份专门报告中。完整回归可在仓库根目录运行：

```powershell
$stage7Py = '.\.venv-v3\Scripts\python.exe'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
$env:TOXNET_MULTITASK_ENABLE = 'false'
$env:AIDD_TXGEMMA_ENABLE = 'false'
$env:AIDD_TXGEMMA_LICENSE_ACCEPTED = 'false'
$env:TOXNET_VALIDATION_ALLOW_LABELS = 'false'
& $stage7Py -m unittest discover -s test -p 'test_*.py' -v
npm test
& $stage7Py -m pip check
& $stage7Py scripts/verify_compare_preservation.py
git diff --check
```

## 下一步与停止条件

1. 优先补 HERG-01/02：取得合法方法摘录，确认原阈值、比较符、单位、边界及具体实验组成。缺失保持 `MANUAL_REVIEW_REQUIRED`，不默认 10 μM。
2. 用户取得免费 CTX 个人授权，或由独立管理员提供 result-free 的固定版本成员/结构投影；同时补齐 invitrodb 4.3 绑定与许可。Key 只解决鉴权，不会自动解决科学资格。
3. 补齐逐文献—原实验—实体—批次/队列与历史别名/跨库导入映射，解决端点注释冲突及使用范围。未解决不放行，不按模型预测筛样本。
4. 建立真实的隔离保管机制，在结果接触前批准新版本映射/删失/范围/缺失/异常/重复规则，并冻结合法成员、模型绑定、支持要求与评价协议。

只有全部证据、足够规模、冻结协议和标签隔离均获真实审查后，才能在后续审查提出 `STAGE7C_CANDIDATE_READY`。当前结论始终为 **`STAGE7C_BLOCKED`**，本轮停止于补充证据和资格门禁，不开展正式独立盲测。
