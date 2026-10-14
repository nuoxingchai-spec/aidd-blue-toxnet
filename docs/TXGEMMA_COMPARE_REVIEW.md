# TxGemma-2B-Predict：Compare 专项任务审查

审查日期：2026-10-13。**适配器已实现，真实 TxGemma 权重未下载，用户条款未代为接受，尚未完成真实模型推理或 RTX 4060 容量验收。** 模型注册不代表安装或可用。全部结果为研究原型，不能作为临床诊断、人体毒性风险或因果机制证明。

## 官方来源与固定版本

- 模型：[google/txgemma-2b-predict](https://huggingface.co/google/txgemma-2b-predict)。固定 commit：`6b0eb81135ad9e73c9c18409c03e315a5a23a18f`，不加载浮动 `main`。
- [官方模型卡](https://huggingface.co/google/txgemma-2b-predict)、[Google HAI-DEF 条款](https://developers.google.com/health-ai-developer-foundations/terms)、[禁止用途政策](https://developers.google.com/health-ai-developer-foundations/prohibited-use-policy)。权重许可证不是 Apache-2.0；需用户本人审阅条款及访问条件。
- Hugging Face 公开模型 API 返回 `gated=auto`，明确要求登录后接受 HAI-DEF 条款。匿名 `raw/{revision}/README.md`、`tdc_prompts.json` 和配置文件均返回 **401**。本轮读取公开模型网页、公开 API 和官方公开 cookbook，没有绕过 gate、使用访问 Token 或下载模型权重。
- 官方公开 [Quickstart notebook](https://github.com/google-gemini/gemma-cookbook/blob/461e9f6fd44015171ca0efdbbfe7979427da8ea3/TxGemma/%5BTxGemma%5DQuickstart_with_Hugging_Face.ipynb)，固定 cookbook commit：`461e9f6fd44015171ca0efdbbfe7979427da8ea3`；下载的 notebook SHA256：`1a965d8b92428b89ac8174213d5bec157bdf3b1fa1584a8c112e73e9e76c9c82`。本项目仅引用其 BBB 示例提示，保留 Google 作者、Apache-2.0 来源归属；没有再分发 gated 模板集或权重。

官方模型卡说明：模型基于 Gemma 2，经治疗开发 TDC 指令任务训练，Predict 需要狭窄的任务提示；2B Predict 不是聊天解释模型。发布权重仅使用符合发布许可的数据集，论文中的任务覆盖不代表每个公开权重都具备每个任务的可靠性。本项目不采用论文聚合表现作为本站预测准确率。

公开 metadata 声明 `Gemma2ForCausalLM`、`model_type=gemma2`、Transformers、safetensors、FP32 参数 **2,614,341,888**。三个权重文件分别为 4,992,576,136 / 4,983,443,424 / 481,381,384 bytes，总计 **10,457,400,944 bytes**（包含 safetensors header；Hub 聚合大小还可能包括 index）。这是源文件大小，**不是已测 GPU 峰值**。项目固定 Transformers 4.57.6 的内置 Gemma2/Tokenizer 实现，`trust_remote_code=false`，只用 `use_safetensors=true`、`local_files_only=true`；禁止 pickle 权重及自定义 Python 代码。

## 首个已审查任务合同

合同：`aidd.txgemma.reviewed-tdc-tasks.v1`。`backend/compare_tasks.py` 提供不可变注册及严格解析。

| Registry ID | TDC 原任务 | 输入 | 真实输出语义 | 状态 | 与四毒性端点直接可比 |
|---|---|---|---|---|---|
| `tdc-bbb-martins` | `BBB_Martins` | `Drug SMILES` | A：不跨 BBB；B：跨 BBB | 支持模板/类别解析，待真实权重验收 | 否，BBB 通透性不是四毒性端点 |
| `tdc-herg` | hERG 候选任务 | SMILES | 阳性定义/模板尚未审核 | unsupported | 否 |
| `tdc-dili` | DILI 候选任务 | SMILES | 阳性定义/模板尚未审核 | unsupported | 否 |
| `tdc-ames` | AMES 候选任务 | SMILES | 阳性定义/模板尚未审核 | unsupported | 否 |
| `tdc-clintox` | ClinTox 候选任务 | SMILES | 任务字段/阳性定义/模板尚未审核 | unsupported | 否 |

四个 unsupported 名称仅是待审查候选，不能当作已核实模型能力。特别是 hERG 阈值、ClinTox 的任务字段和数据集身份不能由名称相似推定一致。

BBB 模板逐字来自官方 notebook 的打印输出，仅将示例 SMILES 替换回其代码中的 `{Drug SMILES}`。仅归一化 CRLF 和 `print()` 末尾换行，SHA256 为 `0b8139032fee96df468b8cab46fd3ca09d9d957558b528be766be41002d93edb`。实际本地 `tdc_prompts.json["BBB_Martins"]` 必须与该文本一致，否则审计拒绝，等待重新审核；不会猜测或替换官方模板。输入使用这个任务提示，**不使用 chat template**。

模型生成部分最多 8 tokens，只接受 A/B 或与字母一致的完整选项文字。返回 `outputType=categorical-text`、`class`、`classLabel`、`positive`，**`probability=null`**；类别文字不转换成 0/1 概率或 softmax 置信度。生成多余解释、概率数字、未知字母或矛盾类别时失败，没有替代预测。盐组分、立体化学及同位素保留在 RDKit canonical SMILES；未建立这些化学类别的科学泛化能力。遇未知 Tokenizer token、超出 2048 token 受控长度或非法 SMILES 时明确拒绝，不截断分子或移除组分。

## 离线文件与许可门禁

`scripts/audit_txgemma_snapshot.py` 只审核用户自行合法下载的本地文件，不读取 HF Token、不访问网络、不下载、不替用户接受条款。需要用户显式 `--license-accepted`；输出 exclusive receipt，禁止覆盖已有 receipt，禁止将 receipt 放入权重目录。

全部 13 个官方源文件均计算 SHA256。小文件实际 bytes 核对固定官方 Git blob SHA1；LFS 大文件的实际 SHA256 + 大小重建标准 Git LFS pointer，再核对该修订公开的官方 pointer blob SHA1。这解决匿名 API 隐藏 LFS SHA256 的情况，**没有编造权重 SHA**。所有固定大小及 blob oid 已写入 adapter；权重、Tokenizer、config、索引、任务模板任一不符均不可用。拒绝路径逃逸、缺文件、Python/pickle 添加文件、`auto_map`、非 Gemma tokenizer、非 safetensors 索引及未知修订。

HF 下载生成的 `.cache/huggingface/` 元数据可保留，但不加载、不上传；receipt 覆盖全部模型源文件。这些下载元数据不属于模型输入或能力证据。receipt 含本机 snapshot 路径，仅服务端使用，不能提交 Git 或出站传输。Adapter 公共状态只暴露实际 ID、revision、dtype/设备、许可状态和原因，不暴露本地路径。

运行需要同时满足 `AIDD_TXGEMMA_ENABLE=true`、`AIDD_TXGEMMA_LICENSE_ACCEPTED=true`、完整本地 snapshot、可信 receipt、固定 revision、依赖和显式设备可用。状态的 `available` 表示这些运行前提已通过，不意味着科学准确率或容量已验证，容量字段仍为 `not-measured`。每次 worker 运行重新核对源 bytes；不自动连接 Hub。

## Windows 11：用户授权及合法下载（本轮未执行）

先由用户本人访问模型页面，登录 Hugging Face 并审阅/接受 HAI-DEF 条款。无需向聊天、前端或仓库发送 Token。确认权重存储/网络和 RAM 足够；公开 FP32 源权重约 10.46GB。本轮没有接受许可，以下命令仅是**授权完成后用户执行**的步骤。

```powershell
Set-Location 'D:\安装包\OneDrive\เอกสาร\ChatGPT\项目实践  二'
$txPython = '.\.venv-v3-cuda\Scripts\python.exe'
& $txPython -c "import torch,transformers; print(torch.__version__, transformers.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CUDA unavailable')"

# 仅在本人接受模型条款后登录；HF 密钥保存在本人 HF 客户端安全配置中。
& '.\.venv-v3-cuda\Scripts\hf.exe' auth login
& '.\.venv-v3-cuda\Scripts\hf.exe' download google/txgemma-2b-predict `
  --revision 6b0eb81135ad9e73c9c18409c03e315a5a23a18f `
  --local-dir '.\artifacts\compare-models\txgemma-2b-predict'

# 国内网络超时：保留已下载内容，用同一官方命令重试。401/403 应检查账号
# 是否被授予该 gated revision 权限，不关闭审计、不用未知镜像或第三方量化权重。
& $txPython scripts/audit_txgemma_snapshot.py `
  --snapshot '.\artifacts\compare-models\txgemma-2b-predict' `
  --output '.\artifacts\compare-models\txgemma-audit.json' --license-accepted

$env:AIDD_TXGEMMA_ENABLE = 'true'
$env:AIDD_TXGEMMA_LICENSE_ACCEPTED = 'true'
$env:AIDD_TXGEMMA_SNAPSHOT_DIR = (Resolve-Path '.\artifacts\compare-models\txgemma-2b-predict').Path
$env:AIDD_TXGEMMA_AUDIT_RECEIPT = (Resolve-Path '.\artifacts\compare-models\txgemma-audit.json').Path
$env:AIDD_TXGEMMA_REVISION = '6b0eb81135ad9e73c9c18409c03e315a5a23a18f'
$env:AIDD_TXGEMMA_DEVICE = 'cuda'
$env:AIDD_TXGEMMA_DTYPE = 'float16'
$env:AIDD_TXGEMMA_TIMEOUT_SECONDS = '180'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'

# 与 Compare 主启动文档合并使用其他 B16/Ollama 配置。
& $txPython -m uvicorn backend.main:app --host 127.0.0.1 --port 4183
```

在 Compare 选择 `tdc-bbb-martins` 发起单次研究运行。先让 B16 结束，再由资源管理器启动 TxGemma 独立进程，然后启动本地 Qwen；不得同时装载三套 CUDA 模型。每个 TxGemma 请求使用独立 worker，结束、超时或取消时终止进程/释放其 CUDA 上下文。显式 CPU 模式可用 `AIDD_TXGEMMA_DEVICE=cpu` 与 `AIDD_TXGEMMA_DTYPE=float32`，但模型 RAM 峰值未测；CUDA 缺失或 OOM 时不会自动切换设备、量化或收费云模型。

当前提供 float16 / bfloat16 的 CUDA 受控选项，未假设 8GB 足够；bfloat16 先检查硬件支持。没有采用未经验收的 bitsandbytes。GPU 日志只有真实成功运行才返回 peak allocated/reserved MiB；不将 MoLFormer 已通过数据套用到 TxGemma。实际 loading/generation 秒、输入/输出 tokens、生成 tokens/s 都在 worker 同步后计时，端到端秒包含进程和验证开销。费用为 `null`，状态为本地无供应商计费，未把硬件电费说成已测零成本。

## 已执行测试与剩余验证

在无需权重、网络或 GPU 的 CPU 单元测试中覆盖：唯一已审核任务、A/B 解析、概率与矛盾文本拒绝、盐/立体/同位素身份、未知任务、授权关闭、缺权重/依赖、未知 revision、未审核 Transformers 版本、显式 CUDA 不可用、合法固定 blob/LFS pointer 审计、SHA 失配、重哈希绕过拒绝、未完成及畸形 receipt、路径/附加 pickle/Python 拒绝、worker Mock 输出、非有限性能字段拒绝、OOM、超时/取消（包括进程创建中的取消）清理。小型 checksum fixture 明确不是 Google 权重，不做真实模型性能宣称。

本模块 **24 项测试**在 `.venv-v3` 和 `.venv-v3-cuda` 各实际通过，四个 Python 代码文件通过 `py_compile`。在 CUDA 环境通过相同 Mock 测试不等于执行了 TxGemma GPU 推理。

```powershell
& '.\.venv-v3\Scripts\python.exe' -m unittest test.test_compare_txgemma -v
```

未完成：用户模型访问授权、合法完整 snapshot 审计、真实 TxGemma B/A 输出验收、CPU RAM/CUDA 显存与吞吐实测、任何同标签科学评测；也未审查完整 gated TDC 模板集。未来新增任务须逐一核查正式原名/标签合同/官方模板 SHA/解析/许可，只有与 ToxNet 实验定义完全一致且采用合法同成员同标签评测，才可设 `directComparable=true`。旧 test 与历史 hERG_Karim 均不能用于本轮新模型选择。BBB 的预测不能列成 hERG/DILI/Ames/ClinTox 直接对照分数，不能混入临床概率或模型投票。
