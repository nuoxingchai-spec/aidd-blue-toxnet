# Qwen 与 TxGemma 本机接入验收（2026-10-14）

本轮在 `feat/aidd-four-model-compare` 继续开发，保持 [Draft PR #5](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/5)，不直接修改或合并 main。用户已安装两份模型并明确确认 TxGemma 许可授权完成；没有代替用户接受协议、重新下载模型或调用收费 API。

## 实际接入

Qwen 使用 Ollama `/api/chat`。首次真实报告返回输入3828、输出268 Token并截断，合计达到原4096上下文边界；合法计量保留，报告没有发布。默认上下文显式设置为8192、输出上限4096后，完整报告通过事实 ID、分数、来源与快照 hash 的确定性核验。没有降低核验条件或改写分数。

TxGemma 增加独立 `ollama-gguf` Adapter，与原 `local-transformers` 路径并存，默认仍为原路径。仅支持原已审核的 `BBB_Martins`，调用 `/api/generate`、`raw=true` 和官方任务模板，避免聊天模板改写；不使用强制 A/B 语法把异常输出伪装成类别。输出严格解析，自然结束、来源及清理核验后才发布，概率为 null。BBB 不是 hERG/DILI/Ames/ClinTox 的直接对照。

| 模型 | 实际包与来源 |
|---|---|
| Qwen | `qwen3.5:4b`，Q4_K_M，Ollama digest `d8b0f5e9760cd1682034f292d7ef72ec46f432149be0df7574bf2d6e92e38c04` |
| TxGemma | `txgemma-2b:latest`，Q4_K_M，Ollama manifest digest `b6578da0f91e0974123bf1b9ba924349ef901c897143784fd746faaa1d1ba094` |
| 量化仓库 | `matrixportalx/txgemma-2b-predict-GGUF`，固定修订 `e93f94fc209e1b5971fa96582f346fc7a4d7bec9` |
| GGUF 文件 | `txgemma-2b-predict-q4_k_m.gguf`，1,708,582,112 bytes，SHA256 `d2631215ae8be65343b8a22820e2551947aee1d0cf00e9dad872f610a3cf2ace` |

本机实际文件哈希与固定修订的 HF LFS 元数据一致。源码模型卡声明来自 `google/txgemma-2b-predict`、经 llama.cpp/转换 Space 生成，但没有公开上游 Google commit 或转换工具版本。因此明确标为 `third-party-quantized`、`upstreamRevisionStatus=unknown-not-published-by-quantizer`，不等同于原已审核的官方 safetensors 修订。许可状态是用户的完成声明，不是独立法律认证。

审核支持旧 `manifests/registry.ollama.ai` 与 Ollama 0.40 的 `manifests-v2/ollama.com` 两个固定布局；模型根目录只能来自服务端配置。不会根据 `/api/show` 返回的任意本机路径读取文件。模型及config均验证字节，运行前后重新核验；额外权重/adapter层、含糊manifest、来源改变、非法输入、未知任务或异常 A/B 输出拒绝运行/发布。普通相同用户进程的恶意瞬时替换不在此文件审核的防护范围，需要维护受信本机模型库。

## 真实功能验收

首个完整本机比较任务实际状态为 completed：

| 阶段 | 结果 | 阶段耗时 | 供应商返回 Token |
|---|---|---:|---:|
| B16 | 真实16任务计算完成 | 7.696秒 | 不适用 |
| TxGemma | BBB 类别 B，概率 null，严格模板及来源核验通过 | 6.998秒 | 输入112 / 输出4 |
| Qwen | 完整结构报告及事实核验通过 | 52.808秒 | 输入4317 / 输出3070 |
| DeepSeek | 未授权、已跳过；没有请求 | 未执行 | 未测量 |

上述耗时包含加载/审计/清理。该次Qwen纯生成速率约66.08 Token/s；TxGemma仅4个输出Token，其短调用速率不能当作稳定吞吐基准。Qwen与TxGemma的峰值显存、整板占用和科学准确率未测量。两个模型阶段均确认卸载，三个本机模型按顺序运行；不声称管理其他应用的GPU。

源代码回归、最终浏览器复验与CI以新聚合证明 `artifacts/compare/ollama-deployment-20261014.json` 及 PR checks 为准。没有覆盖旧稳定性验收、模型权重、评估或划分文件。

### 最终源代码回归与浏览器复验

CPU、CUDA两套Python环境分别实际运行544项：542通过、2跳过、0失败。跳过项分别为POSIX权限模式（Windows使用ACL）和Windows无创建符号链接权限。CUDA环境的544项仍是软件回归，不是544次GPU模型推理。JavaScript实际95/95通过；两套`pip check`、JavaScript语法及`git diff --check`通过。50份历史工件SHA256一致，原4份Compare验收记录内容未变。最新Adapter对实际安装包的只读来源、额外层和Tokenizer审核也已通过，没有启动模型生成。

真实浏览器任务再次completed：B16阶段7.327秒、TxGemma5.153秒、Qwen54.336秒。Qwen输入4317/输出3070 Token，完整报告通过核验；DeepSeek未请求。前端已加载最新来源展示代码，验证了第三方/未知上游详情、中文报告、真实JSON下载、导出分数和快照哈希一致、修改输入后旧记录和导出失效、刷新只读恢复同一任务且没有新增模型执行。两个真实任务均仅默认保存摘要；下载全文留在本机，没有提交报告正文或查询结构。截图留在本机`artifacts/ollama-deployment-ui.jpg`。

**后端重新加载尚未完成。** 自动审批分别拦截了停止现有服务和启动独立验收服务，返回信息仅为策略阻止，没有更具体原因。因此保留现有4185服务；以上真实浏览器运行来自收紧前已加载的三模型后端。最新额外层、Tokenizer与未知runtime门禁已通过源代码测试及本机只读包审核，尚不能标记为已重新部署。操作者正常停止4185服务后，使用下方完整环境命令重启，才能加载这些最终收紧。GitHub CI是否通过以当前PR实际checks为准，本机测试不能代替CI。

## 配置与使用

本机已配置 `.env`（未提交）。其他机器需先自行安装且核对模型许可与来源；以下包digest为本机示例，不能将不同包盲目设置成相同digest：

```dotenv
TOXNET_OLLAMA_URL=http://127.0.0.1:11434
TOXNET_OLLAMA_MODEL=qwen3.5:4b
TOXNET_OLLAMA_CONTEXT_TOKENS=8192
TOXNET_OLLAMA_OUTPUT_TOKENS=4096
AIDD_TXGEMMA_RUNTIME=ollama
AIDD_TXGEMMA_ENABLE=true
AIDD_TXGEMMA_LICENSE_ACCEPTED=true
AIDD_TXGEMMA_OLLAMA_MODEL=txgemma-2b:latest
AIDD_TXGEMMA_OLLAMA_DIGEST=b6578da0f91e0974123bf1b9ba924349ef901c897143784fd746faaa1d1ba094
```

`OLLAMA_MODELS` 可由服务端配置可信本机模型库，默认用户目录 `.ollama/models`。不接受浏览器传模型路径、版本、生成工具参数或凭据。Qwen上下文允许4096–16384、输出256–8192且输出必须小于上下文；配置值不是显存容量证明。

在 [工作台](http://127.0.0.1:4185/compare) 点“刷新模型”，选择 `BBB_Martins` 和实际 `qwen3.5:4b`，保持 DeepSeek 关闭后运行。报告来源详情区分第三方量化仓库修订、本机manifest hash与未知上游修订。新输入或配置会使旧结果失效。

```powershell
$comparePython = (Resolve-Path '.venv-v3-cuda\Scripts\python.exe').Path
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts\integration\multitask-cuda-20261013\B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models\pretrained\molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
$env:TOXNET_COMPARE_PYTHON = $comparePython
& $comparePython -m uvicorn backend.main:app --host 127.0.0.1 --port 4185 --workers 1
# 已运行时使用原服务；修改运行时配置后正常停止，再单worker重启。
& '.\.venv-v3\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -v
npm test
& '.\.venv-v3\Scripts\python.exe' scripts/verify_compare_preservation.py
```

修改文件为独立GGUF Adapter、运行时路由、Qwen生成预算、来源合同和持久化字段、前端详情、环境示例、相关测试与文档。全部 v3 保持 `formalAvailability=false`；安装/功能验收不等于模型科学性能提高或临床验证。

本轮文件清单：

- 后端：`backend/compare_ollama_txgemma.py`、`compare_adapters.py`、`compare_routes.py`、`compare_reporting.py`、`compare_persistence.py`。
- 前端：`compare-app.js`。
- 回归：`test/test_compare_ollama_txgemma.py`、`test_compare_ollama_provenance.py`、`test_compare_runtime_selection.py`、`test_ollama_generation_config.py`、`compare-ui-state.test.js`。
- 文档与配置：本文件、`README.md`、`AIDD_COMPARE_WORKBENCH.md`、`MODEL_API_CONTRACT.md`、`.env.example`、`.gitignore`。
- 聚合验收：`artifacts/compare/ollama-deployment-20261014.json`。
