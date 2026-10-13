# ToxNet v3：Windows 11 离线提取与复现

所有命令在仓库根目录的 PowerShell 执行。保留原 `.venv` 和 `models/aidd-toxnet`；以下环境/目录均独立。真实数据角色与实验限制见 [计划](TOXNET_V3_PLAN.md)，结果见 [评价](TOXNET_V3_EVALUATION.md)。

## 1. 独立环境与 CUDA 检查

```powershell
git switch feat/toxnet-v3-pretraining
python -m venv .venv-v3
$taskV3Py = '.\.venv-v3\Scripts\python.exe'
& $taskV3Py -m pip install -r requirements.txt -r requirements-dev.txt -r requirements-model.txt -r requirements-v3.txt
& $taskV3Py -m pip check
& $taskV3Py -c "import torch; print(torch.__version__); print('CUDA:',torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'CPU only')"
nvidia-smi
```

本轮实际使用 Python 3.13.2、PyTorch 2.14.1+cpu，CUDA 不可用，所有真实实验均为 CPU。预训练依赖固定 Transformers 4.57.6、tokenizers 0.22.2、huggingface-hub 0.36.2、safetensors 0.6.2。原环境未被 pip 改动。本机创建隔离 venv 的 ensurepip 阶段曾返回 WinError 5，但解释器已生成；本轮验证环境用本地 `.pth` 读取原环境包，只在 `.venv-v3` 安装新增包。这个本机 overlay 不提交，正常复现优先使用上述完整独立环境。如果 Windows 的目录权限阻止 venv 创建，请选择有写入权限的本地目录后使用那个环境的解释器，不修改正式 v2 环境。

RTX 4060 8GB 的 CUDA 实验应先使用 [PyTorch 官方安装选择器](https://pytorch.org/get-started/locally/)核对 Python/wheel/驱动兼容性。例如官方 cu128 源有兼容 wheel 时，可在独立环境替换 torch：

```powershell
# 仅在官方源确实提供与本机匹配的版本时执行；不要在原 .venv 执行
& $taskV3Py -m pip install --upgrade --force-reinstall --no-deps torch==2.14.1 --index-url https://download.pytorch.org/whl/cu128
& $taskV3Py -m pip check
& $taskV3Py -c "import torch; assert torch.cuda.is_available(), 'CUDA unavailable'; print(torch.cuda.get_device_name(0)); print(torch.cuda.get_device_properties(0).total_memory)"
```

若官方没有兼容 wheel，先运行 CPU 路线并记录实际版本，不能把 CPU 运行记为 CUDA 验证。冻结提取从 batch=8 开始，可降到 4/2/1；OOM 明确报错，不自动更换模型或写完成工件。本轮未测 8GB GPU 峰值，不承诺具体吞吐/显存数值。

## 2. 准备真实源数据

若已有原始源和候选数据，直接使用现有只读数据及哈希；缺失时明确失败。新 clone 可在新目录准备来源，避免覆盖任何既有评价：

```powershell
& $taskV3Py scripts\prepare_toxicity_data.py --data-dir data\v3-sources\four-endpoint
& $taskV3Py scripts\prepare_candidate_data.py --candidate-dir data\v3-sources\candidates --metadata data\v3-sources\tdc-metadata.py
& $taskV3Py scripts\prepare_v3_data.py --data-dir data\v3-sources\four-endpoint --candidate-dir data\v3-sources\candidates --output data\v3-development-new --seed 42 --validation-fraction 0.2 --encoder-compatible molformer
```

上述原始下载会依照既有 SHA256 拒绝变化源，ClinTox 的 CT_TOX 核验参考也必须存在。候选 hERG 池只用于结构排除，不作为训练/选择/校准数据。Tox21 只审计角色。FDA 原表及审核映射不存在时明确说明未完成该来源审计，而非构造结构或标签；本机现有 FDA 原表已经核对，仍缺结构映射。

本机现有源对应本轮实际运行的命令：

```powershell
& $taskV3Py scripts\prepare_v3_data.py --output data\v3-development --encoder-compatible molformer
```

该目录已经生成，因此再次执行会拒绝覆盖；重建请使用不同 `--output`。源缺失时可给原数据准备命令加 `--offline` 来确认缺少哪个固定文件；下载失败不要拿随机镜像或生成标签替代。下载/清洗需要原始数据权利审查，仓库不再分发原始或派生分子标签。

## 3. 固定预训练权重与离线加载

```powershell
& $taskV3Py scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
# 可下载权重为 187,248,784 bytes，下载器检查官方固定 SHA256
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $taskV3Py -c "from pathlib import Path; from backend.pretrained_encoder import PretrainedEncoder; e=PretrainedEncoder(Path('models/pretrained/molformer')); print(e.provenance); print(e.encode(['CCO']).shape)"
```

唯一支持的 revision 为 `7b12d946c181a37f6012b9dc3b002275de070314`。权重 SHA256 为 `0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。其他版本需要新审查，不能仅改环境变量或使用浮动 main。

国内网络失败时，可通过允许的网络显式获取[同一个官方固定修订文件](https://huggingface.co/ibm-research/MoLFormer-XL-both-10pct/resolve/7b12d946c181a37f6012b9dc3b002275de070314/model.safetensors)，转移到本机，然后：

```powershell
& $taskV3Py scripts\fetch_pretrained_encoder.py --offline --local-weight 'D:\下载\model.safetensors' --output models\pretrained\molformer-offline
```

显式导入仍验证大小、SHA256 和全部 allowlist 文件；不跳过 TLS、哈希或代码审查，不从未知版本自动执行远程 Python。runtime 本身不联网。已验证完整目录可在离线机器使用；不提交到 GitHub。

## 4. 特征缓存与真实三种子对照

```powershell
# 单独提取（可选）；trainer 也会自动调用相同缓存逻辑
& $taskV3Py scripts\extract_embeddings.py --data-dir data\v3-development --snapshot models\pretrained\molformer --device cpu --batch-size 8 --output artifacts\v3\embedding-cache.npz

& $taskV3Py scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache artifacts\v3\embedding-cache.npz --output artifacts\v3\baselines-new --models B0 B1 B2 --seeds 42 43 44 --device cpu --threads 6 --encoder-batch-size 8 --epochs 40 --patience 8 --batch-size 64
```

本轮真实输出路径是 `artifacts/v3/baselines-20261013`。复现使用新输出路径；默认拒绝所有已存在实验目录和正式 v2 目录。cache 绑定原始/规范输入顺序、源 manifest hash、encoder revision/weights/code、化学引擎、完整 provenance、维度、dtype 与自身 SHA。不能把 `--data` 独立提取的无标签文件缓存当作 `--data-dir` 正式开发缓存，不匹配需显式换新路径。

GPU 检查通过后可将 `--device cpu` 改为 `--device cuda`；CPU 和 CUDA 数值可能不同，缓存 provenance 和实际依赖必须分别记录。不要混用不同精度/设备生成的未知缓存或把失败运行标为完成。

当前缓存契约不单独绑定硬件设备：不同 CPU/CUDA torch wheel 会因依赖 provenance 不同而拒绝复用，但同一 CUDA wheel 在 CPU 与 GPU 提取时可能复用相同 Float32 契约。本轮只实际使用 CPU；CUDA 试验应明确使用新缓存和新输出，不能把缓存命中的耗时当作 GPU 提取性能：

```powershell
& $taskV3Py scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache artifacts\v3\molformer-cuda-new.npz --output artifacts\v3\baselines-cuda-new --models B0 B1 B2 --seeds 42 43 44 --device cuda --encoder-batch-size 8
```

只有 train 拟合 LR/MLP、类权重、embedding 标准化。validation 选择 MLP epoch；四端点缺失任务只在 mask=1 时参与损失。固定超参数、固定三种子，没有旧 validation/test 或 Karim 标签调参。所有种子结果都保留，不能挑最好种子当最终性能。

## 5. 可选冻结融合与推理

先阅读 B0/B1/B2 的实际结果及限制，再另存 F1 探索；不能跳过基线：

```powershell
& $taskV3Py scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache artifacts\v3\embedding-cache.npz --output artifacts\v3\fusion-new --models B0 B1 B2 F1 --fusion-branches molformer morgan --seeds 42 43 44 --device cpu
# 消融分别使用新的目录和 --fusion-branches molformer 或 --fusion-branches morgan

& $taskV3Py scripts\predict_toxnet_v3.py --model artifacts\v3\baselines-20261013\B1-seed-42 --snapshot models\pretrained\molformer --smiles 'CCO' --device cpu --allow-research
```

v3 仍是 development-validation 工件，`--allow-research` 是显式研究用途，不是正式效能认证。主网站没有改供应商，也不读取这些实验模型。schema、完成状态、四任务、真实参数数、SHA、预处理和编码器契约不符会拒绝。`--fine-tune-encoder` 当前明确报“未实施”，不会偷偷训练全 Transformer。

## 6. 测试、工件及复现记录

```powershell
& $taskV3Py -m unittest discover -s test -p 'test_*.py' -v
node --test 'test/*.test.js'
& $taskV3Py -m pip check
node --check app.js
node --check toxicity-core.js
git diff -- models/aidd-toxnet
# 复算真实保存预测的指标、验证全部九个 head 加载及离线推理
& $taskV3Py scripts\verify_toxnet_v3_run.py --experiment artifacts\v3\baselines-20261013 --output artifacts\v3\verification-new.json
# 用新输出保存旧模型 HTTP 功能回归，绝不覆盖历史核验
& '.\.venv\Scripts\python.exe' scripts\verify_local_model.py --output artifacts\v3\v2-http-check-new.json
```

新增单元测试不依赖下载大权重或 GPU；测试夹具的分子、标签、权重和指标只验证软件边界，不写入真实结果。实际模型目录含 manifest、数值 weights、train-only preprocessing、training-history、validation-predictions、training-references 和逐文件 SHA；全实验 `experiment.json` 保存三种子评价与均值/标准差。未定义的指标为 `null` 并标状态。

不向 GitHub 提交预训练权重、embedding cache、原始/派生数据、训练结构/预测清单、API 密钥或本机环境。公开复现证据仅包括代码、审查源、许可、汇总指标/审计和原 v2 不变的哈希证明；本机完整工件继续保留用于离线核查。
