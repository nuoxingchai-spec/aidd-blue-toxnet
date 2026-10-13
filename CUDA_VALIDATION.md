# ToxNet v3 Windows CUDA 验证

本轮验证独立 GPU 环境，保留正式 v2 的 `.venv`、CPU `.venv-v3`、原权重及历史评价。识别到显卡不等于通过真实 CUDA 模型验收；最终结果以本页实测节与机器可读 JSON 为准。所有 v3 工件保持 `formalAvailability=false`。

## 1. 驱动、Python 与官方 wheel

本机驱动查询识别 `NVIDIA GeForce RTX 4060 Laptop GPU`、驱动 `566.24`、总显存 `8188 MiB`；`nvidia-smi` 显示的 CUDA 12.7 是驱动支持上限，不是当前 Python 的 CUDA 构建。原 `.venv-v3` 的 `torch 2.14.1+cpu` 没有 CUDA，真实记录在 [cuda-cpu-wheel-verification.json](artifacts/integration/cuda-cpu-wheel-verification.json)，正确标为 `not-tested-no-cuda-device`，其 reason 明确是 CPU wheel，不能据此说物理显卡不存在。

官方 `cu126` 索引提供 Python 3.13 / Windows amd64 的 `torch 2.14.1+cu126`。CUDA 12.6 与本机驱动支持范围一致。本轮记录的官方 wheel SHA256 为 `92d0ae66f10ae9dfaaf85d684d6794a3d6ca4ed938dcfaa25e94c54cff3764eb`，大小 2,602,769,784 字节；不要给其它 Python/架构套用这个 wheel。

从仓库根目录运行，创建独立环境，不修改原环境：

```powershell
python --version
nvidia-smi --query-gpu=name,driver_version,memory.total --format=csv,noheader
python -m venv .venv-v3-cuda
$taskCudaPy = '.\.venv-v3-cuda\Scripts\python.exe'
& $taskCudaPy -m pip install 'torch==2.14.1+cu126' --index-url https://download.pytorch.org/whl/cu126 --timeout 300 --retries 3
& $taskCudaPy -m pip install -r requirements.txt -r requirements-model.txt -r requirements-dev.txt -r requirements-v3.txt
& $taskCudaPy -m pip check
& $taskCudaPy -c "import sys,torch; print(sys.version); print(torch.__version__,torch.version.cuda); print(torch.cuda.is_available()); print(torch.cuda.get_device_name(0) if torch.cuda.is_available() else 'No usable CUDA device')"
```

`requirements-model.txt` 的 `torch==2.14.1` 接受同版本的已安装 `+cu126` 构建，不应先装 CPU wheel 再在原正式环境里替换。若 Python 版本不同，先查看官方索引是否有匹配 wheel，不能把 cp313 文件重命名后安装。不要使用不明镜像或关闭 TLS 验证。

## 2. 国内下载失败与离线安装

本轮官方备用 CDN `download-r2.pytorch.org` 的大文件首次下载超时，主站 `download.pytorch.org` 可达且支持 HTTP Range。Python 3.13 Windows 可使用同一官方文件的固定主站地址及索引哈希：

```powershell
& $taskCudaPy -m pip install 'https://download.pytorch.org/whl/cu126/torch-2.14.1%2Bcu126-cp313-cp313-win_amd64.whl#sha256=92d0ae66f10ae9dfaaf85d684d6794a3d6ca4ed938dcfaa25e94c54cff3764eb' --index-url https://download.pytorch.org/whl/cu126 --timeout 300 --retries 3
```

可在允许访问官方源的机器下载 wheel 和依赖包，传到本机后核对上述 SHA256，使用本机路径与 `--no-index --find-links` 离线安装；依赖目录必须包含匹配版本，缺包不能视为安装成功。本轮下载传输方式和校验结果保存在独立安装汇总中，不提交 wheel 或缓存。需要足够空间容纳约 2.6 GB wheel 与解压后的 GPU 运行库。

## 3. 固定 MoLFormer 与已审计数据

模型仅使用 `ibm-research/MoLFormer-XL-both-10pct` 修订 `7b12d946c181a37f6012b9dc3b002275de070314`，权重 SHA256 `0795977fe7192c4acdaf052f0e8464af57bc4bb59211271c5e61aaba2637b9c6`。运行时只加载已审计本地源码与 safetensors，不执行 snapshot Python 或新的远程代码。

```powershell
& $taskCudaPy scripts\fetch_pretrained_encoder.py --output models\pretrained\molformer
# 若已有通过校验的模型目录，复用它；不要覆盖或改写其 receipt
& $taskCudaPy scripts\prepare_toxicity_data.py
& $taskCudaPy scripts\prepare_candidate_data.py
& $taskCudaPy scripts\prepare_v3_data.py --output data\v3-development-new
$toxnetAbsentPriorDevelopment = 'data\historical-v3-development-not-distributed'
if (Test-Path -LiteralPath $toxnetAbsentPriorDevelopment) { throw '请使用不存在的历史参考路径，或提供 SHA 匹配的真实历史缓存。' }
& $taskCudaPy scripts\prepare_multitask_data.py --previous-development-dir $toxnetAbsentPriorDevelopment --output data\v3-multitask-new
```

原始缓存已合法获取时准备下载脚本可加 `--offline`。固定预训练离线导入参数见 [TOXNET_V3_TRAINING.md](TOXNET_V3_TRAINING.md)。下载失败、来源缺失、哈希不符或未完成数据审核都会报错，不产生替代分子或标签。本机历史真实数据为 `data/v3-development` 与 `data/v3-multitask-audited`；新 clone 根据自己的新 output 修改以下参数，不把新数据冒充历史版本。

刚生成的四任务目录不能作为固定历史参考。历史 v3 缓存未分发时，不存在的参考路径会如实记录 `unknownStructureReuse=true` 及不可评估的历史结构交集；它不取消 v2 heldout/Karim 隔离。该行为已有 CPU 回归覆盖，不构造假的历史数据。

## 4. 真实 CUDA 工具

需要完整真实 B16 工件；仓库不分发该研究工件、大型预训练权重和标签缓存。已有本机工件可直接使用，缺少时先执行第 5 节训练。

```powershell
$env:CUBLAS_WORKSPACE_CONFIG = ':4096:8'
$env:HF_HUB_OFFLINE = '1'
$env:TRANSFORMERS_OFFLINE = '1'
& $taskCudaPy scripts\verify_v3_cuda.py --encoder-dir models\pretrained\molformer --model-dir artifacts\v3-multitask\baselines-20261013\B16-seed-42 --data-dir data\v3-multitask-audited --output artifacts\integration\cuda-validation-new.json --batch-sizes 1 2 4 8 --threads 6
```

脚本拒绝覆盖结果，退出 0 仅表示全部请求 batch 的真实工程验证通过，退出 2 表示依赖/CUDA 不可用，退出 1 表示失败或部分失败。没有 CPU 推理冒充 GPU 或随机 MoLFormer 回退。

先运行真实 CUDA 矩阵乘法、前向、反向和 AdamW 更新；随机张量仅用于这个算术检查。随后加载校验过的实际 MoLFormer 和 B16，读取同一合法开发划分，对 train 中按固定结构顺序的前八个分子分别执行 batch 1/2/4/8，不根据标签挑选 probe。CPU/GPU embedding 比较预先固定 `atol=rtol=0.001`，返回维度、有限性及实际误差。

B16 的内存副本在真实 train 标签上做一个 masked BCE 训练步，类别权重只由完整 train 计算；无观察任务的监督梯度必须为 0，缺失标签不成为阴性。原编码器冻结且原预测头/磁盘工件不更新，不保存优化器。这只是工程反向验证，不能当作完成训练。

每个 batch 独立测量 synchronized 时间、峰值 allocated/reserved 及增量；预测头训练前卸载 GPU 编码器，区分两种工作负载。OOM 会保留失败状态并跳过没有合法 embedding 的后续阶段，记录真正成功的最大实测 batch，不推算容量。

PyTorch allocated/reserved 不等于整张显卡的总占用，Windows WDDM 和其他应用也占显存。前八个分子的实测不能证明最长 202 token、其它分子、全量微调或生成模型的显存需求。任何 capacity 结论限定于报告的 probe 顺序哈希和工作负载。

## 5. 两组完整冻结训练复现

使用三个固定种子，预先固定参数和合法开发 validation，输出及 embedding cache 必须是独立新路径。Morgan/LR 部分在 CPU，`--device cuda` 让冻结编码器和 MLP 计算在 GPU；不会把逻辑回归称作 CUDA 训练。

```powershell
& $taskCudaPy scripts\train_toxnet_v3.py --data-dir data\v3-development --encoder-dir models\pretrained\molformer --embedding-cache artifacts\integration\fourtask-cuda-cache-new.npz --output artifacts\integration\fourtask-cuda-new --models B0 B1 B2 --seeds 42 43 44 --device cuda --threads 6 --encoder-batch-size 8 --batch-size 64 --epochs 40 --patience 8
& $taskCudaPy scripts\train_multitask.py --data-dir data\v3-multitask-audited --encoder-dir models\pretrained\molformer --embedding-cache artifacts\integration\multitask-cuda-cache-new.npz --output artifacts\integration\multitask-cuda-new --models A4 B16 C_Morgan B16_noTox21 --seeds 42 43 44 --device cuda --threads 6 --encoder-batch-size 8 --batch-size 64 --epochs 40 --patience 8 --minimum-selection-per-class 10 --tox21-weight 0.25
& $taskCudaPy scripts\verify_toxnet_v3_run.py --experiment artifacts\integration\fourtask-cuda-new --data-dir data\v3-development --snapshot models\pretrained\molformer --output artifacts\integration\fourtask-cuda-verified-new.json
& $taskCudaPy scripts\verify_multitask_run.py --experiment artifacts\integration\multitask-cuda-new --data-dir data\v3-multitask-audited --snapshot models\pretrained\molformer --output artifacts\integration\multitask-cuda-verified-new.json
& $taskCudaPy scripts\predict_multitask.py --model artifacts\integration\multitask-cuda-new\B16-seed-42 --encoder models\pretrained\molformer --smiles 'CCO' --allow-research
```

没有完整编码器微调；四任务脚本的 `--fine-tune-encoder` 明确拒绝。GPU 和 CPU 的缓存绑定各自 encoder provenance，不能互相重命名使用。旧 test 和 Karim 不参与优化/选择/校准。四任务和十六任务开发划分不同，两组结果不能直接当同一验证集比较。ClinTox 少样本状态和负迁移仍需独立说明。

## 6. 研究运行与测试

保持原 v2 供应商；显式配置的 v3 只提供独立研究接口。新 PowerShell 终端执行，不把这些设置写入原正式配置：

```powershell
$taskCudaPy = '.\.venv-v3-cuda\Scripts\python.exe'
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts\integration\multitask-cuda-new\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
& $taskCudaPy -m uvicorn backend.main:app --host 127.0.0.1 --port 4183
# 另一终端
Invoke-RestMethod 'http://127.0.0.1:4183/api/local-model/info'
Invoke-RestMethod 'http://127.0.0.1:4183/api/analyze' -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO","provider":"local","mode":"model"}'
Invoke-RestMethod 'http://127.0.0.1:4183/api/research/tox21/status'
Invoke-RestMethod 'http://127.0.0.1:4183/api/research/tox21' -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO"}'
Invoke-RestMethod 'http://127.0.0.1:4183/api/research/explain' -Method Post -ContentType 'application/json' -Body '{"smiles":"CCO","model":"v3","endpoint":"tox21_nr_ar"}'
```

```powershell
& $taskCudaPy scripts\verify_toxnet_integration.py --device cuda --model-dir artifacts\integration\multitask-cuda-new\B16-seed-42 --encoder-dir models\pretrained\molformer --data-dir data\v3-multitask-audited --index-dir data\evidence-index --output artifacts\integration\runtime-cuda-new.json
& $taskCudaPy -m unittest discover -s test -p 'test_*.py'
node --test test/*.test.js
& $taskCudaPy -m pip check
node --check app.js
node --check toxicity-core.js
```

完整离线来源索引准备见 [EXPLANATION_REPORTS.md](EXPLANATION_REPORTS.md)。常规 CPU 工作台仍可用原 `.venv-v3` 在 4173 启动；不要为 GPU 验证覆盖原 `.env` 或强制把 B16 设置成正式四端点模型。

## 7. 本轮实测结果

真实 CUDA 验收 **passed**，PyTorch `2.14.1+cu126`、CUDA 构建 `12.6`、Python `3.13.2`；RTX 4060 Laptop GPU、驱动 `566.24`、8188 MiB。矩阵乘法、前向、反向与优化器更新均检查张量实际位于 CUDA。真实 MoLFormer 输出为 768 维，CPU/GPU 最大绝对差 `1.1444091796875e-05`，低于预先固定的容差。

真实 B16 输出 16 任务，内存副本的缺失掩码、有限梯度和优化器更新均通过；冻结编码器和原加载头的张量未改变，模型/编码器/v2 的磁盘 SHA256 均未改变。1/2/4/8 全部通过且无 OOM，最大**实测** batch 为 8；没有声称更大 batch 或完整微调已验证。

以下为同步后的真实测量，allocated 包括驻留权重和库工作区；reserved 是 PyTorch allocator 预留。预测头阶段已卸载编码器参数，但库工作区可以保留，baseline 与增量都在 JSON 中，不能把这些数字当作纯权重大小或整张显卡占用。首次运行含初始化开销，时间未作稳定性能评测。

| 阶段 | Batch | allocated 峰值 MiB | reserved 峰值 MiB | 秒 | OOM |
|---|---:|---:|---:|---:|---|
| 冻结 MoLFormer 提取 | 1 | 235.544 | 268.000 | 0.111961 | 否 |
| 冻结 MoLFormer 提取 | 2 | 236.125 | 268.000 | 0.032693 | 否 |
| 冻结 MoLFormer 提取 | 4 | 237.286 | 270.000 | 0.028869 | 否 |
| 冻结 MoLFormer 提取 | 8 | 242.147 | 276.000 | 0.051388 | 否 |
| 真实 B16 副本训练步 | 1 | 66.618 | 68.000 | 0.092593 | 否 |
| 真实 B16 副本训练步 | 2 | 66.621 | 68.000 | 0.029358 | 否 |
| 真实 B16 副本训练步 | 4 | 66.626 | 68.000 | 0.022225 | 否 |
| 真实 B16 副本训练步 | 8 | 66.638 | 68.000 | 0.024914 | 否 |

完整来源、诊断、模型/数据/代码哈希和保护工件见 [cuda-validation-20261013.json](artifacts/integration/cuda-validation-20261013.json)。最终两个环境均通过 **326 项 Python**，JavaScript **52 项**，两套 `pip check` 和前端语法检查通过，计数与日志哈希见 [validation-tests-20261013.json](artifacts/integration/validation-tests-20261013.json)。官方安装包校验见 [official-wheel-verification.json](artifacts/integration/official-wheel-verification.json)；其中 `downloadSeconds` 是最后一次续传和组装校验运行的耗时，不是首次下载到完成的总墙钟时间。

两组新冻结实验均已实际完成：B0/B1/B2 × seeds 42/43/44 的 9 个工件，以及 A4/B16/C_Morgan/B16_noTox21 × 三种子的 12 个工件。实际新 B16 CUDA 后端链路也已通过独立运行验收；真实开发指标见下一节。独立盲测、全量编码器微调、概率校准和适用域仍未完成。

## 8. 完整冻结实验的真实复现结果

本轮重新完成第一组 9 个与第二组 12 个工件，其中 3 个零通路损失工件为 `trained-ablation`，不是完整十二通路模型。没有覆盖历史 CPU 工件。冻结 MoLFormer 特征提取分别记录 15.041 秒和 21.103 秒；两组 MLP 实际使用 CUDA，B0/B1/C_Morgan 的 sklearn 优化实际使用 CPU，训练清单已单独记录 `device` 和 `featureExtractionDevice`。这些时长不是稳定性能基准。

各模型均保留三个固定种子，特征变换/类别权重只拟合 train，checkpoint 只用合法开发 validation。完整逐种子、全部端点、标准差、阳性数和参数/文件版本见两组 `experiment.json`；下表只列原四端点的三种子均值。两组开发划分不同，禁止跨表直接当作同一验证集比较。

### 第一组 B0/B1/B2

数据版本 `aidd-toxnet-v3-development-c1d448002df99aa3`；split SHA256 `7701f6c3d8b69a7b1ed918e1365d39e79726414ce2898dca157f7a42134cbc91`。
[完整指标](artifacts/integration/fourtask-cuda-20261013/experiment.json)，[独立工件/指标核验](artifacts/integration/fourtask-cuda-verification-bound-20261013.json)。

| 模型 | 端点 | n / 阳性 | AUROC 均值 | AP 均值 | Brier 均值 | ECE10 均值 |
|---|---|---:|---:|---:|---:|---:|
| B0 | herg | 97 / 67 | 0.748756 | 0.841513 | 0.198686 | 0.202320 |
| B0 | dili | 55 / 26 | 0.842175 | 0.862051 | 0.186422 | 0.160103 |
| B0 | ames | 772 / 475 | 0.764905 | 0.826583 | 0.206911 | 0.125184 |
| B0 | clintox | 176 / 12 | 0.713415 | 0.132158 | 0.066361 | 0.059724 |
| B1 | herg | 97 / 67 | 0.784080 | 0.886792 | 0.255421 | 0.273700 |
| B1 | dili | 55 / 26 | 0.754642 | 0.775224 | 0.317043 | 0.321863 |
| B1 | ames | 772 / 475 | 0.703909 | 0.790231 | 0.258751 | 0.203862 |
| B1 | clintox | 176 / 12 | 0.715447 | 0.288773 | 0.059471 | 0.058842 |
| B2 | herg | 97 / 67 | 0.795522 | 0.892150 | 0.173353 | 0.098952 |
| B2 | dili | 55 / 26 | 0.816092 | 0.822947 | 0.223075 | 0.225915 |
| B2 | ames | 772 / 475 | 0.775888 | 0.833236 | 0.193596 | 0.093689 |
| B2 | clintox | 176 / 12 | 0.732215 | 0.305182 | 0.058749 | 0.042837 |
### 第二组 A4/B16/Morgan/零通路消融

数据版本 `aidd-toxnet-16task-development-aab05727cde8e47b`；split SHA256 `df0dfa7195f714df81e93f3795d1623c81d1d5a32433a59388421cf7d2b082f8`。
[完整指标](artifacts/integration/multitask-cuda-20261013/experiment.json)，[独立工件/指标核验](artifacts/integration/multitask-cuda-verification-20261013.json)。

| 模型 | 端点 | n / 阳性 | AUROC 均值 | AP 均值 | Brier 均值 | ECE10 均值 |
|---|---|---:|---:|---:|---:|---:|
| A4 | herg | 111 / 89 | 0.771876 | 0.905918 | 0.181957 | 0.203102 |
| A4 | dili | 26 / 16 | 0.927083 | 0.961769 | 0.119333 | 0.140383 |
| A4 | ames | 634 / 444 | 0.790442 | 0.887265 | 0.167258 | 0.083759 |
| A4 | clintox | 81 / 4 | 0.863636 | 0.471599 | 0.049170 | 0.073914 |
| B16 | herg | 111 / 89 | 0.779877 | 0.909528 | 0.160381 | 0.162585 |
| B16 | dili | 26 / 16 | 0.929167 | 0.962231 | 0.115835 | 0.147057 |
| B16 | ames | 634 / 444 | 0.791738 | 0.888882 | 0.166815 | 0.083801 |
| B16 | clintox | 81 / 4 | 0.863636 | 0.309444 | 0.059918 | 0.082023 |
| C_Morgan | herg | 111 / 89 | 0.763534 | 0.895367 | 0.144179 | 0.149220 |
| C_Morgan | dili | 26 / 16 | 0.868750 | 0.928061 | 0.147111 | 0.077189 |
| C_Morgan | ames | 634 / 444 | 0.803497 | 0.898918 | 0.182049 | 0.120580 |
| C_Morgan | clintox | 81 / 4 | 0.844156 | 0.221983 | 0.046082 | 0.034848 |
| B16_noTox21 | herg | 111 / 89 | 0.771876 | 0.905918 | 0.181957 | 0.203102 |
| B16_noTox21 | dili | 26 / 16 | 0.927083 | 0.961769 | 0.119333 | 0.140383 |
| B16_noTox21 | ames | 634 / 444 | 0.790442 | 0.887265 | 0.167258 | 0.083759 |
| B16_noTox21 | clintox | 81 / 4 | 0.863636 | 0.471599 | 0.049170 | 0.073914 |

### 新第二组的负迁移与小样本限制

B16−A4 在相同原四任务 validation 标签上的逐种子配对描述性差异如下。AUROC/AP 下降、Brier/ECE 上升是 adverse 方向；没有显著性或因果结论。

| 端点 | ΔAUROC | ΔAP | ΔBrier | ΔECE10 |
|---|---:|---:|---:|---:|
| herg | +0.008001 | +0.003610 | -0.021576 | -0.040517 |
| dili | +0.002083 | +0.000462 | -0.003498 | +0.006674 |
| ames | +0.001296 | +0.001617 | -0.000443 | +0.000043 |
| clintox | -0.000000 | -0.162155 | +0.010747 | +0.008109 |

第二组 ClinTox validation 81 分子仅 4 个阳性，未达到预注册 10/类条件，因此不驱动 checkpoint；新 B16 的 AP 均值相对 A4 下降约 0.162155，Brier/ECE 上升，AUROC 的差值约为浮点零。第一组 ClinTox 是 176 分子/12 阳性，仍属于少样本端点，不能把较高内部指标当独立可靠性。GPU 再训练不保证改善旧端点，也不能把历史 CPU 与本轮 CUDA checkpoint 差异归因于硬件本身。

三种子的零辅助损失消融仍保留独立工件和禁用任务状态；未混合成临床毒性概率。模型全部为 `formalAvailability=false`、未校准且适用域未验证。旧 test 和 Karim 的标签没有用于任何新的拟合/选择/校准。没有新的独立盲测、前瞻评价或全量编码器微调。

实际新 B16 CUDA 运行验收 [runtime-cuda-20261013.json](artifacts/integration/runtime-cuda-20261013.json) 通过：16 个真实任务、12 条独立通路、v2 的真实 CPU 四端点/归因、报告和拒绝路径均核验。原 v2 的 10 个保护文件未改，其中 9 个与 origin/main 字节一致，未公开进度文件依据原保护快照核验。

实际训练源码固定在 Git 提交 `998afbf0a9cea32cb11b7f33377b7d40cff59032`。之后修正了后续工件的分支设备和缓存命中记录，没有改写已生成清单。已生成清单的 `featureExtractionDevice` 记录整组共享编码器设备，不能据此称 Morgan-only 分支使用 GPU；实际头设备 `training.device` 已正确区分 CPU/CUDA。完整不可变源码和字节核验见 [training-source-audit-20261013.json](artifacts/integration/training-source-audit-20261013.json)。

## 9. 使用本机已完成工件

本机已保留 `.venv-v3-cuda` 和 `multitask-cuda-20261013/B16-seed-42`。可直接在项目根目录的新 PowerShell 启动独立研究工作台，保留原 4173 服务：

```powershell
$taskCudaPy = '.\.venv-v3-cuda\Scripts\python.exe'
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts\integration\multitask-cuda-20261013\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
$env:OMP_NUM_THREADS = '6'
$env:MKL_NUM_THREADS = '6'
& $taskCudaPy -m uvicorn backend.main:app --host 127.0.0.1 --port 4183
```

浏览器访问 `http://127.0.0.1:4183/`，四端点本机供应商仍为正式 v2，B16 独立研究接口只在上述显式配置后可用。来源不足和外部语言模型未配置时保留真实本机报告及证据不足状态。

复核本轮已完成工件时指定真实训练提交；该提交只提供审计字节，不执行历史代码：

```powershell
& $taskCudaPy scripts\verify_toxnet_v3_run.py --experiment artifacts\integration\fourtask-cuda-20261013 --data-dir data\v3-development --snapshot models\pretrained\molformer --training-source-ref 998afbf0a9cea32cb11b7f33377b7d40cff59032 --output artifacts\integration\fourtask-local-next.json
& $taskCudaPy scripts\verify_multitask_run.py --experiment artifacts\integration\multitask-cuda-20261013 --data-dir data\v3-multitask-audited --snapshot models\pretrained\molformer --training-source-ref 998afbf0a9cea32cb11b7f33377b7d40cff59032 --output artifacts\integration\multitask-local-next.json
```
