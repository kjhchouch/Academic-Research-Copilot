# Grounding（claim→span→蕴含校验）+ 引用评测 · 设计

- 日期：2026-10-31（D9）
- 状态：已实现（自治模式下按本文件落地；每条关键决策都给了被否掉的备选）
- 范围：Fast Path 答案的**引用级标注**（supported / uncertain / contradicted / unbacked），
  以及评测 harness 中 **Citation Precision / Recall** 两个 README 承诺指标的落地。
- 非目标：Deep Path 的 Verifier→Drafter 诊断回退（那是另一张图，见 builder.py 的「三个刻意缺席」）；
  PDF 页级 span（`SpanRef` 契约不变，仍然可空）；Jev 蕴含后端（等 `TYPESAFE_API_KEY`，与 D4 同款在线验收项）。

## 1. 问题

README 承诺「所有输出强制绑定真实文献 span，未获支持的引用会被标记」。D6 落地了前半句的
编号层（citation_id 由网关分配，模型抄号——grounding by construction），但后半句不存在：
一个 `[7]` 只要有真实论文对应就渲染成正常 chip，至于**这句话是否被那篇论文支撑**，没有任何检查。

本设计补的是**标注层**，不是**拦截层**：低置信的引用被标记出来给用户看，不被删除、不触发重试
（重试属于 Deep Path Verifier）。

## 2. 分层：L0 确定性 + L1 模型

沿用 routing 已经论证过的「能确定性的绝不概率化」：

- **L0 `unbacked`（确定性，零模型调用）**：正文里的 `[n]` 在 `CitationRegistry` 里没有对应条目
  ——模型编造的引用号。这是最强的失败信号，且**不需要任何模型**：registry 是网关分配的，
  查表即可。`verify_answer` 在 `backend=None` 时也标注这一类，所以 `GROUNDING_BACKEND=off`
  不是「什么都没有」，而是退到只剩确定性标注。
- **L1 `supported` / `uncertain` / `contradicted`（模型蕴含判断）**：claim = 含 `[n]` 的句段，
  span = 该引用论文的 `abstract`（检索真返回的文本）。一个批量调用判完所有 pair。

**fail-open 方向是这条设计的不对称核心**：基础设施失败（超时 / JSON 解析失败 / 后端抛异常），
全部 pair 标 `uncertain`（reason=`entailment_unavailable`），**绝不标 `contradicted`**。
一次核验服务中断必须表现为「无法核验」，不能表现为「这引用是假的」——前者损失信息，
后者是错误断言。这与 routing 的 `needs_clarify=True / path_deep=False` 的方向性默认是同一条原则。

## 3. 词表与聚合

`Verdict = Literal["supported", "uncertain", "contradicted", "unbacked"]`

- `supported`：span 明确陈述了 claim 的内容（同义改写算）。
- `uncertain`：span 未提及 / 信息不足 / 模型没把握 / **任何基础设施失败** / span 文本缺失
  （`abstract` 为 null 时不烧模型，直接 `uncertain`，reason=`span_unavailable`）。
- `contradicted`：span 明确陈述了相反内容。只有模型能产出这个值。
- `unbacked`：标记无 registry 条目。只有 L0 能产出这个值；模型输出 unbacked 一律 coerce 成
  uncertain（词表里它是确定性层专属）。

**同一 citation_id 被多条 claim 引用时取最差**（contradicted < unbacked < uncertain < supported）：
一篇论文支撑其中一句、反驳另一句时，chip 不能显示绿。SSE 事件与 state 里每个 citation_id 只出现一次。

## 4. 模块与数据流

```
paper_agent/grounding/
  __init__.py      导出 + 分层论证
  claims.py        cited_claims(answer) -> list[CitedClaim]   纯函数
  entailment.py    Verdict / ClaimSpan / Judgement / EntailmentBackend(Protocol)
                   / SmallLLMEntailment / EntailmentUnavailable
  verify.py        verify_answer(answer, registry, backend|None) -> list[Judgement]
```

- **claims.py**：先按行切（markdown 的段落/表格行是天然单元），行内按 `。！？；!?` 切句段；
  标记正则与前端同一条窄规则 `\[(\d+)\]`（`[K1]`、`[12, 15]`、`[注]` 一律不匹配——
  知识问答路径的 `[K1]` 引用因此天然跳过，它有自己的审核层）。一个句段含 n 个标记 → n 条 claim。
- **entailment.py**：`SmallLLMEntailment` 模式与 `routing/small_llm.py` 完全同构——裸 JSON
  手工解析（不用 structured output，保住 ScriptedChatModel 可测 + 降级档零能力要求）、
  `asyncio.wait_for` 超时、异常折叠成 `EntailmentUnavailable`。批量：一次调用带全部 pair
  （上限 10 个/批，超出分批串行），要求输出 `{"index", "verdict", "reason"}` 数组；
  index 对不上、缺项、多项、词表外值 → 整批 unavailable（不做部分采纳——「半可信的结果」
  是个没人能消费的状态）。
- **verify.py**：`verify_answer` 是唯一入口。顺序：registry 未绑定 → `[]`（citations 是流的
  增强，不是前提，与 bind_citations 同一哲学）；无 `[n]` → `[]`；unbacked 查表；
  span 缺失 → uncertain；其余进 backend；backend 抛 → 全部 uncertain；
  最后按 citation_id 聚合取最差。

**图接入（builder.py）**：`build_graph(..., entailment=None)`。有 entailment 时，
`agent` 的出边从 `tools_condition` 换成组合条件 `_after_agent_edge`（读 `tools_condition`
的返回值 + 是否还有 verify 可走）：`tools` 或 `verify`；`verify` 节点返回
`{"grounding": [...]}` 后到 END。**返回 messages 为空**——核验结论不进对话历史，
不产生 token 事件。`entailment=None` 时图与今天逐字节相同（全部既有图测试不动）。

**verify 节点的跳过条件**（nodes.py）：`knowledge_sources() is not None` → `{}`（资料问答
有自己的二阶段审核，`[K1]` 也本就不匹配标记正则，双保险）；`current_registry() is None` →
`{}`（非聊天场景 / 未绑定的测试）；`backend is None` 时仍做 L0 unbacked 标注。
节点内部 try/except 全包：**核验永远不掀翻回答流**。

**State**：`grounding: NotRequired[list[dict[str, Any]]]`（每项
`{citation_id, verdict, reason}`），跟随 D8 七字段的先例——网关发事件、trace 可归因。

**SSE（chat.py）**：`event: grounding`，出现在 token 之后、`done` 之前：

```json
{"verdicts": [{"citation_id": 3, "verdict": "supported", "reason": null}], "source": "small_llm"}
```

稳定形状，每个字段都在，缺的是 null（与 `route`/`citation` 事件同一规则）。前端本期不渲染
（`route` 事件的后端先行先例），transport 已忽略未知事件名。

**配置（config.py / main.py / .env.example）**：`grounding_backend: "small_llm" | "off"`，
默认 `small_llm`；`grounding_timeout_ms: 8000`。`llm_api_key` 存在即启用降级档（与 D8
「路由就靠降级档单独开着」同一判断），`GROUNDING_BACKEND=off` 可整体关掉。
`/health` 增 `grounding: {configured, backend}` 块。默认开启是刻意的产品决策（README 承诺），
且只在答案真的含 `[n]` 时才花那一次调用——润色/格式化等无引用轮次零开销。

## 5. 评测（packages/evals）

**数据 `data/grounding.jsonl`**：24 条起步（刻意 < 50，与 routing < 100 同一条纪律：要长就
刻意长，别凑数）。每行：`{id, answer_id, claim, span, expect, note}`。`expect ∈
supported|uncertain|contradicted`；`note` **必填**（provenance：span 摘自哪个真实 fixture /
合成，expect 的理由）。claim 中文、span 英文摘要为主——这正是生产里的真实分布（中文提问、
英文文献），混合语言蕴含是这个后端真正要过的关。

**harness `src/evals/grounding.py`**：与 routing.py 同构的三件套——

1. `score()` 纯函数，指标算术离线可测；
2. `by_backend` 强制出处（`--backend` 无默认、非生产档打印 NOTE，防降级分被当生产分）；
3. errored 行记 miss 不剔除。

指标两层，边界写进 docstring：

- **后端质量**（这个 harness 直接测的）：3 类 accuracy / macro-F1 / per-class P/R，
  加**危险方向泄漏率** = `predicted=supported ∧ expect∈{uncertain, contradicted}`
  ——它直接上界了 Citation Precision 的测量误差：把假的判成真的，比把真的判成假的糟。
  errored 行**不**计泄漏（与 routing 相反且各自正确）：routing 的 errored 是「没拒成」，
  这里的 errored 是「没断言」——fail-open 的 uncertain 是安全输出。
- **产品指标演示**（README 的 Citation Precision / Recall）：按 `answer_id` 分组，
  对 `supported` 类取 P/R（Precision = 判 supported 中真 supported 的比例；
  Recall = 真 supported 中被判出的比例），组间宏平均，**从预测算**。
  实现期修正：原设计的「from_predictions 与 from_expect 两套对照」被推翻——
  本数据集的构造规则就是「该支持的都支持、不该支持的不支持」，from_expect 恒为
  1.0，没有信息量。真正回答「产品数字可信吗」的是后端质量指标 + 泄漏率；端到端
  live 数字需要对真实答案做人工标注的 live 评测，另列，不在离线 harness 里谎报。

**CLI**：`python -m evals --task grounding --backend small_llm`；`--task routing` 保持默认
（向后兼容现有命令）。`--backend jev` + grounding → 干净报错（Jev 蕴含后端待 key），
不静默降级。

**门禁测试**：`test_grounding_data.py`（schema、词表、note 必填、answer_id 分组合法）
+ 与 `paper_agent.grounding.VERDICTS` 的交叉一致性测试（routing 的 INTENTS 双清单同款纪律）；
`test_grounding_eval.py` 钉死指标算术（含手算 toy 集、泄漏方向、分组宏平均、两套 P/R 的分叉）。

## 6. 测试策略（全部离线）

- `packages/agent/tests/test_grounding_claims.py`：切句边界（中西混排、markdown 行、
  `[K1]`/`[12,15]` 不匹配、一句多标记、无标记）。
- `test_grounding_entailment.py`：ScriptedChatModel 驱动——合法 JSON / 围栏包裹 / 畸形 /
  词表外 / 超时 / 空批不发调用 / >10 分批。
- `test_grounding_verify.py`：unbacked 查表、span 缺失不烧模型、backend 异常 → 全 uncertain、
  同 id 取最差、registry 未绑定 → []。
- `test_graph_grounding.py`：真编译图——含引用答案写出 grounding、无引用为空、
  entailment=None 图不变、unbacked 在图内产生、verify 不追加 messages。
- `apps/api/tests/test_api_grounding.py`：SSE 契约——grounding 事件在 citation 后 done 前、
  形状稳定、knowledge 路径不发、核验失败流不断。
- 全套维持 `pytest -q` 全绿 + ruff check/format 全绿门禁。

## 7. 被否掉的备选

- **整篇答案一次性判**（便宜一个量级）：给不出每引用的标注，Citation Precision 无从定义。否。
- **NLI 小模型本地推理**（零边际成本）：Week 3 之前没有本地推理栈，且 devlog「后续规划」已把
  本地小模型档位列为刻意不做。协议已留好（EntailmentBackend），可后换。否。
- **contradicted 时触发重检索**（Deep Path Verifier 行为）：本设计是标注层；把重试接进
  Fast Path 会改变每轮成本模型，且 README 把「带诊断回退」明确划给 Deep Path。否。
- **`unbacked` 交给模型判**：registry 是网关的，查表是确定性的；把确定信号概率化只会引入
  新的泄漏面。否。
- **默认 off**：README 的核心承诺继续是空头支票，且无引用轮次本就零开销。否（off 开关保留）。

## 8. 上线边界（如实声明）

- span 是**摘要级**，不是页级/句级：`SpanRef` 仍为 null，页面 chip 不会因本设计变化。
- 标注是模型判断，不是事实证明；`supported` 不等于「该结论正确」，只等于「摘要说了这话」。
- Jev 蕴含后端、页级 span、Deep Path 的拦截-回退，三项是显式遗留，不在本设计内。
