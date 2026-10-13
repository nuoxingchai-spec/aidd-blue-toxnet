# 模型 API 接入契约

本项目提供真实 HTTP 调用能力，密钥仅由 Python 服务端读取。默认不内置任何 API 密钥，也不生成虚假的模型成功响应。模型不可用时返回错误，用户可以另外主动选择规则基线。

ChatGPT / OpenAI、DeepSeek、千问、GLM 使用各供应商的 OpenAI-compatible `chat/completions` 接口。用户需要填写其账户可用的 API 模型名、密钥和版本；产品中的 ChatGPT 标签表示 OpenAI API，并不使用 ChatGPT 网页订阅。不同地区可覆盖 `TOXILENS_<PROVIDER>_API_URL`。本轮没有持有效密钥调用这些供应商，兼容链路通过本地 HTTP 契约测试验证。

通用对话模型输出统一标为 `uncalibrated_score`。模型生成的数字和说明是探索性输出，不能当作已验证的 TxGemma 概率。接入真正的 TxGemma-Predict 或其他训练后的分类器时，应部署专用 JSON 预测服务，遵守下列契约。

## 工作台接口

网页通过 `GET /api/providers/{provider}/config` 读取可编辑配置，只返回 `hasApiKey`，不返回密钥。`POST` 同一路径保存配置，支持 `apiUrl`、`modelName`、`modelVersion`、`apiFormat`、`scoreKind`、`timeoutSeconds`、`temperature`、`sendImage`、`jsonMode` 和可选 `apiKey`。密钥省略或留空时保持当前值；`temperature=null` 表示省略。请求必须为同源 JSON。

保存会校验完整配置，原子更新本机 `.env` 并立即替换运行中的模型客户端。其他供应商配置保留；写入失败时不启用新配置。响应不会回显密钥。`restartOverrideFields` 提示重启时由显式系统环境变量覆盖的字段。保存不发起外部推理，也不证明密钥或模型权限可用。

默认不发送 temperature 参数，以兼容只允许默认温度的推理模型。可配置 `TOXILENS_<PROVIDER>_TEMPERATURE`；若模型不接受 JSON mode 参数，可配置 `JSON_MODE=false`，此时仍要求模型内容返回严格 JSON。实际参数会记录在模型 provenance。只有专用服务的 probability 分数接受有证据字段的校准声明，未校准分数保持校准未知。

`GET /api/health` 返回化学引擎版本、供应商列表、配置状态、模型名称/版本和是否启用图片。配置状态只表明配置完整，不能证明密钥有效、供应商可达或模型权限已授权。

`POST /api/analyze`：

```json
{"smiles":"CCO","mode":"model","provider":"qwen"}
```

`mode` 默认 `model`，可显式指定 `rules`；`provider` 可为 `local`、`openai`、`deepseek`、`qwen`、`glm`、`custom`。规则模式不会请求任何模型服务。返回报告包含规范 SMILES、稳定结构标识、RDKit 描述符与 SVG、SMARTS 警示、分离的模型/规则/参考分数，以及完整的版本和验证状态。

### 本机自主模型

`provider=local` 使用蓝研 ToxNet 的实际 PyTorch 权重进行本机 CPU 推理，无需 API 密钥。默认读取 `models/aidd-toxnet`；可通过 `AIDD_LOCAL_MODEL_DIR` 指定另一个已完成训练的工件目录。模型工件缺失、未完成训练、权重校验失败或依赖缺失会返回 503，不回退成规则预测。

`GET /api/local-model/info` 返回模型架构、实际参数量、数据来源、划分计数、独立测试指标、校准拟合状态、训练限制和 SHA256。不会返回训练结构列表或二进制权重。`local` 不使用 `/api/providers/local/config` 配置密钥。

后续外部来源回顾性评估作为可选的 `externalEvaluation` 单独提供。只有评估记录中的模型版本、权重和清单 SHA256 与当前加载模型相符、权重/校准/阈值没有用新标签修改、指标合法时才展示；记录缺失或不匹配不影响本机推理。该字段同时提供固定原训练集基线、来源偏移限制及独立评估 SHA256，不能当作前瞻验证或可靠校准证明。它不修改原始训练清单和内部测试记录。

本机模型返回 `scoreKind=probability`，使用验证集拟合的温度及偏置调整。`calibration.status=measured-not-certified` 表示在内部独立测试中测量过 Brier/ECE，不能理解为临床风险概率已验证。`applicability.status=similarity-heuristic` 是与真实训练结构的 Morgan/Tanimoto 相似度，`validated=false`。原推理结果继续返回 `drivers=[]`、`explanationSource=none`，该接口不提供模型机制归因；规则说明仅解释规则分数。神经归因与来源报告由独立研究接口返回，不改写原结果。模型版本、权重 SHA256、数据版本、训练与推理版本随每次结果返回。详见 [MODEL_CARD.md](MODEL_CARD.md)。

四端点的正类定义：hERG 通道抑制、DILI 药物性肝损伤、Ames 致突变阳性、ClinTox 临床毒性。ClinTox 不等于 FDA 审批状态；若数据源同时含两个任务，应选择正确的标签列。hERG 输出也不等同于 IC50。

## 自定义 JSON 模型服务

配置 `TOXILENS_CUSTOM_API_FORMAT=json`。请求由服务端发往 `TOXILENS_CUSTOM_API_URL`，有密钥时使用 `Authorization: Bearer ...`：

```json
{
  "schema":"toxilens.model.request.v1",
  "model":"configured-model-id",
  "smiles":"CCO",
  "structureId":"canonical-SMILES-sha256",
  "endpoints":["herg","dili","ames","clintox"],
  "scoreKind":"uncalibrated_score"
}
```

响应应完整提供四端点且无重复。下面的数字只说明接口形状，不能作为实验结果：

```json
{
  "modelVersion":"actual-model-revision",
  "endpoints":[
    {"id":"herg","score":0.2,"drivers":[]},
    {"id":"dili","score":0.3,"drivers":[]},
    {"id":"ames","score":0.1,"drivers":[]},
    {"id":"clintox","score":0.25,"drivers":[]}
  ],
  "calibration":{"status":"unknown"},
  "applicability":{"status":"unknown"}
}
```

分数必须是有限的 0..1 数字；布尔值、NaN、无穷大、缺失或重复端点被拒绝。`SCORE_KIND=probability` 表示专用服务返回概率，不能单凭类型声明认定已经校准；`binary_label` 只允许 0 或 1；工作台的综合指数始终是聚合指数，不能解释为概率。

可选说明 `drivers` 最多 8 条，每条为 `{label,detail}`。模型说明与规则贡献分离；模型未提供说明时显示缺失，不用规则说明冒充模型归因。

专用服务的校准声明只有同时包含 `status=validated`、`evidenceId`、`datasetVersion` 才会保留，并注明这是预测服务声明。适用域的 `in_domain` / `out_of_domain` 声明还须带 `evidenceId`、`referenceDatasetVersion`、`method`。工作台不独立核验这些实验材料。缺少证据字段时为 `unknown`。通用对话模型生成 JSON 内的版本、校准与适用域声明不被采信。

## 多模态

为支持视觉输入的具体模型配置 `TOXILENS_<PROVIDER>_SEND_IMAGE=true`。服务端会生成标准 PNG 分子结构图，OpenAI-compatible 请求采用 `text` + `image_url` 内容；自定义 JSON 请求增加 `imageDataUrl`。必须确认具体模型和接口支持图片；同一供应商的其他模型可能只支持文本。纯文本模型使用 `false`。本轮不接受任意文件上传。

## 可追溯性与失败处理

审计报告记录规范结构、API 契约版本、RDKit/规则版本、模型名称、配置版本、供应商返回的模型标识、请求标识、提示词版本、是否含图片、分数类型及验证状态。配置版本与供应商实际返回版本的来源会分别记录。对话模型的 `returnedModel` 可能仍是可变别名，调用方应使用版本化模型并保存实际修订信息。

超时、HTTP 错误、网络错误、非 JSON 响应和契约不符均返回可理解错误，不回显密钥或上游响应原文。客户端禁止重定向以避免将认证头转发到其他站点；仅允许 HTTPS 或本机 HTTP 推理服务。浏览器不会直接请求供应商，也不会接收模型密钥。

## 独立 Tox21 研究接口 v1

原 `/api/analyze` 的四端点语义保持不变。新增 `aidd.research.tox21.api.v1` 使用独立的 `aidd.toxnet.multitask.artifact.v1` 工件，不加入工作台供应商下拉框，不自动部署。

- `GET /api/research/tox21/status`：200 返回 `configured`、`status`、`formalAvailability=false`、`researchOnly=true`、十二个 `tasks` 和语义。不可用时包含 `errorCode`、`message`，无虚构预测。
- `POST /api/research/tox21`：JSON 仅允许严格字符串字段 `smiles`，长度 1..4096；不允许其它字段。成功返回规范结构及 SHA256 `structureId`、十二条 `pathways`（`id`、`assay`、`score`、`positiveClass=assay active`）、`semantics`、`calibration`、`applicability`、`provenance` 和 `requestId`。
- `pathways` 顺序固定为 NR-AR、NR-AR-LBD、NR-AhR、NR-Aromatase、NR-ER、NR-ER-LBD、NR-PPAR-gamma、SR-ARE、SR-ATAD5、SR-HSE、SR-MMP、SR-p53，对应 `candidate_data.TOX21_ENDPOINTS`。分数是未校准的各通路活动模型输出；无 `overall`，不把通路阳性混成临床毒性概率。
- 非法 SMILES / 不受支持的 tokenizer 输入返回 422；未启用研究、缺工件、训练未完成、校验失败、CUDA 不可用返回 503。错误包含 `error.code`、`error.message`、`error.requestId`；没有模型回退或模拟值。

只有显式 `TOXNET_MULTITASK_ENABLE=true`、模型目录与完整所需工件核验成功，且十二通路全部完成监督训练后才可调用。A4 或零通路损失消融工件可离线审计，但不能充当 Tox21 服务。共享原同源/请求大小/安全响应头约束。部署及复现步骤见 [TOXNET_V3_MULTITASK.md](TOXNET_V3_MULTITASK.md)。

## 独立解释与报告研究接口 v1

`POST /api/research/explain` 使用严格同源 JSON，额外字段被拒绝；原 `/api/analyze` 与 `/api/research/tox21` 的预测语义和工件保持不变。请求示例可直接运行，不包含演示预测数值：

```powershell
$body = @{
    smiles = 'CCO'
    model = 'v2'
    endpoint = 'herg'
    steps = 32
    includeLanguage = $false
} | ConvertTo-Json
$result = Invoke-RestMethod -Uri 'http://127.0.0.1:4173/api/research/explain' -Method Post -ContentType 'application/json' -Body $body
$result | ConvertTo-Json -Depth 100 | Set-Content -Encoding utf8 'explanation-result.json'
```

| 请求字段 | 类型、默认值与含义 |
|---|---|
| `smiles` | 必填严格字符串，1..4096 字符；用现有 RDKit 规范化，保留盐、立体、同位素和电荷身份 |
| `model` | `v2`（默认）或 `v3`；v3 必须显式配置且已完成训练，不自动切换 |
| `endpoint` | 默认 `herg`，严格字符串 1..64 字符；必须是实际加载模型的已训练端点 ID |
| `steps` | 严格整数 8..128，默认 32；v2 实际用此步数及最多两倍步数做积分收敛比较，128 步时没有更高步数比较，不标记收敛通过；v3 不使用积分步数 |
| `includeLanguage` | 严格布尔值，默认 `false`；只启用封闭结论排序，原预测与报告事实保持不变 |
| `languageProvider` | 可选严格字符串，最长 64 字符或 `null`；使用已配置的兼容对话接口供应商 ID，未配置时保留本机报告并注明语言服务不可用 |
| `expectedWeightsSha256` | 可选 64 位小写十六进制 SHA256；与原报告实际权重不一致时拒绝解释 |
| `expectedStructureId` | 可选 64 位小写十六进制 SHA256；与当前 canonical SMILES 标识不一致时拒绝解释 |

成功响应的 `contractVersion=aidd.research.explanation.api.v1`，包含下列字段；实际数值由已加载真实模型计算，本文不填写示例实验值：

| 响应字段 | 含义 |
|---|---|
| `status` | `available` 或 `partial`；有真实预测但归因带警告/不可用，或无实验匹配时为 `partial`；顶层状态不证明科学机制或临床效能 |
| `canonicalSmiles`、`structureId`、`generatedAt` | 规范结构、其 SHA256 与 UTC 生成时间 |
| `model`、`endpoint`、`modelPrediction` | 选定模型/解释端点，以及未经报告修改的真实预测、校准、适用域与 provenance |
| `attribution` | 状态、方法、原子/片段、PNG data URL、数值诊断及模型/源码 SHA256；不可用时没有替代贡献 |
| `evidence` | 本机检索状态、最多三条所选端点观察、结构 PNG、Tanimoto、实验正类定义、来源行/版本/哈希、定义一致性与用途限制 |
| `structuralAlerts` | 既有 SMARTS 警示与规则版本，明确 `sourceKind=human-rule`、`notNeuralAttribution=true` |
| `report` | `aidd.source-bounded-research-report.v1`；中文 sections、逐条 claims、sources、`evidenceBundleSha256`、`mechanismAssessment` 和独立 `language` 状态 |
| `provenance`、`limitations`、`requestId` | 模型/权重/报告/契约版本、研究用途与临床限制、关联请求标识 |

v2 `attribution.method=joint-embedding-morgan-integrated-gradients`，单位为校准 logit 贡献，不是概率变化。零嵌入/零指纹基线保留原图，可能偏离训练分布；位贡献到原子的映射存在碰撞/重复环境限制，重叠官能团汇总不能相加。数值检查未通过时返回 `computed-with-warning`，归因预算最多 128 个原子。v3 `method=legal-fragment-deletion-model-rerun` 返回合法片段删除后的真实预测差；`atoms=[]`，片段差异不是加性原子贡献或因果证明。

`report.claims` 每条含 `id`、`sectionId`、`text`、`sourceKind`、`sourceIds`、`confidence`、`limitations`。来源区分 `model-computation`、`model-metadata`、`neural-attribution`、`human-rule`、`experimental-observation`、`mechanism-hypothesis`、`insufficient-evidence` 与 `limitation`；可信度是证据措辞，不是新增置信概率。`report.mechanismAssessment.status=insufficient-evidence`、`verifiedMechanisticClaims=[]`；实验来源只提供背景标签定义，机制章节明确“不确定；尚无充分证据”，数据集文献来源不能冒充分子特异机制验证。

实验索引只从固定来源、原始和处理文件哈希、冻结成员以及确切标签核验成功的数据显式构建；未随仓库分发。默认 `data/evidence-index`，可配置 `TOXNET_EVIDENCE_INDEX_DIR`。索引缺失、来源文件缺失、哈希/标签核验失败或无相似记录时返回 `evidence.status=insufficient-evidence` 和空 `results`，不阻断其余真实本机报告。相似度使用包含手性的 Morgan radius-2 / 2048-bit，与旧 v2 适用域方法分开；Tanimoto 不能当预测置信度。已曝光 test/validation/Karim 观察保留角色，不能用于新调参或称新盲测；不同来源的 hERG 阈值不可默认相同。

可选语言整理只复用 `apiFormat=openai` 且完整配置的对话服务。上游请求只含结论 ID、章节标题、来源类型与可信度措辞，不含分子结构、单条标签、实验行或文献。上游仅允许返回 `aidd.llm.report-plan.v1` 的 `schema`、`sectionOrder` 与 `claimOrder`；必须完整且不重复地排序已有 ID，不得增删、跨章节挪用或附加科学文本。所有文本本机渲染。语言失败分别注明未配置、超时、HTTP 错误或无效响应，保留本机报告。`report.language` 记录配置/返回模型标识、提示词版本、未独立核验不可变快照、供应商管理硬件及账户条款；不声称本地 TxGemma 权重或许可已审核。

| HTTP 状态 | 行为 |
|---|---|
| 200 | 实际预测已获得，归因/检索可以部分不可用；语言模型失败只反映在 `report.language` |
| 422 | 严格请求字段验证失败、非法 SMILES 或选定模型没有训练该端点；字段错误由既有处理器返回 `error.code=INVALID_REQUEST`，业务错误也使用 `error` 格式 |
| 409 | `EXPLANATION_STRUCTURE_CHANGED` 或 `EXPLANATION_MODEL_CHANGED`；输入结构或真实权重与原报告不一致，需重新筛选 |
| 503 | 模型未配置、工件/依赖不可用或解释基础设施异常；返回明确不可用，不提供模拟预测 |

业务错误包含顶层 `status` 和 `error.code`、`error.message`、`error.requestId`。同源、请求大小限制及安全响应头沿用现有后台。模型密钥和上游响应原文不返回浏览器。JSON 导出是用户主动下载的本机研究报告，可能含本机检索的来源观察，应遵守底层数据许可，不将导出当作允许公开数据库的授权。方法、验证和启动流程见 [EXPLANATION_REPORTS.md](EXPLANATION_REPORTS.md)。
## 独立四模型 Compare（aidd.multimodel.compare.v1）

原 `toxilens.api.v2` 与研究 Tox21/解释 API 保持原语义。新增工作台 `/compare`，详细操作见 [AIDD_COMPARE_WORKBENCH.md](AIDD_COMPARE_WORKBENCH.md)。

| 方法 / 路径 | 含义 |
|---|---|
| `GET /api/research/models` | 四注册项的实际状态、ID、revision、许可、设备、任务合同和资源限制；配置不是可用证明 |
| `POST /api/research/compare/runs` | 严格请求，返回 HTTP 202 与 `runId` / `pollUrl`；不等待大模型结束 |
| `GET /api/research/compare/runs/{runId}` | 进程内运行状态、规范结构、四模型独立结果、共同事实、已核验报告、限制和真实性能 |
| `DELETE /api/research/compare/runs/{runId}` | 取消等待清理；已终态结果保留到 TTL；不存在或过期 404 |
| `GET /api/research/compare/presets` | 仅研究配置，不包括密钥/分子/问题/授权 |
| `POST /api/research/compare/presets` | `{name,settings}` 原子保存，201；未知字段拒绝 |

请求字段：`smiles`（合法，1–4096字符）、`question`（最多1000字符）、`mode="compare"`、固定 `primaryPredictor="toxnet-v3-b16"` / `specialistPredictor="txgemma-2b-predict"` / `primaryReporter="ollama-qwen-local"`、`specialistTask`（注册ID或null）、`comparisonReporter`（`deepseek-api`或null）、`ollamaModel`（真实本机Qwen ID或null）、严格布尔 `includeTox21` / `includeEvidence` / `allowCloudTransfer`、`explainEndpoint`（原四端点ID或null）。额外字段、凭据、URL和类型强制转换均拒绝。POST必须JSON，同源检查覆盖POST/DELETE。

运行状态：`pending/running/completed/partial/failed/cancelled`；模型还可 `unavailable/unsupported/skipped`。单模型结果有 `status/data/metrics/provenance/error`，失败不清除其它成功结果。B16始终返回实际16任务，`includeTox21`只控制报告/界面展示。TxGemma只返回审核的类别结果，`probability=null`，不与毒性标签直接比较。未核验报告 `data=null`；已取得的实测metrics/provenance可保留。

`snapshot` 为 `aidd.compare.facts.v1`，带canonicalJSON SHA256与事实ID/来源/章节；`reports` 为确定性核验后的 `aidd.compare.report.v1` 结果。报告器精确复制分数与来源，只能排序已有章节内结论。所有自由commentary标记unverified，不能当作实验事实。两个报告独立收到相同快照，不读取对方结果。

`cloudTransfer.allowed` 是本次授权；`requestAttempted` 表示曾尝试HTTP，不证明供应商收到/计费；`executed` 表示报告通过确定性核验。允许的类别为查询canonical结构、核验模型分数、任务语义、规则/归因摘要、证据状态聚合和研究限制。原始实验行、样本标签/结构、受限数据库、密钥、私有路径和自由问题不外发。无授权时DeepSeek不执行；预设不会持久化授权。

`metrics` 仅填真实测量：duration/queue/wall、实际返回tokens/token/s、PyTorch子进程峰值或null。`cost=null`表示未获真实账单，不是零费用。所有v3结果 `formalAvailability=false`；没有临床综合概率、模型投票或准确率排名。

默认本机阶段串行、运行并发1、队列32、阶段240秒、总计900秒、终态TTL1800秒（按请求清理）。运行与报告仅在内存，重启丢失；JSON导出含完整当前结果。取消后未确认Ollama清理则隔离后续本机阶段；原v2推理路径不替换。
