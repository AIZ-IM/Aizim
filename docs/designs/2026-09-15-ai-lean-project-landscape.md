# AI + Lean 项目调研与 Aizim 的下一步

调研日期：2026-09-15。Aizim 基线：`62b52bc`。

依据：项目官方仓库、作者文档、选定源码及 Aizim 当前实现。下文区分上游已有能力和
Aizim 的建议改动；本轮完成的是静态调研，没有安装、运行或性能对测这些候选系统。

## 结论

最接近整套研究工作流的新增参考是 **Numina-Lean-Agent**。最适合近期吸收的具体能力是：

1. **可靠的数学检索**：jixia 的声明提取，LeanExplore 的本地混合检索，LeanSearch v2 的多轮引理检索。
2. **可审阅的研究页面**：leanblueprint 的数学蓝图，Verso 的文档与 Lean 连接，Paperproof 的证明状态展示。
3. **可复用的证明交付**：Numina 的逻辑整理阶段，lean-eval 的题面比较、独立复核与评测方法。

产品判断：Aizim 适合发展为让研究者提出问题、比较路线、理解结果、整理知识的工作空间。
AI 执行搜索与形式化，人的判断通过问题定义、路线选择、语义审阅和解释成为可追溯的研究成果。

## 当前代码基线

| Aizim 已有能力 | 下一步可以补齐什么 | 本地入口 |
| --- | --- | --- |
| 不可变目标、依赖调度、有界修复、恢复 | 证明后的整理阶段；方案和目标修订之间的关系 | [engine.py](../../src/aizim/research/engine.py)、[records.py](../../src/aizim/research/records.py) |
| 源码扫描、BM25、名称与类型文本搜索 | 编译器提取的完整声明信息、持久索引、语义召回与重排 | [search.py](../../src/aizim/research/search.py) |
| 指导收件箱、贡献记录、语义与研究价值审阅 | 便于研究者操作的界面、解释与具体证明步骤的关联 | [records.py](../../src/aizim/research/records.py) |
| 本地只读看板、JSON/HTML 导出 | 论文式阅读、声明导航、假设变化和审阅对应关系 | [dashboard.py](../../src/aizim/research/dashboard.py)、[report.py](../../src/aizim/research/report.py) |
| 受控 promotion、构建、类型与公理证据 | 可信题面与提交环境比较、独立内核复核证据 | [lean_verifier.py](../../src/aizim/knowledge/lean_verifier.py) |
| 多个证明 Worker、一个共享 Lean runtime | 基于真实等待时间数据决定是否引入多个运行实例 | [resources.py](../../src/aizim/orchestration/resources.py)、[runtime.py](../../src/aizim/lean/runtime.py) |

这里的检索差距有明确代码依据：`declarations()` 用正则识别源码，`type` 模式匹配签名文本，
每次检索重新构建候选列表。这适合作为初始检索入口；后续需要正确处理完整命名空间、
编译后类型、导入可见性与库版本。

## 1. Numina-Lean-Agent：研究编排与证明整理

[项目](https://github.com/project-numina/numina-lean-agent) 提供基于 Claude Code 的形式化工作流，
包含研究协调、非形式化推理、证明及逻辑整理等角色；公开了蓝图模板、逐轮输出及验证脚本。

最值得吸收的是完成证明后的 **逻辑整理**：上游 golfer 明确关注消除绕路、冗余中间结果和
复杂结构，保留可读性。Aizim 可以为已验证成果启动独立的整理任务，输出候选新版本、
变化说明、构建成本和复核结果，保留原始已验证版本及其证据。

蓝图中的数学陈述、非形式化论证、Lean 声明和依赖关系也值得采用。Aizim 可以把这些内容
存进现有事件系统，再生成文档，让蓝图和实际执行状态保持一致。

源码入口：

- [蓝图模板](https://github.com/project-numina/numina-lean-agent/blob/1c9af8a52e715f22fede766425ba3d3b95526132/prompts/BLUEPRINT_template.md)
- [逻辑整理角色](https://github.com/project-numina/numina-lean-agent/blob/1c9af8a52e715f22fede766425ba3d3b95526132/prompts/autosearch/subagent_prompts/golfer.md)
- [原题快照与 SafeVerify 接口](https://github.com/project-numina/numina-lean-agent/blob/1c9af8a52e715f22fede766425ba3d3b95526132/scripts/safe_verify.py)

## 2. jixia + LeanSearch v2：北大团队中值得继续吸收的基础设施

[jixia](https://github.com/frenzymath/jixia) 属于北大 BICMR 的 AI for math 项目，能够提取声明、
完成 elaboration 后的符号类型与引用图、语法树、tactic 信息及逐行证明状态。
它适合作为 Aizim 声明索引的候选提取器。

[LeanSearch v2](https://github.com/frenzymath/LeanSearch-v2) 使用 jixia 构建语料，提供向量检索与
重排，以及“拆分问题—检索—筛选—判断是否充分”的多轮模式。其依赖顺序驱动的自然语言解释
生成也有价值：解释一个新定义时，可以携带依赖定义的已生成说明。

建议分两步吸收：先提取准确的声明和依赖；随后为困难任务增加有预算上限的多轮检索。
索引应绑定 Lean 版本、Lake 依赖、项目 revision 和内容哈希，返回的引理必须在本次环境里可用。

集成条件：上游完整本地服务要求 embedding 和 reranker 各使用一张 GPU；公开服务使用的
reranker 配置也与论文实验不同。部署方案应按 Aizim 的机器资源和自己的评测结果选择。
这两个项目都具有 Apache-2.0 代码许可；LeanSearch 的部分评测数据另有许可。
见 [构建、部署与数据说明](https://github.com/frenzymath/LeanSearch-v2/blob/94f4888cbaf9f4322535755f86cbac690ec18080/README.md)。

## 3. LeanExplore：优先验证的本地检索方案

[LeanExplore](https://github.com/justincasher/lean-explore) 当前的本地后端组合了名称 BM25、
自然语言说明的向量检索、排序融合、依赖加权和重排。下载数据与模型后可以本地查询，GPU 可选。
其文档提供了源码提取流水线，因此能进一步评估为用户项目建立索引的路径。

两个具体经验适合 Aizim：

- 检索先返回简短结果，再按需读取类型、源码、解释与来源，降低上下文开销。
- 区分公共库索引和当前项目索引，针对同一环境组合检索结果。

建议给现有 `lean_search()` 引入适配层，先保留轻量文本检索，再比较本地混合检索效果。
缓存键包含环境和检索配置；研究中的私有查询采用本地处理，远程服务作为明确选择的后端。

源码与文档：[本地后端](https://github.com/justincasher/lean-explore/blob/6b25f8632cc3387cf85f7730375a690a1f1dfb79/docs/local-backend.md)、
[按需读取接口](https://github.com/justincasher/lean-explore/blob/6b25f8632cc3387cf85f7730375a690a1f1dfb79/docs/mcp-server.md)、
[提取流水线](https://github.com/justincasher/lean-explore/blob/6b25f8632cc3387cf85f7730375a690a1f1dfb79/docs/extraction-pipeline.md)。

## 4. leanblueprint + Verso：把成果做成可阅读的数学

[leanblueprint](https://github.com/PatrickMassot/leanblueprint) 把数学叙述、Lean 声明链接、依赖图和
形式化进展组织成蓝图；`checkdecls` 可以检查文中引用的声明是否存在。
[Verso](https://github.com/leanprover/verso) 提供 Lean 内的文档语言、代码展示和交叉引用。

Aizim 可以增加“研究页面”：每个目标都有非形式化陈述、定义选择、证明草图、关键引理、
对应 Lean 声明和人类解释。读者从结论一路点到某个引理及其理由。
先支持蓝图导入与导出，再评估 Verso 输出格式。

这些页面应分别展示编译验证、语义审阅和解释完成情况。蓝图手写的完成标记只是文档状态；
实际验证状态继续来自 Aizim 的可信发布证据。
参见 [蓝图声明与状态处理](https://github.com/PatrickMassot/leanblueprint/blob/56e066d30fb7b608a63f4241fee80982d7eae3ef/leanblueprint/Packages/blueprint.py)。

## 5. LeanCopilot + Paperproof：让研究者能介入局部证明

[LeanCopilot](https://github.com/lean-dojo/LeanCopilot) 提供 tactic 建议、引理选择、结合 aesop 的
多步搜索，以及点击插入候选证明的交互。
[Paperproof](https://github.com/Paper-Proof/paperproof) 根据证明中目标与假设的变化绘制证明树；
其 README 说明当前展示支持 tactic 风格的证明。

适合吸收的产品形式是：研究者选中一个节点，查看当前目标、假设和候选路线；可以添加引理、
提出约束、修改思路或发起局部重试。证明树与定理依赖图表示不同层次，应分别展示。

Aizim 已有指导 RPC 和材料化 Worker 视图，可用于接收这些操作；当前 HTTP 看板只提供读取，
需要单独设计受控的编辑通道和版本冲突处理。上游交互候选同样应经过既有验证与发布路径。
参见 [LeanCopilot 前端](https://github.com/lean-dojo/LeanCopilot/blob/84e433ee2c1b71c70a470258c743632ba62245a9/LeanCopilot/Frontend.lean)、
[Paperproof 的证明信息提取](https://github.com/Paper-Proof/paperproof/blob/05c68e9cb8e881f9e971abb94efd231a269e0229/lean/Services/BetterParser.lean)。

## 6. lean-eval：验证原题、独立复核、衡量进步

[Lean 官方 lean-eval](https://github.com/leanprover/lean-eval) 将可信题面、求解者提交及连接两者的
固定声明分开。comparator 比较题面相关的常量图，核对允许的公理，并回放验证；调研所见的
工作空间入口还强制启用 nanoda 独立内核。

Aizim 已经有不可变目标及受控验证。可进一步借鉴的内容是：保存原题环境，比较提交是否
改变相关定义，记录独立复核状态，并形成固定研究样本集。这些检查确认形式对象，数学含义
仍需结合原问题与研究者审阅。

建议建立首批 30–50 个固定样本，覆盖项目中真实的引理检索、分解、修复和整理任务。
比较首次成功率、预算内成功率、token、墙钟时间、Lean 等待时间，以及人为改变题面后的拒绝结果。
引理复用和语义审阅结果单独记录，以免用一个成功率掩盖所有差别。

源码入口：[比较模型](https://github.com/leanprover/lean-eval/blob/dd42cf4ae36899031a9c40768adac775734d11ed/SECURITY.md)、
[强制独立复核入口](https://github.com/leanprover/lean-eval/blob/dd42cf4ae36899031a9c40768adac775734d11ed/generated/two_plus_two/WorkspaceTest.lean)。

## 7. Kimina + Pantograph：有性能证据后再扩展执行层

[Kimina Lean Server](https://github.com/project-numina/kimina-lean-server) 提供并行验证服务。
其运行实例管理器按导入头复用 REPL，支持预热、数量限制、等待超时及回收。
[Pantograph](https://github.com/stanford-centaur/PyPantograph) 提供程序化 tactic 执行、证明搜索和
相互关联的未确定变量处理。

这些经验适合解决两个不同问题：Kimina 对应多个验证作业的吞吐量；Pantograph 对应一个证明
内部的状态搜索。Aizim 可以先测量当前共享 runtime 的等待时间，再按环境与导入集合划分
独立运行实例，保留现有单一状态写入与发布授权机制。

证明子目标可能共享尚未确定的变量。设计分支调度时应显式维护这种联系。
参见 [Kimina 管理器](https://github.com/project-numina/kimina-lean-server/blob/fb2393de3461db35eda4c714e3fd21187e92ec90/server/manager.py)、
[Pantograph 搜索](https://github.com/stanford-centaur/PyPantograph/blob/f8aee320ee5550ea2677e414534618a61e7e1497/pantograph/search.py)。

## 其他候选

- [LeanAgent](https://github.com/lean-dojo/LeanAgent)：跨仓库知识数据库、依赖有序的数据提取及渐进训练。
  值得借鉴可追踪来源的知识积累；其模型训练路线需要单独的数据与计算投入。
- [Aristotle](https://aristotle.harmonic.fun/)：官方产品支持从自然语言或已有 Lean 项目开展证明，
  可作为远程求解后端的调研对象；本轮没有获得其核心实现或验证其服务效果。
- [Leanstral 1.5](https://docs.mistral.ai/models/leanstral-1-5)：Mistral 的 Lean 证明工程模型，
  官方文档标注为 Public Preview。可用同一批 Aizim 任务评估质量与成本。

后端接入需要显式实现适配、取消、超时和证据接收。Aizim 当前的 Worker 固定为 Codex；
这些候选的存在不代表当前已有对应 Worker 支持，也不足以判断哪个在本项目上更强。

## 如何让人的用途成为具体能力

建议在研究页面上提供四种能够改变研究进程的操作：

| 人的判断 | 界面与记录 | 后续行为 |
| --- | --- | --- |
| 这个问题及定义是否表达研究意图 | 对照自然语言、Lean 类型、关键假设与例子 | 修改问题时创建关联的新目标版本，保留原有证据 |
| 哪条证明路线值得探索 | 比较草图、关键引理、反例及已失败路线 | 选择路线、调整预算、添加约束或新子目标 |
| 哪个结果值得沉淀进数学库 | 比较一般性、接口、依赖、说明和可读性 | 启动整理任务，形成可审阅的库贡献候选 |
| 如何让同行理解并使用结果 | 编写解释，关联具体声明和步骤，记录来源与贡献 | 生成可导航的研究文档和选定成果包 |

例如，AI 为了完成证明而提出“增加一个紧性假设”，研究者应能看到这一变化，判断它是否
保留原问题价值，并选择新目标或其他路线。语义审阅应围绕这些具体变化开展。

人的判断也可以得到 AI 协助；记录应描述实际贡献。贡献、形式有效性、原创性和优先权
继续分别表达，详见 [现有人类角色设计](2026-09-15-human-role-in-lean-native-research.md)。

## 建议实施顺序与验收

| 顺序 | 可交付改动 | 验收依据 |
| --- | --- | --- |
| 1 | 编译器提取的声明索引 + 检索适配层 + 固定查询集 | 完整名称、多行类型与导入可见性正确；环境变化使旧索引失效；比较召回和查询成本 |
| 2 | 研究页面 + 蓝图关联 + 可定位的语义审阅 | 陈述、假设、证明和解释可互相定位；审阅绑定具体版本；文档状态与形式验证状态分别显示 |
| 3 | 证明整理任务 + 原题比较 + 独立复核试点 | 原始版本保留；新版本重新验证；改变相关定义、增加不允许公理等样本被拒绝 |
| 4 | 局部证明交互、运行实例池、其他求解后端 | 用同一固定样本比较成功率、总预算、等待时间、可读性及使用体验 |

## 源码快照与使用方式

下列为本轮读取的默认分支 HEAD，供复查具体实现；引用上游代码时应按对应文件许可处理。
Lean 工具的源码版本还需与 Aizim 的 Lean/Lake 环境做兼容验证。

| 仓库 | 快照 | 代码许可信息 |
| --- | --- | --- |
| Numina-Lean-Agent | `1c9af8a` | README 声明 MIT；此次树中未见独立根 LICENSE 文件 |
| jixia | `755fde2` | Apache-2.0 |
| LeanSearch-v2 | `94f4888` | Apache-2.0；部分数据另有许可 |
| LeanExplore | `6b25f86` | Apache-2.0 |
| leanblueprint | `56e066d` | Apache-2.0 |
| LeanCopilot | `84e433e` | MIT |
| Paperproof | `05c68e9` | MIT |
| lean-eval | `dd42cf4` | Apache-2.0 |
| Kimina Lean Server | `fb2393d` | MIT |
| PyPantograph | `f8aee32` | Apache-2.0 |

仓库可见性、默认分支的新旧和作者性能报告是筛选信息；以上建议的优先级来自与 Aizim 当前
代码的对照。本轮没有测得跨项目的性能排名。
