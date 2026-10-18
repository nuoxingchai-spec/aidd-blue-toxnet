# Stage 7B：PubChem 官方 hERG 实验来源核验

证据获取：2026-10-17；报告收尾：2026-10-18（Asia/Shanghai）。仅来源与身份调查，**`STAGE7C_BLOCKED`**。未请求 assay data/活性分类、数值、结果分布或评价接口；未获取候选论文结果/补充表。下列 SID 数是官方 `sid_type=all` 返回的 **AID-associated SID**，不是阳性数量，也不代表已经证明完整原始受测总体。

## 官方合同与获取边界

官方 [PubChem ASN.1 specification](https://ftp.ncbi.nlm.nih.gov/pubchem/specifications/pubchem.asn) 的本轮核查保存了 `PC-AssayDescription` 定义片段，用于识别来源、目标、协议、版本及结果字段定义等元数据；不读取实际实验结果数据，也未固定完整结果对象规范。

有限规范探测回执记录 **102,400 bytes**，SHA256 `2321e17d9229118ad5e6de3e2ee3738a1e7c1a4eb32e426736d6e800deed875b`；完整响应未保留，**不能把它当作整个官方规范文件的 SHA**。本机保存的描述定义片段为 **5,818 bytes**，SHA `a65fd2730e9e0029fbc6165bdf22fc39ef4eda05be51a0eda14f64fde9f19797`，也不是原始完整响应。公开回执分别记录这两项，避免把探测响应与本地片段混作同一工件。

使用官方 `.../assay/aid/{aid}/description/JSON` 在受限内存投影来源标识、目标、版本、文献及预先列举的方法词，不保留全文、评论、结果定义或其他自由叙述；方法词没有数值输出。背景里出现 patch-clamp 不能被解释为实际主平台。`.../sids/JSON?sid_type=all` 只接受 `InformationList.Information[{AID,SID}]`，禁止其他字段、活性选择器或分页/异步结果混入。

官方 PUG REST 网页文档返回浏览器挑战，不能当作已经读取的文档；未绕过。身份接口本轮确实返回可验证的纯 SID envelope，说明该来源身份路径可用；完整实验总体、版本冻结和实际样品含义仍须保管人复核。

## 三个实验

| AID / 版本 | 来源与目标 | 方法、平台与对象 | 与原 hERG 合同的关系 |
|---|---|---|---|
| [588834](https://pubchem.ncbi.nlm.nih.gov/bioassay/588834)，version 2 / revision 1 | NCGC；source ID `HERG01`；标题声明 human hERG；gene 3757 / protein `NP_001191727.1` | 协议词支持 U2OS、荧光、铊通量、FDSS、室温、astemizole 试剂。协议也提及 patch-clamp，不能据词出现将整个 qHTS 认定为直接电流测量 | 针对通道抑制的功能代理读出，值得来源复核；尚不能与原 hERG 二分类合同直接比较；原始测量体系仍待核实 |
| [1671200](https://pubchem.ncbi.nlm.nih.gov/bioassay/1671200)，version 1 / revision 1 | source 名返回字符串 `824`，source ID `HERG915`；gene 3757 / protein `ABQ01243.1`；描述有 Tox21 来源词 | human hERG antagonist、U2OS、荧光、FDSS、室温、astemizole；描述有铊通量词，完整平台仍需原协议确认 | 功能代理读出；Tox21 来源家族风险明显，不宜优先作为新的独立来源。不能把数字 source 名猜成具体机构 |
| [1511](https://pubchem.ncbi.nlm.nih.gov/bioassay/1511)，version 1 / revision 3 | Johns Hopkins Ion Channel Center；`JHICC_hERG`；目标注释 human，tax_id 9606；protein `AAA62473.1` | 标题明确是保护 hERG 免受促心律失常阻断剂影响的 primary screen；协议词有 CHO、荧光/铊、FDSS、室温及 dofetilide | **保护/救援筛选不是直接 hERG 抑制任务**。不能把这类效应直接转换为 blocker 标签；本轮不建议为原任务构建候选 |

“室温”仅是方案词，不是已核验的精确温度。浓度范围、孵育时间、电压程序、重复测量、数据完整性与阳性边界尚未通过独立协议审核。目标物种与宿主细胞分别记录，不用其中一个推断另一个。metadata 内的 assay outcome method 是设计元数据，未用于解释候选活性类别，公开回执也不输出该编码。

## 实际身份覆盖与完整性

| AID | 官方 all-SID 请求 | 已恢复唯一 SID | 原始完整受测总体 / 化合物结构 |
|---|---|---:|---|
| 588834 | HTTP 200；110,127 bytes | **5,381** | 未证明完整原始总体；CID/SMILES 映射未执行 |
| 1671200 | HTTP 200；203,131 bytes | **9,667** | 同上 |
| 1511 | HTTP 200，但流式读取达到预定 5 MiB 上限后停止 | **`null`** | 没有保存部分响应或推算总量；未扩大上限 |

两个完整 SID 投影共享 **8 个 SID**，各自另有 5,373 / 9,659 个 SID。它们只说明精确 SID 关联交叉；不同 SID 仍可能对应相同 CID、盐型或近等价结构，不能据此计算分子独立数或合格数。

首次冻结的身份投影程序采用解析后白名单；为加强重复 JSON key、非有限数和 Content-Length 检查，新增了仓库脚本 `scripts/survey_pubchem_identities.py` 与测试。用新脚本实际重取两个较小列表，原响应 SHA 与首次完全相同；它们不是两个独立实验或评价重复。1511 未重新请求。

| AID | 官方完整 SID 原响应 SHA256 | 新脚本身份投影 SHA256 |
|---|---|---|
| 588834 | `1edd841aef6f6b755fc7e719eb5454971cd040b7f264c49ab42644e7c9d93d38` | `200a044dd8bea8fb088d467775befc288edaf7582b370c2c59a8573a386b61dc` |
| 1671200 | `354f2183b1092033bc6baad8382a38a539852185913390cbe48ffca5f1b619fb` | `1d129b6cc57f47c7bebfba428154683d2fc43879cfb9983a1f30992c33a8d8a9` |

原始完整响应不落盘；只有通过完整白名单验证的 SID 投影进入非 OneDrive 私有目录。公开产物不包含 SID 列表、分子结构或实验行。1511 没有完整文件哈希，保留 `null`，不能为截断文件编造 SHA。

## 文献、提交者与时间

文献身份来自 assay xref 与官方 PubMed 的 citation-only 投影，不读取摘要结果或全文：

- 588834：PMID **19583963**，DOI [10.1016/j.ab.2009.07.003](https://doi.org/10.1016/j.ab.2009.07.003)，发表 2009-11-01；PMID **21362439**，DOI [10.1016/j.taap.2011.02.016](https://doi.org/10.1016/j.taap.2011.02.016)，发表 2011-05-01。
- 1671200：PMID **28551711**，DOI [10.1007/s00204-017-1995-9](https://doi.org/10.1007/s00204-017-1995-9)，发表 2017-12。
- 1511：有多个背景文献及相关 AID，但本轮没有把背景引用当成这批实验的原始研究或另一个独立实验。

允许的 description 身份投影没有取得原始提交时间字段，三者的 **deposition date 均未知**。publication date、版本/revision、获取 UTC、实验实施时间和提交日期不能互相替代。1671200 的原始法律提交者名称仍需解析官方 source 824 身份，本轮没有猜测。

## SID → CID → 结构路径及许可

可交管理员进一步审核的官方路径是 **AID 的 all-SID 列表 → 每个 SID 的 CID 关联 → CID 的结构属性**，并保留原物质记录。PubChem SID 是提交物质记录，CID 是标准化 compound；不是一对一保证。单个 SID 可能存在缺失/多个 CID 或组分，标准化结构可能与原盐/混合物/立体实体不同。

本轮**没有请求 SID→CID 或 SMILES**，因此映射成功、缺失、歧义及结构审核数量均为 `null / 未执行`。上述是有官方身份基础的待审核路径，不是已完成的结构映射。不能用名称/CAS 模糊匹配补足，也不向其他模型外传列表。

PubChem copyright 页面返回浏览器挑战；可访问的 [NCBI 官方政策](https://www.ncbi.nlm.nih.gov/home/about/policies/) 明确 PubChem 等材料可能受第三方版权约束，复制、再分发及商业使用须遵守权利人条件。其本轮响应 SHA：`8ad8f6f186ca51ec73a5fb8935ecfa17b8cbaad300b7025b381898ab72621869`。NIH 托管、免费接口与公开记录不自动授予统一的公共领域或再分发许可；三项具体 assay 的用途许可均保持 `MANUAL_REVIEW_REQUIRED`。

## 历史来源风险与结论

588834 的 2009/2011 文献早于 Wang 2016、Ryu 2020、Karim 2021 汇编，有被纳入的机会；1671200 的 Tox21 来源与项目既有数据家族有关。不得只看 PubChem AID 或较晚提交时间认定独立。已有 161 个文献、237 个 assay 的有限 ChEMBL 来源缓存没发现这三个 AID 字符串，但不包含完整 PubChem 交叉标识，**没有证明任何 AID 与历史实验无重叠**。

本轮恢复的是官方身份入口，尚无 canonical、母体、互变异构、立体/同位素、骨架或历史来源排除结论。结构通过数和完整合格数未知；标签合同、原始样品/批次、文献—实验来源映射、许可及独立保管机制均未完成。最终优先来源见 [资格矩阵](STAGE7B_SOURCE_ELIGIBILITY_MATRIX.md)；**`STAGE7C_BLOCKED`**。
