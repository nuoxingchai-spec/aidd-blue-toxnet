# Compare V1 稳定性与恢复验收（2026-10-14）

本轮在 `feat/aidd-four-model-compare` 上继续完善 [Draft PR #5](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/pull/5)。审查及修改前再次核验远端 head 为 `2d51de2173622f2afafa85c8bd95d875e1af3823`；main 为 `3e0cbe432360b5c9b22aa0c3d538c5b0e23e8a51`。不自动合并、不改历史预测/训练指标、不下载大型模型、不调用收费 API。

## 实际改动

| 文件 | 用途 |
|---|---|
| `backend/compare_adapters.py`, `compare_resources.py`, `compare_txgemma.py` | 跟踪有界清理任务、等待进程/卸载确认、重复取消、隔离；优先提取实际 usage、安全错误计量 |
| `backend/compare_orchestrator.py` | 幂等取消和关闭、分阶段落盘、恢复只读、持久隔离、预设 CRUD、来源与失败计量保留 |
| `backend/compare_persistence.py`（新增） | SQLite 事务、schema 1→2 迁移、最小/显式全文、私有权限、单 owner、中断恢复及维护证明 |
| `backend/compare_logging.py`（新增） | 结构化、字段白名单诊断，不记录模型正文或密钥 |
| `backend/compare_schemas.py`, `compare_worker.py`, `compare_routes.py`, `main.py` | 检索/归因独立开关、旧合同兼容、分页历史、预设 PATCH/DELETE、安全 JSON 错误、实际流式请求大小限制 |
| `compare-app.js`, `compare-core.js`, `compare.html`, `compare.css` | 稳定 DOM/展开/焦点/阅读位置、runId 恢复、历史分页、预设确认、当前所选模型与 Mock 来源、逐次授权消费 |
| `scripts/verify_compare_cleanup.py`（新增） | 显式本机维护：检查进程、loopback Ollama 与 DB owner；不杀进程、不生成、不下载 |
| `scripts/verify_compare_preservation.py`（新增） | 固定历史 main 的 50 文件 SHA256 与原 4 份 Compare 证明校验 |
| `test/test_compare_cleanup.py`, `test_compare_error_metrics.py`, `test_compare_persistence.py`, `test_compare_maintenance.py`, `test_compare_stability.py`（新增） | 生命周期、计量、持久化、维护、中断、HTTP、安全、跨模块回归 |
| `test/test_compare_txgemma.py`, `test/compare-core.test.js` | 原测试扩展 |
| `test/compare-dom.test.js`, `compare-ui-state.test.js`, `compare_stability_fixture.py`（新增） | DOM/真实应用脚本的软件交互验证及显式 Mock 持久浏览器夹具 |
| `.github/workflows/compare-cpu.yml`（新增）, `.gitignore` | 无大型权重/收费服务 CPU CI；排除数据库、缓存、日志及私有内容 |
| `MODEL_API_CONTRACT.md`, `AIDD_COMPARE_WORKBENCH.md`, `README.md`, 本文件 | 合同、Windows 操作、科学限制与当前验收 |
| `artifacts/compare/stability-20261014/*.json` | 本轮新增聚合证明；未覆盖原验收文件 |

## P0 复现与修复

`test_legacy_shield_only_reproduces_false_safe_and_unfinished_cleanup` 使用确定性事件复现旧 shield 等待者受到第二次取消时提前释放资源锁的风险。它是软件负对照，不表示曾观察到真实 GPU 故障。新实现拥有单一清理任务，直到可信退出/卸载确认或有界清理失败后才结束占用；失败进入 quarantine，禁止后续冲突模型。重复 DELETE 复用单一停止操作，服务关闭排空所有适配器，不因某一清理错误跳过其它资源。

测试覆盖二次取消、卸载/取消并发、清理超时/验证失败、全局超时叠加手动取消、服务关闭、第二个任务与 GPU 串行竞争。原子化数据库资源状态在普通重启及 TTL 清理后继续保留；即使旧运行已过期，中断的本机阶段/明确清理失败也能触发隔离。合法维护证明只覆盖其核验时刻之前的旧执行，新异常不能利用旧证明解锁。

SQLite schema 2 使用事务、参数化 SQL、Windows 当前用户+SYSTEM 保护 ACL；单库 owner lock 和显式 `WEB_CONCURRENCY>1` 拒绝不等于跨数据库/跨进程 GPU 调度。默认只保存结构 ID、版本/hash、阶段和实际分数/usage摘要；SMILES、自由问题、实验行、完整快照和正文要求本次 `persistSensitiveContent=true`。本项目在 OneDrive 下，默认数据库位于本机 AppData。该库没有磁盘加密或多用户认证。

重启把未完成阶段标为 `INTERRUPTED`，保留已完成阶段，运行为 failed/partial；不自动重算、不重新外传。历史授权/尝试可以作为审计标记保留，恢复请求授权为 false。数据库写失败不能作为安全清理证明；隔离未成功持久化时不会继续覆盖为终态记录。

DeepSeek 的 HTTP 响应、usage、报告 JSON、事实核验分别记录。测试使用 `httpx.MockTransport`，包含非法内容 JSON但真实返回计量、合法 JSON 但错误事实 ID、429、500、截断、超时、拒绝/空内容、null/负数/不可信计量。合法字段保留，无效字段 null；失败报告不发布。无账单/可靠费率时 cost 为 null/unknown，不能说失败费用为零。没有发生真实 DeepSeek 调用。

## 实际验证结果

| 验证 | 结果 |
|---|---|
| CPU 环境全仓 Python unittest | 504 项运行；503 通过，1 项 POSIX 权限检查在 Windows 跳过 |
| CUDA 环境全仓 Python unittest | 504 项运行；503 通过，同 1 项跳过；软件套件通过不代表所有生成模型已部署 |
| JavaScript npm test | 92/92 通过（含稳定 DOM 与实际 compare-app 脚本夹具） |
| 两套 pip check、4 个 JS 语法、git diff --check | 均通过 |
| 真实 CUDA B16 HTTP | 16 任务正常，等价结构一致，Tox21 展示开/关正常；原 v2 四端点 HTTP 正常 |
| 历史保护 | 50 个原 main 历史文件严格 SHA256 一致，原 4 个 Compare 证明 Git 内容未改；Windows Git JSON 换行单独记录，不改科学工件字节 |
| B16 本机工件 | 真实复验前后所有该模型目录文件 hash 未变 |
| 浏览器 Mock | 报告等待期间展开/焦点/scrollY 保持；刷新仅恢复 GET、授权关闭；服务重启后全文/摘要分别恢复且模型调用数为 0；历史第 1/2 页；预设创建/409/重命名；独立检索/归因、旧结果失效、失败/取消保留预测、实际 JSON 下载 |
| 确认取消预设操作/BFCache/迟到响应 | 实际应用脚本+VM 软件测试；不冒充人工浏览器弹窗行为或真实模型验证 |
| GitHub Actions | 代码提交 `8b85370` 的 Ubuntu CPU [运行 38441502963](https://github.com/nuoxingchai-spec/aidd-blue-toxnet/actions/runs/38441502963) 实际成功；Python 504 项运行、501 通过、3 项 Windows 专属测试跳过；JS 92/92、依赖/语法/历史工件复核均通过 |

完整聚合记录：`artifacts/compare/stability-20261014/{validation,runtime-verification,browser-verification,artifact-preservation}.json`。原 `artifacts/compare/*.json` 保留。

真实浏览器也通过已更新的 4185 工作台完成 B16，四端点与十二通路实际显示；Qwen 明确不可用，未授权的 DeepSeek 和未选择的 TxGemma 被跳过。该次 B16 阶段约 7.746 秒（含加载），实测显存范围与 HTTP 探针一致。截图仅保存为本机验收附件，不提交分子导出或截图至 GitHub。安全恢复脚本实际只读核验成功，`quarantineCleared=false`，没有执行真实解锁。

真实 B16 复验模型为 `aidd-toxnet-multitask-b16-42-2229faa18f2d`，weights SHA256 为 `2229faa18f2dd3240b726efaf2653b163f9b867fb6844aea9f71ab4b6fb4e3b3`。本次两次探针预测约 0.286–0.515 秒，含 worker 启动及模型加载约 9.75–11.29 秒。PyTorch peak allocated/reserved 为 187,969,024 / 213,909,504 bytes，限于本次 B16 进程；不是整板显存、吞吐基准、归因峰值或其他模型容量证明。

## 本机启动、模型准备与复现

Ollama 本机服务已可达，但核验时 `/api/tags` 为空。官方标签页面确认 `qwen3.5:4b` 存在。需要用户下载一次模型，不需要 API Key。本轮没有执行 pull：

```powershell
$ollamaExe = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama.exe'
& $ollamaExe pull qwen3.5:4b
& $ollamaExe list
```

下载完成后在 Compare 点“刷新模型”，选择实际 Qwen ID。首次只运行 B16→本机 Qwen，保持 DeepSeek 关闭、TxGemma 跳过。模型安装与受控报告通过核验是两个独立状态；不得提前说报告已生成。

```powershell
$comparePython = (Resolve-Path '.venv-v3-cuda\Scripts\python.exe').Path
& $comparePython -c "import torch; print(torch.__version__, torch.cuda.is_available())"
$env:TOXNET_MULTITASK_ENABLE = 'true'
$env:TOXNET_MULTITASK_MODEL_DIR = (Resolve-Path 'artifacts/integration/multitask-cuda-20261013/B16-seed-42').Path
$env:TOXNET_MULTITASK_ENCODER_DIR = (Resolve-Path 'models/pretrained/molformer').Path
$env:TOXNET_MULTITASK_DEVICE = 'cuda'
$env:TOXNET_COMPARE_PYTHON = $comparePython
# 若4185已有本项目服务，请使用该服务；不要重复占用同一数据库。
& $comparePython -m uvicorn backend.main:app --host 127.0.0.1 --port 4185 --workers 1
# 页面：http://127.0.0.1:4185/compare
```

```powershell
& '.\.venv-v3\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -v
& '.\.venv-v3-cuda\Scripts\python.exe' -m unittest discover -s test -p 'test_*.py' -v
npm test
& '.\.venv-v3\Scripts\python.exe' -m pip check
& '.\.venv-v3-cuda\Scripts\python.exe' -m pip check
& '.\.venv-v3\Scripts\python.exe' scripts/verify_compare_preservation.py
& $comparePython scripts/verify_compare_runtime.py `
  --model-dir artifacts/integration/multitask-cuda-20261013/B16-seed-42 `
  --encoder-dir models/pretrained/molformer --device cuda `
  --output artifacts/compare/local-runtime-new.json
# 用新路径输出，避免覆盖历史验收。
```

持久 Mock 浏览器测试可使用 `python -m test.compare_stability_fixture --directory <非同步临时目录> --port 4186`。该页面明确标注 Mock；合成分数、类别、报告只能证明软件流程。安全解除 quarantine 的两步维护命令见 `AIDD_COMPARE_WORKBENCH.md`。

## 尚未完成与科学限制

- Qwen 未安装模型，未生成真实报告，其显存/token/s/通过率未测量。
- TxGemma 未获本机许可授权、未安装权重；审核任务仍仅 `tdc-bbb-martins`，A/B 类别不自动变成概率，不是四毒性端点直接对照。
- DeepSeek 账户访问、真实报告和账单未验收；不能把 Mock usage 当作真实账单。
- 浏览器部分模拟流程、真实 B16 推理、Linux CPU CI、三种生成模型的部署验收分别报告。
- v3 仍 `formalAvailability=false`；旧 test/Karim 已曝光、ClinTox 少样本/可能负迁移、未独立校准/新盲测/可信适用域；归因不是因果机制，自然语言整理不是科学验证。
- 不将工作库、导出分子、模型/权重缓存、虚拟环境、密钥或受限数据库提交 GitHub。

本輪最新提交 SHA 与实际 CI 结果由 PR #5 及最后交付消息给出。main 保持原状态。
