# Grounding + 引用评测 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 README 的「未获支持的引用会被标记」成真：Fast Path 答案的每个 `[n]` 获得
supported / uncertain / contradicted / unbacked 标注，并落地 Citation Precision / Recall 的
评测 harness。

**Architecture:** L0 确定性（unbacked 查 registry 表）+ L1 模型蕴含（claim 句段 ×
abstract span，一次批量调用）；fail-open 全部指向 uncertain，绝不把基础设施失败说成
contradicted。图上加 `verify` 节点（只写 state、不发消息），网关翻成 `grounding` SSE 事件。
评测 harness 与 routing 同构：纯函数 score + by_backend 出处强制 + errored 行记 miss。

**Tech Stack:** LangGraph（图节点）、LangChain chat model（ScriptedChatModel 测试件）、
pytest（asyncio_mode=auto）、ruff（line-length 100）。

**执行方式：** inline（executing-plans 式，批次推进；本计划由侦察过全库的同一会话执行）。
提交策略：按任务提交，message 用仓库现有 `feat(agent): …` 风格。

**命令约定（Windows）：** 测试 `uv run --no-sync pytest -q`；ruff `uv run --no-sync ruff check . && uv run --no-sync ruff format --check .`。

---

### Task 1: `paper_agent.grounding` 包——claims 抽取（纯函数）

**Files:**
- Create: `packages/agent/src/paper_agent/grounding/__init__.py`（本任务先放最小导出，Task 3 补全）
- Create: `packages/agent/src/paper_agent/grounding/claims.py`
- Test: `packages/agent/tests/test_grounding_claims.py`

- [ ] **Step 1: 写失败测试**——覆盖：中文句号切分；行优先（markdown 段落/表格行不打穿）；
  西文 `.` 后跟空格+大写才切，小数/缩写不切；`\[(\d+)\]` 匹配、`[K1]`/`[12, 15]`/`[注]` 不匹配；
  一句两标记产出两条 claim；无标记 → 空列表；标记紧贴标点（`…显著[3]。`）也能归属该句。

```python
from paper_agent.grounding.claims import cited_claims, CitedClaim

def test_chinese_sentence_with_trailing_marker():
    claims = cited_claims("对比学习提升了表征质量[3]。随后的工作扩展了这一结论。")
    assert claims == [CitedClaim(text="对比学习提升了表征质量[3]。", citation_id=3)]

def test_line_is_the_unit_not_the_paragraph(): ...
def test_decimal_is_not_a_sentence_boundary(): ...
def test_k_markers_and_ranges_do_not_match(): ...
def test_two_markers_in_one_sentence_yield_two_claims(): ...
def test_no_markers_returns_empty(): ...
```

- [ ] **Step 2: 跑测试确认失败**（ModuleNotFoundError）。
- [ ] **Step 3: 实现 claims.py**：`_MARKER = re.compile(r"\[(\d+)\]")`；先 `splitlines()`，
  行内 `_SENTENCE_END = re.compile(r"(?<=[。！？；!?])(?=[^\s])|[.](?=\s+[A-Z0-9])")` 切句段；
  每段对 `_MARKER.finditer` 产 `CitedClaim(text=段.strip(), citation_id=int(n))`。

- [ ] **Step 4: 测试通过。**

### Task 2: entailment 抽象 + SmallLLM 后端

**Files:**
- Create: `packages/agent/src/paper_agent/grounding/entailment.py`
- Test: `packages/agent/tests/test_grounding_entailment.py`

- [ ] **Step 1: 写失败测试**——用 `ScriptedChatModel`：合法 JSON 数组→verdicts；```json 围栏→解析；
  畸形 JSON / index 缺项 / 词表外值（含模型输出 "unbacked" → uncertain）/ 抛异常 / 超时 →
  `EntailmentUnavailable`；空批不调模型；>10 pair 分批串行（记录调用次数）。

```python
class SmallLLMEntailment:
    name = "small_llm"
    def __init__(self, model: Any, *, timeout: float = 8.0, batch_size: int = 10): ...
    async def judge(self, pairs: Sequence[ClaimSpan]) -> list[Judgement]:
        """一次批量调用判全部 pair；任何失败抛 EntailmentUnavailable（调用方 fail-open）。"""
```

类型（同文件）：`Verdict = Literal["supported","uncertain","contradicted","unbacked"]`；
`VERDICTS` 元组；`MODEL_VERDICTS = ("supported","uncertain","contradicted")`（unbacked 是
L0 专属，模型输出一律 coerce 成 uncertain）；`ClaimSpan(claim, citation_id, span)`；
`Judgement(citation_id, verdict, reason: str | None)`。

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**——SYSTEM_PROMPT（中文，判定标准三行 + 「宁可 uncertain 不要猜 +
  数字/人名/结论必须能在来源里找到依据」）；Human 消息是
  `[{"index": i, "claim": ..., "source": ...}]` 的 JSON；解析取首个 `[` 到末个 `]`，
  要求 list 且长度==len(pairs)、index 逐项对上、verdict 在 MODEL_VERDICTS 内；
  `asyncio.wait_for` 包 `ainvoke`，异常统一 `raise EntailmentUnavailable(...) from exc`
  （与 `routing/small_llm.py` 同构）。
- [ ] **Step 4: 测试通过。**

### Task 3: verify_answer 编排（L0+L1 聚合）

**Files:**
- Create: `packages/agent/src/paper_agent/grounding/verify.py`
- Modify: `packages/agent/src/paper_agent/grounding/__init__.py`（导出 + 包 docstring 写分层论证）
- Test: `packages/agent/tests/test_grounding_verify.py`

- [ ] **Step 1: 写失败测试**——registry 未绑定（None）→ []；无标记 → []；unbacked 查表
  （registry 空/标记越界）；span 缺失 → uncertain(span_unavailable) 且**不调 backend**；
  backend 正常 → 逐 pair verdict；backend 抛 → 全部 uncertain(entailment_unavailable)；
  同一 citation_id 两条 claim 取最差（contradicted < unbacked < uncertain < supported）；
  输出按 citation_id 升序、每 id 一项。

```python
async def verify_answer(
    answer: str, registry: CitationRegistry | None, backend: EntailmentBackend | None
) -> list[Judgement]: ...
```

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。聚合用字典按 citation_id 收 worst（`_RANK = {"contradicted": 0, "unbacked": 1, "uncertain": 2, "supported": 3}`，取 min）。
- [ ] **Step 4: 测试通过。**

### Task 4: 图接入——verify 节点 + 条件边

**Files:**
- Modify: `packages/agent/src/paper_agent/graph/state.py`（加 `grounding: NotRequired[list[dict[str, Any]]]`，
  docstring 的 Week 2 规划行更新为已落地 D9）
- Modify: `packages/agent/src/paper_agent/graph/nodes.py`（`make_verify_node(entailment)`）
- Modify: `packages/agent/src/paper_agent/graph/builder.py`（`build_graph(..., entailment=None)` +
  `_after_agent_edge` + `_VERIFY_EXITS` 映射）
- Test: `packages/agent/tests/test_graph_grounding.py`

- [ ] **Step 1: 写失败测试**（真编译图 + ScriptedChatModel/ScriptedEntailment）：
  含 `[n]` 答案（工具返回论文注册 registry）→ state 有 grounding、graph 输出 verify 节点更新、
  messages 未被 verify 追加；无引用答案 → grounding 为空/缺失；entailment=None →
  图行为与现在逐字节一致（显式断言 bare 图不含 verify 节点）；verify 内部抛异常 →
  grounding == [] 且图正常结束（fail-open 在节点内吞掉）。

```python
def _after_agent_edge(state: AgentState) -> str:
    if tools_condition(state) == "tools":
        return "tools"
    return "verify"

# builder 内（entailment 不为 None 时）：
graph.add_conditional_edges("agent", _after_agent_edge, {"tools": "tools", "verify": "verify"})
graph.add_node("verify", make_verify_node(entailment))
graph.add_edge("verify", END)
```

- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。`make_verify_node` 跳过条件：`knowledge_sources() is not None` → `{}`；
  `current_registry() is None` → `{}`；`verify_answer` 全包 try/except → `{"grounding": []}`。
- [ ] **Step 4: 测试通过；随后跑 `packages/agent` 全量确认无回归。**

### Task 5: 网关——`grounding` SSE 事件 + 配置 + /health

**Files:**
- Modify: `apps/api/src/api/config.py`（`grounding_backend: str = "small_llm"`、`grounding_timeout_ms: int = 8000`）
- Modify: `apps/api/src/api/main.py`（`_build_entailment(settings)`；`build_graph(entailment=…)`；
  `/health` 加 `grounding: {configured, backend}`）
- Modify: `apps/api/src/api/routes/chat.py`（`_grounding_event(update)`；node == "verify" 分支）
- Modify: `.env.example`（两个新变量 + 注释）
- Test: `apps/api/tests/test_api_grounding.py`

- [ ] **Step 1: 写失败测试**——事件顺序 `… citation … token … grounding → done`；
  形状稳定（`verdicts` 每项三键齐全、`source` 在）；knowledge 路径无 grounding 事件；
  entailment 抛异常时流仍以 done 结束（fail-open 不掀流）；`/health` 的 grounding 块。
  复用 test_api.py 的 `_routed_agent` 模式：真编译图 + dependency_overrides + bind_registry。
- [ ] **Step 2: 失败确认。**
- [ ] **Step 3: 实现**。`_grounding_event`：

```python
def _grounding_event(update: dict[str, Any] | None) -> dict[str, str] | None:
    verdicts = (update or {}).get("grounding")
    if not verdicts:
        return None
    return _event("grounding", {
        "verdicts": [
            {"citation_id": v.get("citation_id"), "verdict": v.get("verdict"), "reason": v.get("reason")}
            for v in verdicts
        ],
        "source": (update or {}).get("grounding_source"),
    })
```

（`grounding_source` 由 verify 节点一并写入 state：backend.name 或 None。）
- [ ] **Step 4: 测试通过 + `apps/api` 全量。**

### Task 6: 评测数据 + harness + CLI

**Files:**
- Create: `packages/evals/data/grounding.jsonl`（24 条，note 必填；span 取
  `packages/mcp-servers/literature-search/tests/fixtures/search_works.json` 的真实摘要
  （用 server 的 inverted-index 重建算法离线重建，provenance 写进 note）+ 标注了合成的行）
- Create: `packages/evals/src/evals/grounding.py`（Row/Report/score 纯函数/run_eval；
  危险方向泄漏率；answer_id 分组的 from_predictions / from_expect 双套 P/R）
- Modify: `packages/evals/src/evals/__main__.py`（`--task {routing,grounding}` 默认 routing；
  grounding 的 backend 构建器；报告排版）
- Test: `packages/evals/tests/test_grounding_data.py`（schema/词表/note 必填/answer_id 合法 +
  与 `paper_agent.grounding.VERDICTS` 交叉一致）、`packages/evals/tests/test_grounding_eval.py`
  （手算 toy 集钉死算术：accuracy、per-class、泄漏方向、分组宏平均、两套 P/R 分叉、errored=miss）

- [ ] **Step 1: 写数据门禁失败测试。**
- [ ] **Step 2: 写 harness 失败测试（纯函数 score）。**
- [ ] **Step 3: 生成 24 条语料**（脚本重建 fixture 摘要 → 手写 claim/expect/note）。
- [ ] **Step 4: 实现 harness + CLI；离线测试全绿。**
- [ ] **Step 5: live 跑与否——不跑。** 需读 `.env` key + 打外部服务，按 D7 先例必须用户明文授权；
  把确切命令写进 devlog（`uv run python -m evals --task grounding --backend small_llm`）。

### Task 7: 门禁 + 文档

- [ ] 全套 `uv run --no-sync pytest -q` 全绿（预期 ~960+ passed）。
- [ ] `uv run --no-sync ruff check .` + `ruff format --check .` 全绿（新文件先 format）。
- [ ] README：评测表补 grounding 行与复现命令；「产品边界」段的承诺措辞改为与实现一致
  （摘要级标注，页级 span 仍 Week 3）；架构图 GND 节点标注（Fast Path 已接标注层）。
- [ ] devlog 增 D9 条目（按既有格式：做了什么 / 坑·判断 / 当前门禁 / 仍未做）。

## Self-Review

- **Spec 覆盖**：spec §2 分层→Task 2/3；§3 词表聚合→Task 2/3；§4 模块/图/网关/配置→Task 1–5；
  §5 评测→Task 6；§6 测试→各任务；§8 边界声明→Task 7 README/devlog。无缺口。
- **占位符**：无 TBD；Task 6 Step 3 的 claim 文本在执行时依据重建摘要手写（数据创作，
  非代码占位）。
- **类型一致性**：`Judgement(citation_id, verdict, reason)` 贯穿 verify/state/SSE；
  `EntailmentBackend.judge(pairs) -> list[Judgement]` 贯穿 backend/harness；
  state 键 `grounding` / `grounding_source` 与 `_grounding_event` 读取一致。
