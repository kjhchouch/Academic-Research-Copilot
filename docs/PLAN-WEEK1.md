# Week 1 · 地基：骨架跑通 + Trace 可见

> **本周唯一目标**：周末能演示「用户提问 → 意图路由 → Agent 调用真实文献检索工具 → 流式返回带真实引用的答案」，且 Langfuse 里有完整 trace。
>
> **本周刻意不做**：多 agent 编排、RAG 入库、grounding 校验、长期记忆、部署。这些是 Week 2–4 的事。本周只做**贯通的细链路**，不做宽功能。

---

## 退出标准（Week 1 完成的判定）

全部满足才算过：

- [x] **零外部服务即可启动**（不需要 Docker；checkpoint 走 SQLite）
- [x] 前端输入问题，**逐字流式**看到回答（D2 实测：34 分片 / 40ms 间隔 / `text/event-stream`）
- [ ] 回答里的引用是**真实存在的论文**，点击能跳到 DOI
- [ ] Langfuse 控制台能看到一条完整 trace：`route → LLM → tool_call → LLM` —— **D3 已打通 `LLM → tool_call → LLM`（含 token 与延迟），只差 `route` 节点（D4）；成本不在 Langfuse 里，由 `pricing.py` 算，见 D3 验收结果**
- [ ] 意图路由有**准确率数字**（不是"看起来能用"）
- [x] 重启后端，对话状态不丢（D3 实测：跨进程重启后从 `data/checkpoints.sqlite` 还原 4 条消息，客户端重放的历史未被重复写入，模型直接从 checkpoint 上下文答对且未重复调工具）
- [x] README 有架构图 + 本地启动步骤

---

## 关于 Docker：不是 Week 1 的前置

本机没有 Docker，且**不需要装**。`packages/agent/src/paper_agent/storage.py` 为每个存储后端提供了本地实现：

| 用途 | 本地默认 | 生产（Week 4） |
|---|---|---|
| checkpoint | `sqlite` → `data/checkpoints.sqlite` | `postgres` |
| Jev 决策缓存 | `memory`（LRU+TTL） | `redis` |
| PDF 附件 | `local` → `data/blobs/` | `s3`（MinIO） |

切换只改环境变量，业务代码不动。`infra/docker-compose.yml` 保留，Week 4 部署时用。

> **Week 3 向量检索：已定（2026-10-14）—— 本地全走 SQLite，部署时换 pgvector。**
> 向量 = `sqlite-vec` 0.1.9 `vec0` 虚表，关键词 = SQLite FTS5 `bm25()`，融合 = RRF。三者已实测跑通。
> **不修本机 PostgreSQL 16** 的原因：它缺 `icudt67.dll`（搬迁丢失）起不来；且就算修好，pgvector 官方无 Windows 预编译包，只能 `nmake` 源码编译，而本机没有 VS Build Tools。装它要半天到一天，而 Week 3 的价值在检索**设计**不在存储引擎。
> ⚠️ SQLite `bm25()` 返回负数（越负越相关），`ORDER BY bm25(t)` 升序 —— RRF 只用排名，无害。


---

## D0 · 半天｜环境与账号

**装工具**
```bash
# Python 侧用 uv，别用 pip/poetry
curl -LsSf https://astral.sh/uv/install.sh | sh
uv python install 3.12

# Node 侧
corepack enable && corepack prepare pnpm@latest --activate

# 确认
uv --version && pnpm -v && docker compose version && node -v
```
Windows 需要 Docker Desktop + WSL2。

**申请 Key（今天就全办完，别等到用的时候卡审核）**

| 服务 | 用途 | 备注 |
|---|---|---|
| OpenAI 或 Anthropic | 主模型 | 建议同时备一个 OpenRouter 做模型路由 |
| **Langfuse Cloud** | Trace / Eval | ★ **Week 1 用云免费版**，自托管留到 Week 4 |
| **TypeSafe AI** | Jev 意图路由 | 上一轮讨论的决策模型 |
| Semantic Scholar API key | 文献检索 | 无 key 也能用，但配额低到没法开发 |
| OpenAlex | 文献元数据 | 免费无需 key，填 email 进 `mailto` 参数即可提速 |

**初始化仓库**
```bash
cd E:/agent
git init
```
`.gitignore` 至少包含：`.env`、`__pycache__/`、`.venv/`、`node_modules/`、`.next/`、`*.pyc`、`.DS_Store`、`data/`
`.env.example` 列出所有 key 名（**只提交 example，永不提交 .env**）。

> ✅ **验收**：四个 CLI 都有版本号；所有 key 已写进本地 `.env`。

---

## D1 · 1 天｜Monorepo + 基础设施

**建目录**（这是最终形态，本周只填其中几个）
```
E:\agent\
├── apps/
│   ├── web/                  # Next.js 15
│   └── api/                  # FastAPI
├── packages/
│   ├── agent/                # ★ LangGraph 编排
│   │   ├── graph/            #   state.py · nodes/ · builder.py
│   │   ├── routing/          #   decision_model.py · intent.py
│   │   ├── retrieval/        #   (Week 3)
│   │   ├── grounding/        #   (Week 2)
│   │   ├── tools/
│   │   └── prompts/
│   ├── mcp-servers/
│   │   └── literature-search/
│   └── evals/                # ★ 数据集 + runner
├── infra/
│   ├── docker-compose.yml
│   └── migrations/
├── skills/                   # (Week 2+)
├── docs/
└── pyproject.toml            # uv workspace 根
```

**docker-compose.yml**
```yaml
services:
  postgres:
    image: pgvector/pgvector:pg17        # ★ 用官方镜像，别自己装扩展
    environment:
      POSTGRES_PASSWORD: postgres
      POSTGRES_DB: agent
    ports: ["5432:5432"]
    volumes: [pgdata:/var/lib/postgresql/data]

  redis:
    image: redis:7-alpine
    ports: ["6379:6379"]

  minio:
    image: minio/minio
    command: server /data --console-address ":9001"
    environment:
      MINIO_ROOT_USER: minioadmin
      MINIO_ROOT_PASSWORD: minioadmin
    ports: ["9000:9000", "9001:9001"]
    volumes: [miniodata:/data]

volumes:
  pgdata:
  miniodata:
```
> ⚠️ **不要在这里加 Langfuse**。Langfuse v3 自托管需要额外拉 ClickHouse + worker + web 三个服务，会吃掉你一整天。Week 1 用云版。

**后端最小 FastAPI**
`apps/api/main.py`：一个 `/health` 返回 `{"ok": true}`，一个 `/api/chat` 先返回固定字符串。
用 `uv run uvicorn main:app --reload --port 8000` 起。

**前端**
```bash
cd apps && pnpm create next-app web --ts --tailwind --app --src-dir
cd web && pnpm add ai
```
改一个最简聊天页，能 POST 到 `http://localhost:8000/api/chat` 并把返回打印出来。

> ✅ **验收（已实测通过，但去掉了 Docker）**：~~`docker compose up -d` 三个容器 healthy~~ → 存储层已本地化（SQLite / 内存 / 本地文件），**零外部服务即可启动**，`docker compose` 降级为 Week 4 的部署事项。前端点发送，页面显示后端返回的固定字符串。

---

## D2 · 1 天｜SSE 流式打通 ✅ 已完成（2026-10-14）

**后端**
```bash
uv add --package api "sse-starlette>=2.1"     # 装到 3.5.0
```
`/api/chat` 改成流式，先**不接 LLM**，用一个假的分片生成器验证管道（按码点切 4 字、间隔 40ms；两个常量在测试里被打成 0 以免拖慢套件）。

**前端 —— 原计划写错了，这里记一下为什么**

原计划给的两条路都不对：
- ~~简单路：`TextStreamChatTransport` + 后端返回 `text/plain`~~ → 装了 `ai@7.0.140` 读 `.d.ts` 才发现它**只把响应体当纯文本切 delta，完全不解析 `event:` 行**。它和本节验收标准里要求的 `text/event-stream` 是互斥的两条路——原计划自相矛盾。
- ~~完整路：Python 按 AI SDK 的 data stream 格式输出~~ → 那是个 TS 包的**内部协议**，跨版本会变，而且网关不该依赖一个它根本不 import 的前端包的私有格式。

**实际走的第三条路：自写 transport。** `HttpChatTransport` 是抽象类且运行时有导出，继承它只需实现 `processResponseStream` 一个方法（请求构造 / abort / headers / 非 2xx 全部继承）。协议知识集中在一个有类型检查的 TS 文件里，Python 端保持自有稳定协议：

```
event: token   data: {"text": "..."}     一个文本增量
event: done    data: {}                  正常结束（客户端把「没有 done」判为截断）
event: error   data: {"message": "..."}  失败，之后不再有 done
```

D3 往后加 `route` / `tool` / `citation` 事件时，前端 transport **已经忽略未知事件名**，所以后端可以先加、前端后接。

`useChat` 另外注意：它**不在 `ai` 包里**，要装 `@ai-sdk/react`（`4.0.143` 精确依赖 `ai@7.0.140`，两个包各自独立编号，不是版本冲突）。

> ✅ **验收（已实测通过）**：输入任意内容，文字逐字出现，不是一次性弹出。DevTools Network 里 `mimeType=text/event-stream`，连接保持到结束。
>
> 实测数据：浏览器网络层插桩记录到 **34 个分片、间隔约 40ms（0/38/79/120…）、连接持续 1379ms**；后端用 `httpx.stream` 独立对照测得 **17 个 token 事件、平均间隔 43.3ms**，与配置的 `CHUNK_DELAY=0.04` 吻合。停止按钮实测能真中断（分片数冻结在 79、`eof` 始终未到、状态回到 `ready`、**不误报错误**）。

---

## D3 · 1 天｜LangGraph 第一个 Agent + Langfuse Trace ✅ 已完成（2026-10-17）

**装依赖**
```bash
uv add langgraph langgraph-checkpoint-postgres langchain-openai langfuse
```

> **实际只装了 `langchain-openai` + `aiosqlite`，`langfuse` 提到 `>=5`，`langgraph-checkpoint-postgres` 刻意没装。** checkpoint 和检索都定在 SQLite（见 devlog 坑 11），postgres 分支现在是显式 `NotImplementedError`。Week 4 真要部署再补。

**最小图**
```python
from langgraph.graph import StateGraph, START, END
from langgraph.prebuilt import ToolNode

def build_graph(checkpointer):
    g = StateGraph(AgentState)
    g.add_node("agent", call_model)
    g.add_node("tools", ToolNode([get_current_time]))   # 先用假工具
    g.add_edge(START, "agent")
    g.add_conditional_edges("agent", tools_condition)
    g.add_edge("tools", "agent")
    return g.compile(checkpointer=checkpointer)
```
`AgentState` 本周只需要 `messages: Annotated[list, add_messages]`。

**接 Langfuse**
用 `CallbackHandler` 挂到 LangChain 的 config 上，确保 trace 里能看到：LLM 调用、token 数、tool 调用、延迟。
**同时打开成本追踪**（配好 model pricing），Week 4 的"降本 N%"数字从这里来。

> ✅ **验收**：Langfuse 控制台里一条 trace 展开后能看到 `agent → tool → agent` 的嵌套 span，每个 span 有 token 和耗时。
>
> **这一步比看起来重要**：Week 2–4 所有的调试、评测、成本分析都建立在这个 trace 上。今天没接好，后面全是盲调。

**验收结果（2026-10-17，US region live）**

- ✅ 嵌套 span 完整：`LangGraph → agent → ChatOpenAI → tools_condition → tools → count_words → agent → ChatOpenAI → tools_condition`。工具那一轮 9 个 observation，直接回答那一轮 4 个。
- ✅ token 与耗时**有**：`providedModelName=deepseek-flash`，7 次 generation 合计 input 3621 / output 414 / total 4035，平均延迟 891ms。`usageType` 还拆出 `output_reasoning` 与 `input_cache_read`。
- ✅ `sessionId=<thread_id>`、`traceName=chat.fast_path`、`environment=development` 全部正确；`userId=null` 是对的，还没接鉴权。
- ⚠️→✅ **成本追踪：Langfuse 里做不到，已改在代码里算。** 原因是两条，都不是配置漏了：Langfuse 的托管价格表 190 个条目里**一个 DeepSeek 都没有**；而且它的 model definition 表达不了 DeepSeek 的**分时价**（高峰 = UTC 周一至五 01:00–04:00 与 06:00–10:00，排除中国法定节假日，低峰恒为一半）。所以 `totalCost` 在 Langfuse 里**永远是 0，这是已知且刻意的**。
  → 现在由 `paper_agent/pricing.py` 从 LangChain 的 `usage_metadata` 算，网关每轮打一行日志。live 实测 `turn <id> cost $0.000366 (deepseek-flash=$0.000366)`，与手算逐项吻合。**Week 4 的「降本 N%」有数据源了，而且能做 Langfuse 做不到的反事实比较**（同一轮换成 `deepseek-v4-pro` 要花多少）。详见 devlog「D3 补充」。
- ⚠️ trace 的 `metadata`（`ui_context`）**无法用 API 验证**：2026-09-16 之后建的组织，`/api/public/traces`、`/api/public/sessions`、`/api/public/observations/{id}` 全部 410 或 404，也没有 `/api/public/v2/traces`。只能读 `/api/public/v2/observations` 列表和 `/api/public/v2/metrics`（GET + urlencoded `query`）。metadata 只在 UI 里看得到。
- ⚠️ **一条 trace 含完整 prompt 和 completion**，开 Cloud 就等于用户未发表的手稿出境。给自己用可以，给别人用之前必须挂 `mask=` 钩子。这也是 Week 4 自托管的真正理由。

---

## D4 · 1 天｜Jev 接入 + DecisionModel 抽象层 ✅ 已完成（2026-10-18，离线部分）

**装 SDK**
```bash
uv add typesafe-sdk
```

**先写抽象，再写实现**（不要直接在业务代码里 import Jev）
```python
# packages/agent/routing/decision_model.py
class DecisionModel(Protocol):
    async def decide(self, state: dict, questions: dict) -> Decisions: ...

class JevBackend(DecisionModel): ...
class SmallLLMBackend(DecisionModel): ...   # 降级用，也用于本地测试
```

**必须带的三件套**
1. **超时**：800ms，超时即降级
2. **熔断**：连续失败 N 次 → 打开熔断，走 `SmallLLMBackend`；半开探测恢复
3. **缓存**：`hash(state)` 相同的请求直接返回（Redis，TTL 1h）

**意图定义**（先只 6 个，不要贪多）
```
literature_search · outline · draft_section · polish · citation_format · unclear
```
外加两个 `Noul`：`path_deep`（是否走重流程）、`needs_clarify`（信息是否不足）。

> ✅ **验收**：写 20 条测试语料（中英混杂、口语化各来几条），两个 backend 都能返回 intent；Langfuse trace 里能看到 Jev 调用的延迟和成本。
>
> ⚠️ **今天开始就要攒 eval 语料**。把你想到的每条测试句子追加进 `packages/evals/data/routing.jsonl`，D7 要凑到 100 条。**千万别留到 D7 一次性编** —— 一次性编出来的语料分布是假的，测不出问题。

**验收结果（2026-10-18）**

落地在 `packages/agent/src/paper_agent/routing/`，四个模块：`decision_model.py`（抽象 + 问题集）、`jev.py`、`small_llm.py`、`resilience.py`（熔断 + 缓存 + 级联）。

- ✅ **抽象先于实现**，且 Protocol 签名与本计划写的不一样：`decide(request: RouteInput) -> Decisions`，**不接受 `questions` 参数**。计划里那个签名会让调用方自己构造 `Noul`/`Choice`，等于每个调用点都依赖 Jev 的问题 DSL，抽象就成了装饰。问题集是随版本固定的，不是每次请求现选的，所以它作为数据放在 `decision_model.py`，两个 backend 各自渲染成自己 API 要的格式。
- ✅ **超时 800ms**，但必须配 `RetryPolicy(max_retries=0)`。SDK 默认 `max_retries=2` + `api_timeout_error=True` + `backoff_initial=0.5`，一次挂死调用实测约 3.7s —— 预算的 4.6 倍，而 `RetryPolicy.timeout=30.0` 那个总预算根本拦不住。
- ✅ **熔断**：连续 3 次失败打开，30s 后半开，半开只放一个探测。时钟可注入，测试不用真睡 30 秒。
- ⚠️→✅ **缓存**：计划写「`hash(state)`、Redis、TTL 1h」，两处都改了。内置 `hash()` 对字符串按 `PYTHONHASHSEED` 每进程加盐，跨 worker 不一致、重启即全失效，本地单进程测试还看不出来 —— 改成 sha256(canonical JSON)。Redis 换成了复用已有的 `storage.Cache`（`MemoryCache` 或 `_RedisCache` 都行），本机没有 Redis，不为了一个简历项目起一个。
- ✅ **6 个 intent** 与 `packages/evals` 门禁里的 `INTENTS` 完全一致，并新增交叉测试保证两边不漂移。故意重复的两份清单必须有测试，否则加了标签只改一边，准确率数字会静默失真。
- ✅ **两个 Noul**：`.noul` 返回的是 0–1 的**概率**不是 bool，0.5 附近是「模型没决定」。所以两个问题的安全默认值**方向相反**：`needs_clarify` 不确定 → True（猜错槽位产出的是自信的错活，反问只花一轮）；`path_deep` 不确定 → False（重流程是多 agent 管线，且 80% 流量是窄任务）。
- ✅ **语料 24 → 36 条**：injection 1 → 3（补了间接注入和越狱式注入），并给 `path_deep` 加了 2 真 / 2 假的覆盖 —— 此前这个字段零覆盖，等于快慢双路径这个核心设计无人可测。
- ✅ **门禁**：344 passed / 1 skipped，ruff clean。

**仍未做**

- ❌ **在线验收**：`TYPESAFE_API_KEY` 还没申请。所以「Langfuse trace 里看到 Jev 的延迟和成本」这条没验。目前所有测试都走 SDK 的 `transport` 假件（真实序列化 + 真实解码 + 真实二值化，只换掉 socket）。`MockTransport` 绕过网络层，httpx 不会启计时器，**所以 800ms 超时能否真的触发无法离线验证** —— 只能钉住配置值和它抛出的异常类型。
- ❌ **接进图**：按计划属于 D6。目前 `make_router()` 已可用，`config.py` 的 `typesafe_api_key` / `decision_model_backend` / `jev_timeout_ms` 三个字段早就预留好了，接线时注意 **ms → s 要除以 1000**。
- ❌ **Jev 的成本没有计价**。`pricing.py` 的规则是「未知模型抛错，绝不打成 $0」，而 Jev 的价格表我没有可靠来源，所以只记 token 不算钱。API 文档说 output token 目前免费。

**已知缺口（留给后面的决定）**

产品范围内的「处理数据 / 生成统计代码」在 6 个 intent 里**没有对应标签**。按计划「先只 6 个，不要贪多」，现在加第 7 个是错的；但这类请求目前只能落到 `unclear`，也就是被当成越界拒掉。**没有往语料里塞这一行** —— 因为门禁的 `test_negative_and_injection_expect_unclear` 会反过来保护这个错误标签，等 Week 3 真做数据处理时再加 `data_analysis` 会更干净。

---

## D5 · 1 天｜MCP literature-search Server

**独立进程，走 MCP 协议**（不要写成普通 Python 函数 —— 用 MCP 是本周的技术信号点）

```bash
uv add "mcp[cli]"
```

**三个工具**

| 工具 | 数据源 | 说明 |
|---|---|---|
| `search_papers(query, year_from, limit)` | OpenAlex | 主力，免费，返回 title/authors/year/venue/doi/abstract/cited_by |
| `get_paper(paper_id)` | Semantic Scholar | 详情 + 参考文献列表 |
| `format_citation(doi, style)` | 自己写或 citeproc | style: `apa` / `ieee` / `gb-t-7714` |

> **D5 实测偏差**：`get_paper` 改成**按标识符形状路由**——DOI 和 `W...` 走 OpenAlex，`CorpusId:` / `ArXiv:` / 40 位 hex 才走 Semantic Scholar。原因是**无 key 的 S2 第一个请求就 429**，而 D5 没申请 key；OpenAlex 单家就能满足「详情 + 参考文献列表」（`referenced_works` 再用 `filter=openalex_id:A|B|C` 批量解析成标题）。`format_citation` 走「自己写」：CSL 生态对 GB/T 7714 覆盖很差，而那正是本产品最需要的一个。

**调试**：用 MCP Inspector 单独测 server，别在 agent 里边调边猜。

```bash
# inspector 是 npm 包，不是 Python 工具，所以是 npx 不是 uvx（原计划这里写错了）
npx @modelcontextprotocol/inspector
```

Inspector 里填的 stdio 命令是 `literature-search`（`uv sync --all-packages` 后在 `.venv/Scripts/`），或者 `python -m mcp_literature_search`——后者不依赖 venv 的 Scripts 目录在 PATH 上，写进客户端配置更稳。

> `mcp[cli]` 自带 `mcp dev <file_spec>`，源码里就是 `npx @modelcontextprotocol/inspector` + `uv run mcp run <file_spec>` 的组合。它要一个**模块级 server 对象**，而本项目是 `build_server()` 工厂构造的（settings 和 transport 都要能注入才能离线测），所以没有这样的对象。**D5 没有实跑过 `mcp dev`**（它会 npx 装第三方包），要用得先在模块里暴露一个 `server = build_server()`，或者直接用 Inspector 手填 stdio 命令。

> ⚠️ **本周最大的坑在这里**：MCP server 用 stdio 传输时，**stdout 是协议通道**。任何 `print()` 都会破坏 JSON-RPC 流，表现为 agent 侧莫名的连接错误或解析失败。
> **所有日志必须走 `logging` 到 stderr 或文件**，并且第一件事就是配好 `logging.basicConfig(stream=sys.stderr)`。
>
> **D5 实测补充**：mcp 2.x 的 `stdio_server()` 自己挡了一半——服务期间把 fd 1 指到 stderr，并**显式把 wire 重新包成 UTF-8**（源码注释：「the std handles' platform encodings are unreliable」），所以 GBK 控制台下中文描述是安全的。另一处要知道：`MCPServer.__init__` 会往 root logger 挂 RichHandler，但写的是 **stderr** 且尊重已有 handler，所以先 `basicConfig` 就能拿到一种可预期的格式。**纪律照旧**——这是 2.x 的行为，1.x 没有。回归测试是 `tests/test_stdio_purity.py`：起真子进程，断言 stdout 每一行都是合法 JSON-RPC。

> ✅ **验收**：Inspector 里调 `search_papers("contrastive learning")` 返回 5 条真实论文，含有效 DOI。
>
> **D5 状态**：**离线等价验收已通过**——真子进程 + 真 stdio 管道 + 本地 `127.0.0.1` 假上游 + 真实抓取的 fixture，返回 `Contrastive Learning for Sequential Recommendation`（2022，DOI `10.1109/icde53745.2022.00099`）。**live 那条挂起**：OpenAlex 无 key 预算是 $0.10/天且按出口 IP 共享，本项目已耗尽两次，最后一次 `retryAfter: 66013`（≈18.3h，UTC 午夜重置）。要真跑一次，要么等重置要么配 `OPENALEX_API_KEY`。

---

## D6 · 1 天｜接进 Agent + 引用渲染

**后端**：agent 通过 MCP client 连上 literature-search，把三个工具注册进 `ToolNode`。

**定义引用的结构化契约**（前后端共用，这个定了 Week 2 才好做 grounding）
```python
class Citation(BaseModel):
    citation_id: str        # [1] 这种，前端锚点用
    doi: str
    title: str
    authors: list[str]
    year: int
    venue: str
    abstract: str | None
    span: SpanRef | None    # ★ Week 2 grounding 用，本周先留空但字段要占位
```
让 LLM 在正文里输出 `[1]` 形式的标记，同时返回 citations 数组。**不要把引用信息塞在文本里让前端正则解析** —— Week 2 做 span 级校验时会彻底重写。

> **D6 实测偏差**：三处，都朝「引用不可能凭空捏造」的方向改。
> ① `citation_id` 是 **`int`** 不是 `str`——网关按检索工具真实返回的顺序从 1 递增分配，前端 `[1]` 直接拿数字对齐，无需解析字符串。
> ② 不让 LLM「返回 citations 数组」。数组是**幻觉面**：模型可以写一个从没检索过的条目。改成**grounding by construction**——网关在 `bind_citations` 包装的工具返回里，把分配的 `citation_id` 注进每条 paper（`citations.py`，per-request `ContextVar` 绑定 registry），系统提示词要求「citation_id 是 3 就写 `[3]`，不要自己重新编号」。模型只能**复制**它拿到过的 id；写 `[7]` 而 registry 无 7，前端就渲染成普通文本，不成为 chip。
> ③ citations 不走正文文本，走**独立的 `citation` SSE 事件**（网关在每个 `updates` chunk 后排空 registry 新增项），前端落成 `data-citation` message part（按消息 scope，因为 id 每轮从 1 重开）。正文里只有 `[n]` 锚点，前端 `textWithChips` 正则 `\[(\d+)\]` 命中且有对应 part 才渲染 chip——「不要塞在文本里让前端正则解析」这条纪律**对引用元数据成立**，正则只碰 `[n]` 这个锚点本身。
>
> ✅ 验收的后端半程已用**假检索工具在真编译图里端到端跑通**（`test_api.py::test_chat_emits_citation_events_from_what_the_tool_returned`：citation 事件带 [1][2]、DOI、authors，`span` 恒为 null 占位）。**真论文的浏览器画面仍卡在 OpenAlex 无 key 预算**（同 D5，见坑 17）——D7 录像前必须先解这条：等 UTC 午夜重置或配 `OPENALEX_API_KEY`。

**前端**（你的主场，但**严格控制在 1 天内**）
- 引用标记 `[1]` 渲染成可点击的 chip
- 悬浮显示 title + abstract 预览
- 点击跳 `https://doi.org/{doi}`
- 右侧一个 references 面板汇总所有引用

> ✅ **验收**：问「找 5 篇 contrastive learning 的代表性论文」，流式返回正文带 `[1]…[5]` 标记，chip 可悬浮、可点击，DOI 链接真实有效。
>
> **这就是 Week 1 的 demo 画面**，D7 要录下来。

---

## D7 · 1 天｜路由 Eval + Checkpoint + 收尾

**① 路由 eval（最重要，别砍）**

凑满 100 条标注语料，`packages/evals/data/routing.jsonl`：
```json
{"query": "帮我把参考文献改成国标", "ui_context": {"panel": "references"}, "expect": "citation_format"}
{"query": "这几篇里哪篇最新", "expect": "literature_search"}
{"query": "在吗", "expect": "unclear"}
```

**必须包含三类样本**：
- 正常样本 ~60 条
- **歧义样本 ~20 条**（`expect_clarify: true`，测的是"该反问时有没有反问"）
- **负样本 ~20 条**（闲聊、越界请求，`expect: "unclear"`）← 最容易漏，但 false positive 最伤体验

跑一遍出三个数字：**Accuracy / Macro-F1 / 各意图的召回率**。写进 README。

> 这就是你简历上「路由准确率 X%」的来源。**必须是真的测出来的**，面试官会追问怎么算的。

**② Checkpoint**

```python
# 本机零服务方案（已验证）
from langgraph.checkpoint.sqlite import SqliteSaver

# Week 4 部署时切换，调用点不变
from langgraph.checkpoint.postgres import PostgresSaver   # 需要 psycopg v3，不是 psycopg2
```
`thread_id` = 会话 ID，前端已生成 UUID 存 localStorage（`getThreadId()`），并通过 `ChatRequest.thread_id` 传到后端。

> ✅ **验收**：杀掉后端进程重启，之前的对话历史还在，能接着聊。
>
> **进度**：✅ 已完成。D3 把 graph 接进 `/api/chat` 后在 HTTP 层复验，**当场抓到一个测试全绿也发现不了的 bug**：`make_checkpointer` 返回的是同步 `SqliteSaver`，而网关走 `astream` / `aget_state`，能启动、第一个请求就抛 `NotImplementedError`。测试用 `.invoke()`（同步路径）所以一路绿灯。现已改成 `AsyncSqliteSaver` + `aiosqlite`，`test_storage.py` 全部改走异步路径，postgres 分支同步改成显式抛错（它有完全一样的毛病）。详见 devlog D3 坑 2。

**③ 收尾**
- README：一段话项目定位 + 架构图（用 Mermaid，别画图工具）+ 本地启动步骤 + eval 数字
- **录一段 30 秒 demo**（前端提问 → 流式返回 → 点引用 chip → Langfuse trace 画面）
- 存到 `docs/demo-week1.mp4`

---

## 贯穿全周的三条纪律

**① 每天一次 git commit，能跑就提交**
一个月项目最大的风险不是技术难度，是 D5 卡住然后整个计划崩盘。宁可功能糙，也要链路通。

**② 每天写 3 行 devlog**（`docs/devlog.md`）
记：今天卡在哪、怎么解决的、明天要做什么。
**这是 Week 4 写技术博客的全部素材**。到 D28 再回忆"D5 那个 MCP stdout 的坑是怎么发现的"，你绝对想不起来。

**③ 遇到超过 2 小时解决不了的问题，先绕过**
在 devlog 里记一笔 `TODO: 待解决`，用 workaround 顶上，继续推进主链路。周末统一回来收拾。
**Week 1 的目标是"贯通"，不是"完美"。**

---

## 本周常见坑速查

| 现象 | 原因 | 解法 |
|---|---|---|
| MCP client 连接失败 / JSON 解析错误 | server 往 stdout `print` 了日志 | 日志全部改走 stderr |
| 前端不流式，一次性弹出 | 后端没返回 SSE，或前端 transport 不匹配 | **别用 `TextStreamChatTransport`——它只认 `text/plain`，不解析 SSE。** 要 `text/event-stream` 就自写 transport 继承 `HttpChatTransport`，只实现 `processResponseStream` |
| `InvalidUpdateError: concurrent update` | 并行节点写同一 state key 但没写 reducer | `Annotated[list, your_reducer]` |
| Langfuse 里 trace 断裂 | callback handler 没传进子调用 | 用 `config={"callbacks": [handler]}` 一路透传 |
| Postgres 连接失败 | 用了 `psycopg2` 而 LangGraph 需要 `psycopg`(v3) | `uv add "psycopg[binary,pool]"` |
| pgvector 扩展不存在 | 用了普通 postgres 镜像，或 Windows 原生 PG 不带该扩展 | Docker 用 `pgvector/pgvector:pg17`；本机开发用 `sqlite-vec` |
| Windows 上想装 pgvector | 官方**无预编译包**，只能 `nmake /F Makefile.win`，需 VS C++ 工具链 | 本机没有 VS Build Tools → 不装，走 sqlite-vec |
| `postgres.exe` 退出码 `-1073741515` | `0xC0000135` = DLL 缺失。本机是 `icudt67.dll`（28MB ICU 数据）在 C→D 搬迁中丢失，`icuuc67/icuin67` 连带加载失败 | 逐个 `LoadLibrary` 依赖定位；本项目选择**不修**，全走 SQLite |
| `net start` 报「系统找不到指定的文件」 | 服务 `ImagePath` 仍指向搬迁前的 `C:\Program Files\PostgreSQL\16` | `sc qc <服务名>` 看真实路径；本项目不修 |
| Git Bash 里原生 exe 报 `error while loading shared libraries` | MSYS 的报错文案会误导（`cmd.exe` 自己也这么报） | 用 `powershell` 跑，看 `$LASTEXITCODE` 拿真实 Win32 状态码 |
| SQLite `bm25()` 分数是负数 | FTS5 的 bm25 越负越相关，非经典正分 | `ORDER BY bm25(t)` 升序；RRF 只用排名，无需转换 |
| DeepSeek「不调用工具」，只输出文本 | **`reasoning_content` 的推理 token 计入 `max_tokens`**。预算太小时模型把额度全烧在推理上，来不及发 tool call | 把 `max_tokens` 放到 2048+；判断依据看 `finish_reason`，是 `length` 就是截断而非能力问题 |
| DeepSeek 模型名写错但没报错 | `deepseek-chat` / `deepseek-reasoner` 是**未在 `/models` 列出的遗留别名**，仍然返回 200 | 以 `GET /models` 为准（当前只有 `deepseek-flash`、`deepseek-v4-pro`） |
| OpenAlex 频繁 429 | 没带 `mailto` 参数 | 请求里加 `mailto=你的邮箱`，进 polite pool |
| Windows 下容器路径挂载失败 | 反斜杠 / 盘符问题 | compose 里统一用相对路径 + 正斜杠 |
| `uv add` 报 `拒绝访问 (os error 5)` 删不掉 `.pyd` | 正在运行的 uvicorn 锁住了 DLL（Windows 特有） | **先停 dev server 再装依赖**，装完重启 |
| `uv sync` 之后 import 不到自己的包 | workspace 根是 virtual project，成员没被装 | 永远用 `uv sync --all-packages` |
| `uv pip list` 出现同一个包两个版本 / 缺 `RECORD` | 交替使用 `--package api` 与 `--all-packages`，两者对 websockets 的约束冲突，uv 反复装卸撞文件锁 | 重建：`rm -rf .venv && uv sync --all-packages`；**平时一律 `uv run --no-sync ...`** |
| `curl -d '{...}'` 报 `error parsing the body` (400) | Git Bash 处理含中文/嵌套引号的内联 JSON 有问题 | 写进文件，用 `--data-binary @payload.json` |
| browser-use `click` 报 `VIEWPORT_UNAVAILABLE` | 内置浏览器视口不可见，指针操作全废 | 改用 `evaluate_script`；React 受控输入必须用 native setter + dispatch `input` |
| 冒烟测试看不到 `/api/chat` | FastAPI 0.143 的 `include_router` 生成 `_IncludedRouter`，不展平进 `app.routes` | 用 `app.openapi()['paths']` 检查路由 |
| ruff 想改 `docs/*.md` 里的代码块 | ruff >= 0.17 支持格式化 Markdown 里的 Python fence | 根 pyproject 设 `[tool.ruff] extend-exclude = ["docs"]` |
| `Path(key).name` 削不平 blob key，最后炸一个无关的 `PermissionError` | **`Path("..").name == ".."`、`Path(".").name == "."`**，原样返回，`dest` 指向 root 或其父目录 | 写 `_safe_name()` 显式拒 `""` / `"."` / `".."`；**顺带拒 Win32 保留设备名**（`CON` `NUL` `COM1` `LPT9`），它们会被路径解析器重定向到设备，无控制头的服务里可能挂死请求而非报错 |
| Next 16 构建报 `unstable value Math.random() in a Client Component` | `cacheComponents: true` 禁止预渲染里出现不稳定值；`useChat` 在没给 `id` 时会自己 `Math.random()` 造一个 | 用 `<Suspense fallback={...}>` 包住该 Client Component，让它变成每请求动态渲染（官方 `[stream]` 方案） |
| eslint 报 `react-hooks/refs`：渲染期间不能写 ref | React 19 新规则。原意是让「只建一次」的 transport 读到最新状态 | **别用 ref**：`sendMessage({text}, { body: {...} })`，`ChatRequestOptions.body` 逐请求带动态值 |
| eslint 报 `react-hooks/set-state-in-effect` | React 19 禁止在 effect 里同步 setState；`useEffect(() => setMounted(true), [])` 这个老套路违规 | 读 localStorage 这类外部可变存储改用 **`useSyncExternalStore(sub, get, getServerSnapshot)`**，服务端快照返回 `""`，天然无 hydration mismatch，`mounted` 标志可删 |
| `import { useChat } from "ai"` 报找不到 | `useChat` 不在 `ai` 的根导出里 | 装 **`@ai-sdk/react`**。版本号别慌：`@ai-sdk/react@4.x` 精确依赖 `ai@7.x`，两个包各自独立编号 |
| SSE 解析器漏事件 / 收到莫名的空消息 | `sse-starlette` 的 `DEFAULT_SEPARATOR` 是 **`\r\n`**，且 15s keepalive ping 走**注释行**（`:` 开头） | 边界同时容忍 `\r\n\r\n`/`\n\n`/`\r\r`；跳过 `:` 行；多行 `data:` 用 `\n` 拼接；冒号后只剥一个空格 |
| 中文在 SSE 里变成 `\uXXXX`，字节数翻三倍 | `json.dumps` 默认 `ensure_ascii=True` | `json.dumps(..., ensure_ascii=False)`，并加测试钉住 `b"\\u" not in body` |
| 浏览器里的计时/中断测试结论不可信 | **后台标签页定时器被节流到 ~1s**，叠加自动化工具单次调用约 10s 延迟，短流早就跑完了 | 测「逐字出现」改在**网络层插桩**（包 `fetch` + `res.body.tee()` 记录每个分片到达时刻）；测「流中途」行为就把提交+等待+点击+复查**全塞进一次 `evaluate_script`**，结果存 `window.__x` 再单独读回 |
| 起了第二个 `next dev` 报 `Another next dev server is already running` | Next 16 不允许同一目录跑两个 dev server | `taskkill /PID <pid> /F`。注意**杀掉 CLI 包装进程不等于杀掉服务进程**，`netstat -ano` 看谁还占着端口，用 `Get-CimInstance Win32_Process` 确认命令行再杀 |

---

## Week 1 结束后你应该拥有的东西

一个**能演示、有 trace、有 eval 数字**的细链路。

它看起来功能很少 —— 只能查文献、聊天、流式输出。但它已经具备了 Week 2–4 所有工作的**挂载点**：
- State 结构定好了 → Week 2 加 outline / evidence / verdicts
- Citation 契约里有 `span` 占位 → Week 2 grounding 直接填
- DecisionModel 抽象好了 → Week 2/3 的 Jev 批量校验复用同一个 client
- Trace 通了 → Week 4 的 eval 和成本分析有数据源
- 路由 eval 骨架有了 → Week 4 扩到全链路 eval 只是加数据集

**Week 1 的产出不是功能，是骨架的正确性。** 骨架错了，后面三周每天都在还债。

---

## 下周预告（Week 2）

State schema 扩展 → PostgresSaver 完整 checkpoint → Planner 节点 + `interrupt()` 人在环审批 → **grounding 模块（先做 50–100 条蕴含标注验证 Jev，再决定采用方案）** → verify 失败带诊断回退。
