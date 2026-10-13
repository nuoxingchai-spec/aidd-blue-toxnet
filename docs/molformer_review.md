# MoLFormer 固定修订审查

审查日期：2026-10-13。来源为 Hugging Face 官方仓库 [ibm-research/MoLFormer-XL-both-10pct](https://huggingface.co/ibm-research/MoLFormer-XL-both-10pct)。本项目支持的唯一修订为 **`7b12d946c181a37f6012b9dc3b002275de070314`**（审查时官方 `compat-v4` 指向的完整 commit）。运行时不解析该分支名，不使用 `main`。

## 模型与许可事实

- 官方模型卡声明 Apache-2.0；vendored Python 文件保留 Hugging Face 原版权及 Apache 许可头，完整许可见 `sources/molformer/LICENSE`。
- 10% ZINC + 10% PubChem 的 SMILES masked-language-model 预训练；12 层、隐藏维度 768、12 个 attention heads、32 随机特征的线性注意力及旋转位置编码。
- 模型卡允许冻结特征提取或下游监督微调；并非分子生成模型。上游没有证据保证可靠毒性机制、概率校准、适用域或本项目四端点性能。
- 官方预训练使用 RDKit canonical SMILES，**去掉了 isomeric 信息**，排除长度超过 202 tokens 的分子，且未验证大于约 200 atoms 的分子。保留本项目输入中的立体/同位素不等于这些信息已被充分学习。
- 权重文件：`model.safetensors`，187,248,784 bytes。官方固定修订 tree API 的 LFS OID（SHA256）：`0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。不下载、不加载同修订的 `pytorch_model.bin`。
- 2026-10-13 已实际下载该权重并验证 SHA256；仅 encoder（不含 MLM head）的实际冻结参数数为 **44,375,040**，trainable=0。CPU 真实 smoke 对 `CCO`、`CCO.[Na+]`、`C[C@H](N)C(=O)O`、`[13CH3]CO` 输出 `(4,768)`、全部 finite；不同 padding batch 的 `CCO` float32 最大差为 `7.15e-7`。此检查只证明可执行的冻结提取，不构成毒性性能证明。
- 审查时 `main` 为 `361063d0ad524ef77cf39b08469f6be770dc550f`，模型卡要求 Transformers 5.12.1，且自定义代码改用了 `create_bidirectional_mask`。本轮明确选择官方兼容 v4 的固定提交和 **Transformers 4.57.6**。不在运行时自动迁移。

## 逐文件审查与执行范围

各文件来自固定修订的 `/resolve/<commit>/<filename>`，完整 SHA256 和大小见 `sources/molformer/review.json`。HF tree 来源记录包含 safetensors 官方 hash。

| 文件 | 审查要点 | 运行时是否执行 |
| --- | --- | --- |
| `configuration_molformer.py` | 仅本地配置和 ONNX 配置类，标准 transformers imports；存在文档用浮动 URL 常量，但加载器不调用这些下载入口 | 验证 SHA256 后导入 |
| `modeling_molformer.py` | 仅 torch/transformers/math/typing、本地相对配置导入；检查了线性注意力、padding mask、masked mean pool、随机特征 buffer、初始化、MLM/分类 heads；未发现 subprocess/网络/动态 eval/exec/读取凭据代码 | 验证 SHA256 后导入，仅实例化 `MolformerModel` |
| `tokenization_molformer.py` | regex+WordLevel vocab；仅指定词表读取/保存，不访问凭据。regex 有可能丢弃不匹配语法，所以本项目先检查拼接无损和完整词表覆盖 | 不导入，留作来源审查 |
| `tokenization_molformer_fast.py` | 包装上述类和标准 HF fast tokenizer，旧兼容 API；存在文档下载 URL 常量 | 不导入，留作来源审查 |
| `tokenizer.json` | `normalizer=null`、WordLevel、2362 vocab、regex、BOS/EOS postprocessor；无需动态 tokenizer Python | 以本地标准 `PreTrainedTokenizerFast` 读取 |
| `config.json` / `tokenizer_config.json` | 自定义 `auto_map` 本轮全部忽略，不通过 Auto classes 动态加载 | 读取 config 的 JSON，显式创建审查类；tokenizer config 不动态执行 |
| `README.md` / `vocab.json` / `special_tokens_map.json` | 模型卡、词表和 special token 对照检查 | 模型卡不执行；词表可审计 |

`convert_molformer_original_checkpoint_to_pytorch.py`、`.bin` 及其他未在 allowlist 的文件不下载、不执行。**不存在 `trust_remote_code=True` 路径**。snapshot 内的 Python 即使存在也不导入；只执行项目 `sources/molformer` 中审查过、与硬编码 hash 一致的代码。checkpoint 使用 `safetensors.torch.load_file`，仅剥离 `molformer.` 前缀，明确排除 MLM head；所有 encoder tensors 使用 `strict=True` 校验，包括 attention feature-map buffer。`deterministic_eval=True`，`eval()`、`requires_grad_(False)` 与 `torch.inference_mode()` 防止冻结提取时随机重绘及梯度训练。

## 输入契约及限制

先调用现有 RDKit `canonicalize_smiles`，保留 canonical isomeric identity；不脱盐、不中和、不删同位素、不删立体、不做 tautomer 合并。模型输入和 cache 记录 canonical 结构，缓存同时记录原始输入顺序 hash。parent/scaffold 隔离只属于 v3 开发数据协议，不改该输入身份。

- `CCO`、合法芳香/凯库勒等价写法都支持并 canonical 化。
- `CCO.[Na+]` 保留全部 fragments；支持 token 不表示盐体系下性能已经验证。
- `C[C@H](N)C(=O)O` 与 `C[C@@H](N)C(=O)O` 保留并区别身份；上游预训练去立体是明确限制。
- `[13CH3]CO`、`[2H]O[2H]` 在固定词表有对应 token；`[999CH3]C` 即使 RDKit 解析成功也明确拒绝未知 token。
- regex token 拼接必须完全复现 canonical 字符串，实际 tokenizer 再检查一次 `<unk>` 及长度，任何丢弃语法/未知 token/超过 202 tokens（含 BOS/EOS）都会报错，**不截断、不隐式跳过 sample**。
- batch 使用 attention mask 和右侧 padding，沿输入顺序输出 `[N,768]` float32，验证 finite。padding 不参与 upstream mean pooling，BOS/EOS 按上游契约参与 pooling。
- 正式 B0/B1/B2 的相同可编码 cohort 必须在标签盲审计中预先形成，不能由 embedding 步骤单独丢弃。

## 下载、离线与显存

```powershell
# 在已安装项目基础与模型依赖的隔离 v3 环境中
.\.venv-v3\Scripts\python.exe -m pip install -r requirements-v3.txt
.\.venv-v3\Scripts\python.exe scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
.\.venv-v3\Scripts\python.exe -c "from backend.pretrained_encoder import PretrainedEncoder; from pathlib import Path; e=PretrainedEncoder(Path('models/pretrained/molformer')); print(e.encode(['CCO']).shape); print(e.provenance)"
```

显式下载脚本仅访问官方固定 URL。国内网络失败时，不自动换源或移除验证；可通过允许的网络/浏览器下载下面固定文件后导入，官方 SHA256 校验始终保留：

`https://huggingface.co/ibm-research/MoLFormer-XL-both-10pct/resolve/7b12d946c181a37f6012b9dc3b002275de070314/model.safetensors`

```powershell
.\.venv-v3\Scripts\python.exe scripts\fetch_pretrained_encoder.py --offline --local-weight 'D:\下载\model.safetensors' --output models\pretrained\molformer
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
.\.venv-v3\Scripts\python.exe -c "import torch; print(torch.__version__, torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
# --data-dir 读取并复核完整 v3 协议；该 cache 可供 train_toxnet_v3.py 复用
.\.venv-v3\Scripts\python.exe scripts\extract_embeddings.py --data-dir data\v3-development --device cpu --batch-size 8 --output artifacts\toxnet-v3\embeddings\molformer.npz
# CUDA wheel 可用后，8GB 显存从保守 batch-size 8 开始，必要时改 1/2/4
.\.venv-v3\Scripts\python.exe scripts\extract_embeddings.py --data-dir data\v3-development --device cuda --batch-size 8 --output artifacts\toxnet-v3\embeddings\molformer-cuda.npz
```

runtime 不发网络请求，HF offline 环境变量为附加约束。CUDA 缺失、snapshot 缺失、依赖不一致、SHA 失败均明确报错。CUDA OOM 捕获为减少 batch 或改 CPU 的操作提示；不静默更换 encoder 或产生随机 features。本轮不提供远程微调或自动整模型 finetuning 路径，后续必须单独注册可审计训练方法。

## 依赖与缓存

固定 `transformers=4.57.6`、`tokenizers=0.22.2`、`safetensors=0.6.2`，下载/Hub依赖固定 `huggingface-hub=0.36.2`；torch CPU/CUDA wheel 单独选择，避免覆盖 v2 环境。提取 provenance 记录实际 torch 版本、源文件 hashes、revision、权重 hash、实际冻结参数数、0 trainable parameters、RDKit 版本、chemistryVersion、pooling/precision/长度限制。

缓存 `.npz` 只使用非 pickle 数组，JSON sidecar 必须匹配 data SHA、raw/canonical 输入顺序 hash、encoder 完整 provenance、维度/dtype/chemistryVersion，验证 npz 文件 SHA256 和 finite 值；存在不完整或不匹配 cache 报错并要求显式更换路径。本地源码/预训练 cache/原始数据/embedding cache 不得混入 v3 正式工件；预训练文件与缓存不提交 GitHub。
