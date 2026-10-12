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

本机模型返回 `scoreKind=probability`，使用验证集拟合的温度及偏置调整。`calibration.status=measured-not-certified` 表示在内部独立测试中测量过 Brier/ECE，不能理解为临床风险概率已验证。`applicability.status=similarity-heuristic` 是与真实训练结构的 Morgan/Tanimoto 相似度，`validated=false`。`drivers=[]`、`explanationSource=none` 明确没有模型机制归因；规则说明仅解释规则分数。模型版本、权重 SHA256、数据版本、训练与推理版本随每次结果返回。详见 [MODEL_CARD.md](MODEL_CARD.md)。

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
