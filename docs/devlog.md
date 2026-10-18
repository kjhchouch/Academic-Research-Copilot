# Devlog

每天三行：卡在哪 · 怎么解决 · 明天做什么。
Week 4 写技术博客时，这里是全部素材来源。

---

## 2026-10-14 · D1 · Monorepo + 基础设施骨架

**做了什么**
uv workspace（4 个成员）+ FastAPI `/health`、`/api/chat` + Next.js 16 聊天页 + 可切换存储层（SQLite / 内存 / 本地文件，零外部服务）+ docker-compose（备用，Week 4 部署）+ routing eval 种子数据 24 条。浏览器实测端到端往返通过，CORS preflight 正常，SQLite checkpointer 用真实 LangGraph 图跑通持久化往返，`tsc --noEmit` 与 `ruff check` / `format --check` 全绿。

**卡在哪 / 怎么解决**

1. **系统 Python 是 3.14，LangGraph 生态没有对应 wheel。**
   → 写 `.python-version = 3.12`，让 uv 自己下载 3.12 建 venv。**不要**试图用系统解释器。

2. **`uv sync` 装完是空的。**
   → workspace 根是 virtual project（`package = false`，无依赖），`uv sync` 只装根的依赖，四个成员一个都不装。必须 `uv sync --all-packages`。已写进 README，这是个会反复踩的坑。

3. **Docker 没装 —— 后来发现根本不需要。**
   → 机器上有个 `postgresql-x64-16` 服务但是 Stopped，而且 Windows 原生 PG 不带 pgvector。
   → 真正的解法不是装 Docker，是**把存储后端做成可切换的**：`packages/agent/src/paper_agent/storage.py` 里 checkpoint / cache / blob 各有本地实现（SQLite / 内存 LRU+TTL / 本地文件系统）和生产实现（Postgres / Redis / S3），靠环境变量选，调用点不变。
   → **D7 的 checkpoint 验收标准已提前达成**：用真实 LangGraph 图跑通了 SQLite 往返（invoke → 落盘 → `get_state()` 恢复）。Docker 降级为 Week 4 的部署问题。
   → 意外收获：装 `langgraph-checkpoint-sqlite` 时顺带进来 **`sqlite-vec`**，Week 3 的向量检索本机也能跑，不必等 Postgres。

4. **`curl -d '{...中文...}'` 在 Git Bash 里报 `There was an error parsing the body`（400）。**
   → 不是后端问题（同端点对空 message 正确返回 422）。是 Git Bash 对含中文和嵌套引号的单引号 JSON 处理有问题。
   → 用 `--data-binary @payload.json` 从文件读。**以后所有 curl 测试都走文件，别在命令行内联 JSON。**

5. **ruff 0.17 会格式化 Markdown 里的 Python 代码块。**
   → 它想重排 `docs/PLAN-WEEK1.md` 里的示例代码，把我对齐的注释全打乱。
   → `[tool.ruff] extend-exclude = ["docs"]`。教学文档里的代码可读性优先于风格一致。

6. **browser-use 的 `click` 报 `NATIVE_BROWSER_VIEWPORT_UNAVAILABLE`。**
   → 内置浏览器视口不可见（0x0），指针类操作全部失效，但 `take_snapshot` 和 `evaluate_script` 可用。
   → 改用 `evaluate_script` 驱动。**注意 React 受控输入不能直接赋 value**，必须拿 `HTMLInputElement.prototype` 的 native setter 再 dispatch `input` 事件，否则 React 收不到。
   → 另外别在 `evaluate_script` 里调 `location.reload()`，会销毁脚本上下文报 "Inspected target navigated or closed"，要用 `navigate_page`。

7. **`uv add` 报 `拒绝访问 (os error 5)`，删不掉 `websockets/speedups.cp312-win_amd64.pyd`。**
   → 正在运行的 uvicorn 把 DLL 锁住了，Windows 特有（POSIX 下可以删除已打开的文件）。
   → **先 `taskkill` 停 dev server，再装依赖，装完重启。** 以后所有 `uv add` 前都要先停服务。

8. **★ 上面那次失败把 `.venv` 装坏了，而且暴露了一个会长期咬人的坑。**
   → 症状：`uv pip list` 里同时出现 `websockets 16.1.1` 和 `17.2`，且 17.2 的 dist-info 缺 `RECORD` 文件，uv 报 "Installation may result in an incomplete environment"，之后每次 `uv run` 都失败。
   → **根因不只是文件锁**：`uv run --package api` 只解析 api 的依赖，`uv sync --all-packages` 解析全 workspace 并集，两者对 websockets 的约束不同（langgraph 要 `<17`，uvicorn[standard] 允许 `>=17`）。交替使用会让 uv 反复装卸同一个包 —— 在 Windows 上迟早撞锁。
   → **修复**：`rm -rf .venv && uv sync --all-packages` 重建（`uv.lock` 保证可复现）。
   → **新纪律**：`uv sync --all-packages` 只在依赖变更时手动跑，**平时一律 `uv run --no-sync ...`**。已写进 README 和坑速查表。

9. **`Path.as_uri()` 对相对路径抛 `ValueError: relative path can't be expressed as a file URI`。**
   → `LocalBlobStore` 初始化时 `Path(root).resolve()`，返回值直接用 `str(dest)`。顺手写了路径穿越测试：key 传 `../../escape.txt` 必须被 `Path(key).name` 削平，留在 root 内 —— 已验证逃不出去。

10. **FastAPI 0.143 的 `include_router` 不再把路由展平进 `app.routes`。**
    → 里面变成一个 `_IncludedRouter` 对象，导致我用 `[r.path for r in app.routes]` 做冒烟测试时看不到 `/api/chat`，一度以为路由没注册。
    → 实际 openapi 和真实 POST 都正常。**检查路由要用 `app.openapi()['paths']`，不要遍历 `app.routes`。**

11. **★ 本机其实装了 PostgreSQL 16，但它已经起不来了 —— 而修它并不是正解。**
    → 起因：用户提到 `D:\PostgreSQL\16`。如果能用，Week 3 就有真 pgvector，`CHECKPOINT_BACKEND` 也能换成生产的 `PostgresSaver`。
    → 表层现象：`net start postgresql-x64-16` 报「系统找不到指定的文件」。`sc qc` 一看，服务 `ImagePath` 指向 `C:\Program Files\PostgreSQL\16\bin\pg_ctl.exe` —— 而 `installation_summary.log` 证实原始安装确实在 C 盘，**整棵树后来被搬到了 D 盘，但服务登记和 `data/postmaster.pid` 都没跟着改**。
    → 真正致命的是另一层：`postgres.exe` 退出码 `-1073741515` = `0xC0000135` = DLL 缺失。我用 PowerShell 逐个 `LoadLibrary` 它的依赖做二分，定位到 `icuuc67.dll` / `icuin67.dll` 失败（win32 err 126），根因是它们依赖的 **`icudt67.dll`（约 28MB ICU 数据库）在整棵树、整个 C 盘、System32 里都不存在**。同目录其他 ICU DLL 时间戳都是原始的 Sep 1 16:50 —— 搬迁时唯独漏了这个大文件。ICU 严格锁主版本，D 盘上找到的 `icudt71/73/75.dll` 都不能替。
    → **踩坑**：Git Bash 里跑原生 exe 报的 `error while loading shared libraries: liblz4.dll` 是**误导性的**。同一环境下连 `cmd.exe` 都这么报，而 `liblz4.dll` 用 `LoadLibrary` 一试完全正常。MSYS 的报错文案不可信，**必须用 PowerShell 拿 `$LASTEXITCODE` 换真实 Win32 状态码**。
    → **决定不修**（用户拍板）。理由不是修不了，是**修了也没用**：`lib/` 里没有 `vector.dll`、`share/extension/` 里没有 `vector.control`，pgvector 压根没装。查了官方 README —— **Windows 无预编译包**，只有 `nmake /F Makefile.win` 源码编译这一条路，而本机没有任何 Visual Studio / `cl.exe` / `nmake.exe`。装 Build Tools（2-7GB）再编译要半天到一天，而 Week 3 的价值全在检索**设计**（hybrid → RRF → rerank → 质量自评估）上，不在向量存在哪个引擎里。
    → 替代方案实测通过：SQLite 3.50.4 + `sqlite-vec` 0.1.9（`vec0` 虚表 KNN 排序正确）+ FTS5 `bm25()`。**意外收获**：SQLite 的 `bm25()` 返回负数（越负越相关），不是经典正分 —— 但 RRF 只消费排名不消费分值，所以无害。这反过来印证了之前「用 RRF 而不是加权分数融合」的判断：BM25 无界、余弦在 `[-1,1]`，直接加权本就脆弱。
    → 全程只做了只读诊断，没有改动 PG 安装。

12. **接上 DeepSeek key，顺手挖出两个会在 D3/D4 咬人的坑。**
    → key 只写进 `.env`（已确认被 `.gitignore:22` 排除）。`config.py` 里改用 `SecretStr`，实测 `repr(settings)` 和 `model_dump()` 都渲染成 `**********`，不会顺着日志或 Langfuse trace 漏出去。
    → **没有给 DeepSeek 单开一个 provider 分支**：它是 OpenAI 兼容的，所以配置形状是「一个 `LLM_BASE_URL` + 一个 client」，换 OpenAI/OpenRouter/本地 Ollama 只改 base_url，调用点不动。同时删掉了 `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` / `DEFAULT_CHAT_MODEL` 三个占位字段——没人引用，留着只会让人以为有多 provider 抽象。
    → 验证做了四件事：`/user/balance` 200、普通对话 200、**工具调用**、以及一条「不该触发工具」的对照。工具调用完全正确：`format_citation` 的参数、`gb-t-7714` 枚举值、8 位作者、年份全对；对照组「你好你能做什么」正确返回纯文本，**没有过度触发**。Fast Path 的地基是稳的。
    → **坑 A：`GET /models` 只返回 `deepseek-flash` 和 `deepseek-v4-pro`。** 我一开始按习惯配了 `deepseek-chat` / `deepseek-reasoner`，两个都返回 HTTP 200 且能正常工具调用——**它们是未列出的遗留别名，不会报错，所以配错了也不知道**。以后一律以 `/models` 为准。
    → **坑 B（更贵）：`deepseek-v4-pro` 第一次测工具调用时没触发**，只输出文本。差点判定成「v4-pro 不支持工具调用」。真实原因是我把 `max_tokens` 设成了 120，而**两个模型都产出 `reasoning_content`，推理 token 是计入 `max_tokens` 预算的**——额度被推理烧光，根本来不及发 tool call。放到 1000 之后 2/2 次都正确调用，`finish_reason=tool_calls`。
      **教训：判断「模型不调工具」之前先看 `finish_reason`。是 `length` 就是截断（配置问题），是 `stop` 才是真的没调用（能力/prompt 问题）。** 已把 `LLM_MAX_TOKENS=2048` 写进配置。
    → 顺带发现 `deepseek-reasoner` 那次返回了 `cache_hit=128`，说明重复前缀会命中 prompt cache（命中价约为未命中的 1/50）。系统提示词和工具 schema 要保持**前缀稳定**，别在里面塞时间戳之类会变的东西，否则缓存全废。
    → `/health` 加了 `llm` 段：只暴露 base_url、两个模型名和 `key_present` 布尔值，**不暴露 key**。D3 排查时一眼能分清「凭据没加载」和「上游调用失败」。

13. **补上测试（D1 收尾），第一版就抓到一个真 bug。**
    → 加了 4 个测试文件（storage / config / api / routing 数据），`testpaths` 补上 `packages/agent`。**101 passed, 1 skipped in 0.9s**，ruff 全绿。
    → **真抓到 bug**：`LocalBlobStore` 用 `Path(key).name` 削平 key，我以为这样就逃不出 root 了。错 —— **`Path("..").name == ".."`，`Path(".").name == "."`**，根本没被削平，`dest` 直接指向 root 或它的父目录，最后在 `write_bytes` 里炸一个跟 key 毫无关系的 `PermissionError`。
      修法是加 `_safe_name()`：显式拒 `""` / `"."` / `".."`，顺带拒 **Win32 保留设备名**（`CON` `NUL` `COM1` `LPT9`…）。后者不是理论问题：Windows 的路径解析器会把这些名字重定向到设备，`open("CON","w")` 会挂到控制台上，**在没有控制头的服务里可能把请求挂死而不是报错**。
    → **顺手修了依赖归属**：`langgraph` / `langgraph-checkpoint-sqlite` 原本声明在 `apps/api`，但真正 `import` 它们的是 `packages/agent/storage.py`。之前能跑只是因为 workspace venv 装的是全集。移到 `packages/agent`，`apps/api` 靠 `paper-agent` 传递依赖。改完确认 venv 无抖动（websockets 仍只有 16.1.1）。
    → **两个失败，判断方向相反，这是这次最值钱的部分**：
      - `test_ambiguous_rows_ask_instead_of_guessing` 挂在「我的参考文献格式不对」那行 → **是测试太严，不是数据错**。`expect: citation_format` + `expect_clarify: true` 编码的是「意图已知、槽位缺失」，这是第二种合法的歧义模式。放宽测试，并另加一条测试**要求两种模式都必须存在**。
      - `test_every_boundary_case_explains_itself` 挂在「在吗 / 你是谁开发的 / 今天天气怎么样」→ **这次是数据错**，故意不放宽测试，给三行补上 `note`。边界用例不写理由，三个月后没人知道它为什么在那儿。
    → **撤回一个我自己编的坑**：我一度断言「空字符串 `SecretStr('')` 是 truthy，会让 Langfuse/Jev 被静默启用」，还准备写 validator。**实测是错的** —— `bool(SecretStr(''))` 就是 `False`。没加那个 validator。教训：写防御代码前先跑一遍。

---

## 2026-10-14 · D2 · SSE 流式

**做了什么**
`/api/chat` 从 JSON 回显改成 SSE 流（`sse-starlette` 3.5.0），假生成器分片推送，不接 LLM。前端换成 Vercel AI SDK v7 的 `useChat` + **自写的 `SseChatTransport`**。浏览器实测通过：34 个网络分片、间隔约 40ms、连接持续 1379ms，Network 面板 `mimeType=text/event-stream`；停止按钮实测能真中断（分片数冻结在 79，`eof` 始终没到），且不报错。Python 101 passed / ruff 全绿 / `tsc --noEmit` 与 `eslint --max-warnings=0` 全绿。

**定的协议（D3 会往上加事件）**

```
event: token   data: {"text": "..."}     一个文本增量
event: done    data: {}                  正常结束（客户端把「没有 done」判为截断）
event: error   data: {"message": "..."}  失败，之后不再有 done
```

**卡在哪 / 怎么解决**

1. **★ 昨天给 D2 定的方案是错的：`TextStreamChatTransport` 根本不解析 SSE。**
   → 装了 `ai@7.0.140` 之后去读 `.d.ts` 才发现：`TextStreamChatTransport.processResponseStream` 把响应体当 **`text/plain`** 直接切成 text delta，看见 `event:` 行只会当成正文。它跟验收标准里写的 `text/event-stream` 是互斥的两条路。
   → `DefaultChatTransport` 能解析，但要求 **Python 端吐 AI SDK 内部的 `UIMessageChunk` 格式**——那是个 TS 包的私有协议，跨版本会变，而且网关根本不该依赖它。
   → **最终方案：自定义 transport。** `HttpChatTransport` 是抽象类且**运行时有导出**（`typeof ai.HttpChatTransport === 'function'`，先验过再动手），只需实现 `processResponseStream` 一个方法，请求构造 / abort / headers / 非 2xx 全部继承。协议知识只存在于一个有类型检查的 TS 文件里，Python 端保持稳定简单的自有协议。**这个「网关不学前端 SDK 的私有格式」的边界，面试时是个能讲的点。**

2. **`useChat` 不在 `ai` 包里，在 `@ai-sdk/react`。**
   → `ai` 的根导出里没有 `useChat`（只在注释里出现）。要单独装 `@ai-sdk/react`。
   → 版本号看着像装错了：`@ai-sdk/react@4.0.143` 对 `ai@7.0.140`。**查了 `dependencies` 才放心**——它精确依赖 `ai: 7.0.140`，两个包只是各自独立编号。别看到数字差很大就以为版本冲突。

3. **★ Next 16 的 `cacheComponents` 把 `useChat` 判成构建错误。**
   → 报错：`Next.js encountered the unstable value Math.random() in a Client Component`，指向 `useChat(...)` 那一行。根因是我传了 `id: threadId || undefined`，SSR 阶段 `threadId` 是空串 → `undefined` → **useChat 内部用 `Math.random()` 自己造 id**，而 `cacheComponents: true` 禁止在预渲染里出现不稳定值。
   → 按官方给的 `[stream]` 方案修：在 `page.tsx` 用 `<Suspense fallback={骨架}>` 包住 `ChatPanel`。这样它变成**每请求动态渲染**而不是被冻进静态 HTML，`Math.random()` 就合法了。骨架屏顺带成了真的 loading 态。
   → **`apps/web/agents.md` 早就警告过「This is NOT the Next.js you know，写代码前先读 `node_modules/next/dist/docs/`」。这次算是验证了它没开玩笑。**

4. **React 19 的两条新 lint 规则，各逼我改了一次设计。**
   → `react-hooks/refs`：我原本写 `uiContextRef.current = uiContext` 在渲染期同步 ref，好让只建一次的 transport 能读到最新 `ui_context`。**报错：渲染期间不能写 ref。**
     正解不是塞进 `useEffect`（那会慢一帧），而是**根本不用 ref**：`ChatRequestOptions` 有 `body` 字段，`sendMessage({text}, { body: { ui_context } })` 逐请求带上，读的就是点击那一刻的状态。更简单也更准。
   → `react-hooks/set-state-in-effect`：`useEffect(() => setMounted(true), [])` 这个老套路被判违规（同步 setState 触发级联渲染）。
     正解是 **`useSyncExternalStore(subscribe, getThreadId, () => "")`**：SSR 和 hydration 都用服务端快照 `""`，之后 React 自己拿客户端值重渲染，**天然没有 hydration mismatch**，`mounted` 标志整个删掉。读 localStorage 这类「外部可变存储」本来就该用它。

5. **`id` → `thread_id` 的别名，是给 D7 留的缝。**
   → SDK 管会话 id 叫 `id`，LangGraph 的 checkpointer 要的键叫 `thread_id`。在 `ChatRequest` 上直接 `Field(alias="id")` + `populate_by_name`，**D7 接 checkpointer 时那一行 diff 是零**。
   → 顺便验了一件容易错的事：`useChat` 的内部 `Chat` 实例是 `useMemo(..., [options.id])`，所以 hydration 后 id 从 `""` 变成真 UUID 会**重建一次**。安全，因为那发生在任何消息存在之前。这是读源码确认的，不是猜的。

6. **传输层两个必须显式处理的细节。**
   → **`ensure_ascii=False`**：`json.dumps` 默认把中文转成 `\uXXXX`，字节数翻三倍，DevTools 里还全是转义看不懂。已加测试钉住（`b"\\u" not in r.content`）。
   → **按码点切片，不按字节**：`reply[start:start+4]` 切的是 Python str 的码点，所以多字节汉字永远不会被拦腰截断。前端另外用 `TextDecoder(..., {stream:true})` 兜住「一个 UTF-8 字符跨两个 HTTP 分片」的情况——两层各管一件事。
   → **SSE 分隔符是 `\r\n` 不是 `\n`**（`sse-starlette` 的 `DEFAULT_SEPARATOR`）。解析器按规范同时容忍 `\r\n\r\n` / `\n\n` / `\r\r`，跳过 `:` 开头的注释行（**15s keepalive ping 就是走注释行**），多行 `data:` 用 `\n` 拼接，冒号后只剥一个空格。Python 测试里**故意独立重写了一份解析器**而不共用代码：两边对同一串字节达成一致，才说明协议是真定义清楚了。

7. **★ 浏览器里的计时测试不可信，踩了三次才想明白。**
   → 现象：想用 `setInterval(50ms)` 采样 DOM 证明「逐字出现」，4 秒只采到 6 个点；想用 `setTimeout(420ms)` 点停止按钮，结果按钮还没出现（`chunksAtStop: 0`）。
   → 根因两条叠加：**后台标签页的定时器会被节流到 ~1s**，以及 **MCP 单次调用本身有约 10s 延迟**——我 3000 字的流要跑 34s，可两次 MCP 调用之间就已经过去了。
   → 正解：**别用页面定时器测，改成在网络层插桩。** 包一层 `window.fetch`，把 `res.body.tee()` 的一路拿来记录每个分片的到达时刻。这拿到的是硬证据：34 个分片、间隔 0/38/79/120…ms、总跨度 1379ms。
   → 要测「流中途」的行为，就**把整段编排塞进一次 `evaluate_script`**（提交 + 3s 后点停止 + 6s 后复查，结果存到 `window.__ab`），再单独一次调用把结果读回来。跨 MCP 调用的时序一律不要指望。
   → 后端时序另外用 `httpx.stream` 独立测了一遍做对照：17 个 token 事件、平均间隔 **43.3ms**，和配置的 `CHUNK_DELAY=0.04` 对得上。**浏览器测出来的数要和后端直接测的数互相印证。**

8. **abort 路径本来藏着一个我猜错的坑，实测才发现没事。**
   → 我担心：用户点停止 → 流被取消 → 我的 transport 收不到 `done` → `flush` 里那句「没有 done 就是截断」会误报错误。
   → 实测 `errBanner: false`，**没有误报**。原因：取消一个 `TransformStream` 管道走的是 cancel 路径，**`flush` 只在 writable 端正常 close 时才调用**，所以那句兜底不会触发。分片数冻在 79、`eof` 始终未到、状态回到 `ready`、按钮翻回「发送」，全部正确。
   → 教训：这种「我以为会 bug」的地方，**要么测掉，要么别写**。这次是先写了兜底再测，运气好是对的；下次可能就不是。

9. **测试套件的一个设计取舍：把假生成器的节奏做成模块常量。**
   → `CHUNK_SIZE` / `CHUNK_DELAY` 提到模块级，测试里用一个 autouse fixture 把 `CHUNK_DELAY` 打成 0。否则每条流式测试都要真等 1 秒多，套件会从 0.9s 涨到 10s+。**节奏是给浏览器演示用的装饰，协议本身不依赖它**，所以可以安全地在测试里关掉。
   → 最有价值的一条测试是 `test_chat_stream_is_chunked_not_buffered`（断言 token 事件数 > 1）。如果哪天生成器被不小心收集成一整个响应，**其余测试全都会照常通过**——文字照样对、`done` 照样在——只有这条会发现「流」退化成了「一次性返回」。

**明天的事（D3 · LangGraph + 真 LLM）**
- `uv add langchain-openai langfuse`（**先停 uvicorn**，见 D1 坑 7），把假生成器换成真实 agent 流
- 接 DeepSeek：`deepseek-flash` 走 Fast Path，`max_tokens` 别小于 2048（见 D1 坑 12 的 `reasoning_content` 陷阱）
- 协议加事件：`route`（意图判定结果）/ `tool`（工具调用起止）/ `citation`。前端 transport 已经**忽略未知事件名**，所以后端可以先加、前端后接
- Langfuse key 还没申请，trace 先空着

---

## 2026-10-14 · 计划外 · vendor 第三方 skill 并处理许可

用户提出用 [Nature Skills](https://github.com/Yuan1z0825/nature-skills) 做主要科研能力。核实后判定**可以用**，且这次不是 pi / Hermes 那类错误：**skill 是知识，不是架构**。不从零重写 Nature 风格指南和统计报告规范是称职的选择；必须自研的是路由、grounding、评测——skill 包替代不了这三件事。

上游 23 个 skill，vendor 6 个（`nature-shared` / `nature-polishing` / `nature-statistics` / `nature-social-science` / `nature-ref-verifier` / `nature-response`），996 KB / 123 文件，落在 `skills/third_party/`，配 `LICENSE` + `NOTICE` + `PROVENANCE.md`。

**意外收获（比 vendor 本身更值钱）**

1. **`nature-statistics` 直接顶替了我们原计划手写的 `statistics-reporting` skill。** 而且它的 README 明写覆盖「**跨章节数值一致性**」——这就是同一天我刚提议的第三个 KPI「数字保真度」，上游已经实现了。
2. **`nature-shared/scripts/check_consistency.py`（272 行）是数字保真度的现成参考实现**：纯 stdlib（`Decimal` / `re` / `dataclasses` / `argparse`），术语组一致性 + 单位换算（um/mm/cm/m）+ 数值一致性，输出结构化 `Finding` 与 JSON。**零 LLM 调用、零 token 成本**——在 DeepSeek 余额只剩 ¥3.14 的情况下这点很实际。
3. **`nature-ref-verifier`（Stable）是 Citation Precision 的现成机械层**：逐条多源交叉验证，逐字段比对作者/标题/年份/卷期/页码，标记卷年冲突、**作者编造**、页码漂移。
4. **D5 的阻塞被解掉了。** 上游 README 确认 CrossRef / arXiv **不需要 API key**，PubMed 只要一个联系邮箱（`NCBI_API_KEY` 仅用于提速率）。**不必再等 Semantic Scholar 的 key。**
5. **`nature-social-science`（277 KB，零代码）正中「提供思路」**，而且受众对得上：明确覆盖「找论文题目/研究问题」「区分『研究少』与真正未决的知识问题」「开题报告、学位论文的选题阶段与论证自检」。

**坑 / 判断**

6. **★ `extend-exclude` 挡不住显式传路径。** 根 `pyproject.toml` 原本只有 `extend-exclude = ["docs"]`；加上 `skills/third_party` 之后，`ruff format .` 确实跳过了它——但 `ruff format skills/third_party/` **照样报「3 files would be reformatted」**，`ruff check` 找到 12 个错误并声称 8 个可 `--fix`。编辑器的 format-on-save、pre-commit hook、任何传显式路径的 CI 都会绕过排除。
   → 修法是 `[tool.ruff] force-exclude = true`。加完实测：显式传 vendored 路径变成「No Python files found」，而显式传我们自己的文件照常检查。
   → **为什么这条要命**：`NOTICE` 里声明这些是 verbatim copies。`ruff format` 改一个空格就让这句声明变成假的，并触发 Apache-2.0 §4(b)「修改过的文件必须显著声明改动」的义务——而我们根本没打算改。两个配置项都写了测试守着。
7. **★ 上游用 `../nature-shared/core/*.md` 相对路径引用共享材料，共 12 条。** 只在六个目录**互为兄弟**时才成立。目录结构一变深，失败发生在 **prompt 组装期而不是 import 期**——不报错，只表现为输出质量下降。上游 README 也明写「请复制或引用整个技能文件夹，而不是只复制 `SKILL.md`」。
8. **安全审查用「已审查文件白名单」而不是「危险模式黑名单」。** 5 个 `.py` 全读过：纯 stdlib，无网络 / `subprocess` / `eval` / `exec` / 环境变量读取。黑名单扫描只能证明「我认识的危险模式没出现」；白名单能证明「这里没有我不认识的文件」。测试里两个都做了，白名单是主门禁。
   → 四条门禁全部做过**反向验证**（新增未审查 `.py` / 已审查文件混入 `import requests` / 改名共享文件制造悬空引用 / 混入 `install.sh`），确认每条都真的会 fail。**一个从不失败的测试等于没有测试。**
9. **`nature-academic-search/install.sh` 是「第三方 skill = 任意代码执行」的教科书标本**，留档在 PROVENANCE.md，永不执行：不 pin 版本 `pip install mcp requests toml lxml`；自动改写 `~/.claude/.mcp.json` 并把自己塞进 `settings.json` 的 `enabledMcpjsonServers`（**静默自我启用，用户全程没批准过任何事**）；`PUBMED_EMAIL` 从 shell 参数直插 Python heredoc 与生成的 JSON；工具表含 `search_google_scholar`（上游自述 **scraped**）与需机构订阅的 Scopus / WoS。
   → 对比 pi 的供应链姿态（exact pin / `min-release-age=2` / `--ignore-scripts` / lifecycle-script allowlist），差距一目了然。这段可以直接进安全博客。
10. **`nature-downloader` 明确排除**：上游描述是「通过图书馆资源入口、**Chrome 登录态**和开放获取路径合法获取学术全文/PDF」，触发词含 CARSI、Web of Science。借用用户已认证的机构会话批量取付费全文，产品化在法律与伦理上都不可接受。它还是 MIT 的第三方贡献（`Copyright (c) 2026 baihe26`），会连带继承责任。
11. **不 vendor 第二个 router。** 本机 `~/.qoder/skills/` 里有个 `nature-suite`，正文就是一张关键词 → 子 skill 映射表，还写着「This skill loads first and routes」。**它与自研的 L0→Jev→embedding→LLM 四级意图级联正面冲突**，把路由换成 markdown 关键词表等于自杀。
    → 但它那张表列了 9 类意图 + 中英关键词，**正好是 `routing.jsonl` 从 24 条扩到 100 条的种子语料**。抄分类，不抄机制。
12. **本机安装副本 ≠ 上游仓库，且本机副本无 LICENSE。** 本机有 `nature-academic-search` / `nature-citation` / `nature-data` / `nature-suite`，仓库 skill 索引里没有或不同；本机顶层没有 LICENSE 文件。**来源与授权均不明，所以一个文件都没从本机拿**，全部来自 pin 住的 GitHub commit `19be8d4`。
    → 顺带：仓库内许可是**混合**的——根 Apache-2.0，但 `nature-downloader` / `nature-image2ppt` 各带自己的 MIT LICENSE，`nature-proposal-writer` / `nature-literature-pipeline` 在 frontmatter 里声明 `license: MIT`。我们 vendor 的 6 个都**没有**单独 license 字段，所以统一由根 Apache-2.0 覆盖，无混合授权问题。有一条测试专门守这个前提。
13. **Apache-2.0 的署名占位符是空的**：上游 LICENSE 结尾是模板原文 `Copyright [yyyy] [name of copyright owner]`，没填。所以版权归属只能从 README 开发者表取（**袁一哲** 创始人/维护者、**马昕瑞** 核心开发者）+ git 提交者（`老农民玩AI <mhoang12205@gmail.com>`），已写进 `NOTICE`。上游也没有 NOTICE 文件，我们自己建了一份。
    → 唯一一处需按 §4(c) 保留的文件内署名：`nature-social-science/SKILL.md` frontmatter 的 `author: community contribution, adapted from the "Finding a PhD Topic with Codex" workflow`。
14. **成熟度标签是上游自己标的，要当真**：`nature-polishing` / `nature-ref-verifier` = Stable，`nature-response` = Beta，**`nature-statistics` / `nature-social-science` = Draft**。后两个恰好是「处理数据」和「提供思路」的主力。
    → 这既是风险也是机会：把上游 Draft 级 skill 补到能用、并把改动记进 PROVENANCE.md 的修改日志，是一个真实可讲的贡献故事，比「我调了个 API」强。
15. **上游 README 声称 DeepMind 借鉴了它并推出 Science Skills**（第 105 行）。**未经核实，是作者的自述。** 别在博客或面试里当事实转述。
16. **git clone 到 github.com 会中途断流**：第一次 `--depth 1` 报 `fatal: fetch-pack: invalid index-pack output` + `unexpected disconnect while reading sideband packet`。加 `-c http.postBuffer=524288000 -c http.lowSpeedLimit=1000 -c http.lowSpeedTime=60` 重试即成功。`raw.githubusercontent.com` 在这台机器上直接返回 000，但 `api.github.com` 通。
17. **`skills/third_party/*/tests/` 不在 `testpaths` 里，是刻意的**：那是上游的测试，收进来等于把我们的套件耦合到不自维护的代码上。写了一条测试守住这个配置，防止有人图省事把 `skills` 加进 `testpaths`。

**当前门禁**：118 passed / 1 skipped / 1.0s；`ruff check` 全绿；`ruff format --check` 21 files already formatted。vendored 树经 `diff -r` 逐字节比对确认与上游 `19be8d4` 一致——所以 `NOTICE` 里那句 verbatim 是**验证过的**，不是声明。

---

## 2026-10-17 · D3 · 真 agent：LangGraph ReAct + 工具调用 + Langfuse

**做了什么**

后端从「假生成器吐固定文本」换成真的 LangGraph ReAct 图，前端把工具调用画出来，可观测接上 Langfuse Cloud。新增 6 个模块：

- `paper_agent/llm.py` —— `ChatOpenAI` 指向 DeepSeek 的 OpenAI 兼容端点，`deepseek-flash` 跑 Fast Path。
- `paper_agent/graph/{state,nodes,builder}.py` —— `AgentState` + agent 节点 + `ToolNode`，`tools_condition` 决定是回工具还是收工。
- `paper_agent/tools/common.py` —— `count_words`（中文按字、西文按词），第一个真工具。选它不是因为有用，是因为**它的返回值可被机械验证**：13 个中文字符就是 13，模型编不出来，测试也不用断言自然语言。
- `paper_agent/testing.py` —— `ScriptedChatModel` + `tool_call`，把假模型收进包里而不是塞在测试文件中。
- `paper_agent/prompts/system.py` —— 系统提示词，含「辅助不代写」的产品边界。
- `paper_agent/observability.py` —— Langfuse 封装。

`chat.py` 换成 `astream(stream_mode=["messages","updates"])`，新增 `tool` 事件（`status: start|end`）；`sseTransport.ts` 把它翻成 AI SDK v7 的 `dynamic-tool` part；`ChatPanel.tsx` 渲染 `ToolChip`（●运行中 / ✓成功 / ✕失败）。

**Langfuse 是本地优先的**：没有 key 就完全不初始化、不发网络请求、不影响任何功能。`/health` 多了 `tracing: {langfuse, host}`，一眼能看出当前进程到底在不在上报。

**坑 / 判断**

1. **★ `messages` 流模式会把 `ToolMessage` 一起吐出来。** `stream_mode=["messages","updates"]` 的 `messages` 通道给的是**所有**经过的消息，包括 tools 节点产出的 `ToolMessage`。第一版按 `langgraph_node != "tools"` 过滤，工具返回的 JSON 照样以 token 形式喷到前端，用户看到的是 `{"cjk_characters": 13, ...}` 混在自然语言里。
   → 正解是**按类型过滤**：`isinstance(chunk, AIMessageChunk)`。按节点名过滤是脆的——节点一改名就静默失效，而且它表达的是「哪个节点」而不是「这是什么」，后者才是这里真正要问的问题。
   → 回归测试 `test_chat_does_not_stream_tool_output_as_text` 断言的是**具体字符串不出现**（`cjk_characters` / `latin_words` / `{` / `}`），不是断言 token 数量。数量对也可能内容错。
2. **★ 同步 `SqliteSaver` 装进异步网关：能启动，第一个请求就炸。** `NotImplementedError: The SqliteSaver does not support async methods. Consider using AsyncSqliteSaver instead.`
   → **这是本次最重要的一条，因为测试全绿却抓不到。** 测试用 `.invoke()`（同步路径，`SqliteSaver` 支持得好好的），网关用 `astream` / `aget_state`（异步路径，直接抛）。**测试和应用走了两套执行模型，等于没测。** 只有真跑一次 uvicorn 才暴露。
   → 修法：`make_checkpointer` 改成 `async def`，sqlite 分支返回 `AsyncSqliteSaver` + `aiosqlite`；`aiosqlite` 显式写进 `pyproject.toml`（之前只是碰巧被传递依赖装上了——「在这个 venv 里能跑」不等于「有保证」）；lifespan 退出时关 `conn`（aiosqlite 自己起线程，不关就会在测试套件 / `--reload` 下越攒越多）。
   → `test_storage.py` 全部改成 `ainvoke` / `astream`，并加了 `test_sqlite_saver_streams` 这条专门守流式路径。持久化测试现在是**用一个 saver 写、关掉、再用第二个 saver 读**——比原来同一个实例读写更强的证明。
   → `postgres` 分支同时改成显式抛 `NotImplementedError`：`PostgresSaver` 有完全一样的毛病，接上去等于把这个 bug 原样搬到生产。README 里那行「生产用 PostgresSaver」已改。
3. **`GenericFakeChatModel.bind_tools()` 抛 `NotImplementedError`。** LangChain 自带的假模型不支持绑工具，而 ReAct 图必须 `model.bind_tools(TOOLS)`。所以自己写了 `ScriptedChatModel`。
   → 它的回复是**按输入里 `AIMessage` 的条数取模**选的。这个耦合很隐蔽：`test_chat_seeds_client_history_on_a_cold_thread` 一开始断言存了 6 条，实际 4 条——因为客户端重放的历史里已经有一个 assistant 轮，模型直接跳到最终答案，工具根本没被调用。测试里写了注释点名这层耦合，否则下一个人会以为是 checkpoint 丢消息。
4. **★ 历史归服务端管，客户端每轮全量重放。** 客户端重放整个 history，checkpointer 也存了整个 history，两边都发就会重复写。
   → 规则：**冷 thread 发全量，热 thread 只发 `history[-1:]`**，用 `aget_state` 判断冷热。
   → 之所以能幂等，是因为 **`add_messages` 按 message id 去重**——把 SDK 的 message id 原样带到 LangChain 是关键一步，id 一变就变成追加。
   → 跨进程验证过：重启 uvicorn，从 SQLite 还原 4 条消息，客户端重放的那个假首轮**没有**被再写一次（4→6 而不是 4→8），模型直接从 checkpoint 上下文答出「34」，没有再调一次工具。
5. **工具结果在网关侧解析，不在前端。** `_result_of` 对 `ToolMessage.content` 做一次 `json.loads`，失败就原样返回字符串。
   → 不然 SSE 线上会出现「JSON 文档套在 JSON 字符串里」，前端要 `JSON.parse` 两次，而且 prose 结果和 JSON 结果长得一样、只能靠试。放在网关是因为**只有网关知道这是工具输出**；失败降级成字符串也保证了非 JSON 工具不会炸。
6. **langfuse 是 5.x 不是 3.x，`CallbackHandler` 签名换了。** v5 的 `CallbackHandler(*, public_key=None, trace_context=None)` **不收凭据**，它通过 `get_client()` 拿一个进程级单例（`LangfuseResourceManager._instances`，按 public_key 索引）。v3 是直接传 public_key/secret_key/host。
   → `pyproject.toml` 写的是 `langfuse>=5` 而不是 `>=3`，并留了注释说明为什么：装了 v3 会在 `CallbackHandler()` 上炸一个 unexpected keyword argument——**至少是响的**，比静默什么都不上报好。
   → v5 基于 OTel，span 是批量攒着定时 flush 的，所以 `shutdown()` 不是可选项。短进程（测试、脚本）不关就等于什么都没发出去。
7. **trace 属性靠 LangChain config 的 `metadata` 传，不用 `update_current_trace`（v5 没这个 API）。** 保留键：`langfuse_session_id` → session、`langfuse_user_id` → user、`langfuse_trace_name` → trace 名、`langfuse_tags` → tag；普通 `thread_id` 留给 LangGraph 当 resume key；其余进普通 metadata。
   → `trace_metadata()` 里 **reserved key 最后写**。第一版 `metadata.update(extra)` 放在最后，调用方一个 `thread_id=` 就能把 session 顶掉——`test_trace_metadata_extras_cannot_shadow_a_reserved_key` 是真抓出了这个 bug，不是摆设。
8. **`auth_check()` 绝不放在启动路径上。** 它是对第三方的阻塞调用；await 在 lifespan 里意味着 Langfuse 慢或不可达时，每次 `uvicorn --reload` 都要陪着等。
   → 做成 `asyncio.create_task(_report_tracer_auth(...))`，内部 `asyncio.to_thread(verify_tracer, ...)`，成功打 INFO、失败打 WARNING。task 挂在 `app.state` 上——**没有引用的 task 会被 GC 掉**，这是个真会发生的坑。退出时 cancel。
   → 同理 `shutdown_tracer` 吞掉所有异常：关闭失败不该让进程退不干净。
9. **`CallbackHandler` 每请求新建，不共享。** 想过在 compile 时绑一个全局的，但**「这次运行属于哪个 trace」是请求的属性，一个在 lifespan 里编译一次的图不可能知道**。放 per-request config 才对。
   → 代价要写清楚：v5 handler 的 `_pending_resume_trace_contexts` 是实例级的，所以 Week 2 如果加 human-in-the-loop interrupt（一个 thread 跨多次 HTTP 请求），这里必须改成 per-`thread_id` 一个 handler。session 分组不受影响，因为它走的是 `langfuse_session_id`。
10. **★ 隐私边界必须写进模块 docstring：一条 trace 含完整 prompt 和 completion。** 开 Langfuse Cloud 就等于**用户输入的论文正文出境**。本地开发阶段只有我自己的文本，可以接受；**给别人用之前必须挂 `mask=` 钩子**做 PII / 正文脱敏。这不是以后再说的事，是上线门禁。
    → 也是「Week 4 自托管」的真正理由——不是省钱，是不想把用户未发表的手稿放在第三方。
11. **`test_observability.py` 里一个真 Langfuse client 都不建。** 建 client 会注册**进程级** OTel tracer provider，一个测试发的 span 会被后面所有测试的 exporter 捞走。这种泄漏表现为「莫名其妙的网络流量」而不是「测试失败」，最难查。
    → 真 client 路径由 live run 覆盖（见下）。测试只打桩：`_RaisingClient`（auth_check 抛 ConnectionError、shutdown 抛 RuntimeError）和 `_WorkingClient`。
12. **★ Langfuse 的旧 REST API 对新组织已经关了。** `GET /api/public/traces` 和 `/api/public/sessions` 返回 **410 `LEGACY_API_UNAVAILABLE_FOR_NEW_ORGANIZATION`**——2026-09-16 之后建的组织一律如此；单条 observation 的端点 `/api/public/observations/{id}` 也是 410，`/api/public/v2/observations/{id}` 是 404，**根本没有 `/api/public/v2/traces`**。v3 端点 2026-11-16 全面下线，服务端版本 4.63.0。
    → 能用的只有两条：`GET /api/public/v2/observations?fromStartTime=&toStartTime=`，和 `GET /api/public/v2/metrics?query=<urlencoded JSON>`（**是 GET 不是 POST**，POST 报 405）。
    → metrics 的 query schema 文档我没找到，是**靠 400 的报错信息逐层试出来的**：`dimensions` 要 `[{"field": "..."}]` 不是字符串；`filters` 要带 `"type"` 判别字段；`fromTimestamp`/`toTimestamp` 必填；`sessionId`/`userId` 属高基数维度，必须同时给 `orderBy`（方向 desc）和 `config.row_limit`。measure 名是 `inputTokens`/`outputTokens`/`totalCost` 这种，不是 `usageDetails`。**报错信息把合法枚举全列出来了，这比读文档快。**
13. **Langfuse 侧的最终验证结果（live，US region，环境 development）**：
    - **模型名有**：`providedModelName = deepseek-flash`。`/v2/observations` 列表里 `modelId: null` 只是 Langfuse 的价格表里没有这个模型（自定义 `base_url` 的 DeepSeek 匹配不上），**不是没记录**。
    - **token 用量有**：7 次 generation，input 3621 / output 414 / total 4035，平均延迟 891ms。`usageType` 还拆出了 `output_reasoning` 和 `input_cache_read`——DeepSeek 的推理 token 和缓存命中都被单独统计了。
    - **成本是 0**：因为价格表里没这个模型，算不出钱。**这不是代码问题**，修法是去 Langfuse Settings → Models 建一个 `deepseek-flash` 自定义模型定义填单价（也可以 `POST /api/public/models`）。**目前成本看板是空的，别误当成「免费」。**
    - **session / trace 名 / 环境都对**：`sessionId = <thread_id>`、`traceName = chat.fast_path`、`environment = development`。`userId = null` 是**正确的**——还没有鉴权，D8 接上后才有值。
    - **trace metadata（`ui_context`）无法通过 API 读**：新组织没有可用的 traces 端点。只能在 UI 里看，这一条**未经程序化验证**。
    - 树形结构完整：`CHAIN LangGraph → AGENT agent → GENERATION ChatOpenAI → CHAIN tools_condition → CHAIN tools → TOOL count_words → AGENT agent → GENERATION ChatOpenAI → CHAIN tools_condition`，工具调用那一轮 9 个 observation，直接回答那一轮 4 个。
14. **`ToolChip` 不是装饰。** ReAct agent 调完工具再回答，中间有一段用户什么都看不到的静默。把工具调用画出来既是体验，也是「辅助不代写」的**诚实的一半**：回答里的数字明显来自一个用户可检查的工具，而不是模型编的。
15. **`sseTransport` 里 text part 的 id 必须自增（`sse-text-${textSeq++}`）。** 工具事件会先关掉当前 text part；如果复用同一个 id，重新开的 part 会和 SDK 已经提交过的 part 撞 id。
16. **工具事件的 args 是整块到达的，不是增量。** 所以 `tool-input-start` 之后立刻发 `tool-input-available`，不需要模拟 delta 流。真要做参数级流式得等 D4 之后，而且要先确认 DeepSeek 的 tool call 分片行为。

**当前门禁**：141 passed / 1 skipped / 3.6s；`ruff check` 全绿；`ruff format --check` 29 files already formatted；`apps/web` 的 `tsc --noEmit` 与 `npm run lint` 干净。live run 两轮实测：第一轮 69 个 token 事件 + `tool start/end`（id 一致）+ `done` 收尾，state 与 `ui_context` 落进 SQLite；第二轮跨进程重启后从 checkpoint 答对，未重复调工具。

---

## 2026-10-17 · D3 补充 · 成本自己算，不用 Langfuse 的价格表

D3 验收时 Langfuse 的 `totalCost` 恒为 0，本来打算去后台建一个 `deepseek-flash` 的自定义模型定义。**查完发现这条路走不通，改成在代码里算。** 新增 `paper_agent/pricing.py`（纯函数，不依赖 LangChain）+ 网关每轮一行成本日志。

**为什么 Langfuse 的价格表不行**

1. **它的托管价格表里没有 DeepSeek。** 不是「没匹配上」，是压根没有——分页拉完 190 个条目，全是 OpenAI / Anthropic / Google，一个 deepseek / qwen / mistral 都没有。
2. **就算手写一条也表达不了真实价目。** DeepSeek 按时段计价：高峰是 **UTC 周一至周五 01:00–04:00 与 06:00–10:00，且排除中国法定节假日**，低峰恒为高峰的一半。model definition 有 `startDate` 和 pricing tiers，但没有「一天中的第几个小时」这个维度，更没有节假日日历。**不管填哪一档，误差最多 2×，而且方向随钟点变。**
3. 配置存在 Langfuse 账号里，**不进 git**：换机器、重建 project、把仓库给别人看，成本又变 0，且没人知道为什么。

它唯一能正确处理的是缓存命中/未命中分档（Langfuse 已经把它们记成不同的 `usageType`）——而这一档恰恰是最要命的，见下。

**官方现价（2026-10-17 查 api-docs.deepseek.com，USD / 1M token）**

| | deepseek-flash（V4.1-Flash） | deepseek-v4-pro |
|---|---|---|
| 输入 · 缓存命中 | 低峰 $0.003 / 高峰 $0.006 | 低峰 $0.022 / 高峰 $0.044 |
| 输入 · 缓存未命中 | 低峰 $0.15 / 高峰 $0.30 | 低峰 $0.66 / 高峰 $1.32 |
| 输出 | 低峰 $0.6 / 高峰 $1.2 | 低峰 $1.98 / 高峰 $3.96 |

**坑 / 判断**

1. **★ 2026-08-16 涨过一次价，缓存命中涨幅最高约 1114%。** 搜索命中的两篇 7 月博客给的 `$0.14 / $0.0028 / $0.28` 是**涨价前的旧数字**。抄第三方博客的价格表 = 静默算错钱。所以 `ModelPrice` 里带了 `effective` 日期和 `source` URL，**过期是可见的**，不是靠记忆。
2. **★ `cache_read` 是 `input_tokens` 的子集，不是加项。** 这条是从 `langchain_core.messages.ai.UsageMetadata` 的源码 docstring 读出来的，不是猜的：`input_tokens` 明写「Sum of all input token types」，`input_token_details` 明写「Does *not* need to sum to full input token count」。**当成加项就会把每个命中缓存的 prompt 双算。**
   → live 实测这一档有多大：一次工具调用轮的第二次 generation，`input_tokens=966` 里 `cache_read=768`，**占 79%**。全按未命中价算会把成本高报约 4×。这不是理论风险。
3. **★ `reasoning` 同理，已含在 `output_tokens` 里。** DeepSeek 思考模式的推理 token 量不小（实测一轮 174 + 56），再算一遍约等于把输出成本翻倍。`test_reasoning_tokens_are_not_billed_twice` 钉住这条。
4. **★ 自己的测试抓到一个真时区 bug。** `is_peak` 第一版写了 `utc = at if at.tzinfo is not None else at.replace(tzinfo=UTC)`——**判断了是否带时区，然后直接读 `at.hour`，没有真的转成 UTC**。传一个 `+08:00` 的 aware datetime 进去，读的是北京时间的钟点。本机就是 UTC+8，这个 bug 会把高峰窗口整体挪 8 小时。
   → 修成 `at.astimezone(UTC) if at.tzinfo is not None else at.replace(tzinfo=UTC)`。两个分支不能合并成一句 `at.astimezone(UTC)`：那样 naive datetime 会被按**本地时间**解释，正好是文档里说要避免的。
   → 顺带：`holidays` 比的是 **UTC 日期**，而 DeepSeek 说的是北京日历日，所以即使传了节假日集合，当天的头尾各 8 小时仍可能判错。写进 docstring 了，不装作解决了。
5. **设计规则：未知模型抛 `UnknownModelError`，绝不定价为 0。** 这是整个模块的存在理由——Langfuse 看板上 `$0.00` 和「忘了配价格」长得一模一样。同理，**provider 没上报 usage 时记的是 `cost unknown`，不是 `$0`**。
6. **成本日志放在 `finally` 里。** 上游 500、或者用户直接关页面，token 都已经花掉了——**那正是最想知道花了多少的时候**。
7. **按 `(model, peak)` 聚合，不逐次 generation 列。** 第一版 live 打出来是 `deepseek-flash=$0.000260, deepseek-flash=$0.000111`，看着像双算 bug。peak 留在 key 里，所以跨 04:00 / 10:00 UTC 边界的轮次仍会分成两行报——精度没丢。
8. **`format_usd` 用 6 位小数，不用 2 位。** 一轮的成本是 `$0.000366`；打成 `$0.00` 就又把「很便宜」和「没算出来」混成一件事了。第一版试过「分」为单位（`0.0211c`），可读性更差，弃。
9. **模型名取 `response_metadata["model_name"]`，不取我们请求时填的名字。** DeepSeek 接受已退役的旧名（`deepseek-v4-flash`）并路由到别的模型；用请求名计价会把这种漂移藏起来。实测返回的就是 `deepseek-flash`，与价格表 key 一致。
10. **刻意不建模的两件事**：中国法定节假日（默认空集合，落在工作日的节假日会按高峰算，**高估上限 2×、方向已知**，好过维护一张会静默腐烂的节假日表）；汇率（DeepSeek 以 USD 计价，余额可能按 CNY 显示，硬编一个每天在动的汇率不如全程 USD）。
11. **价格表是 Python 字面量，不是 TOML。** 一年改几次、和用它的算术在同一次 diff 里被 review、不需要路径解析 / 打包配置 / 解析失败分支。数据文件在这里买不到任何东西。
12. 顺手修掉一条噪音：`ui_context` 为 None 时 Langfuse 每个请求都打 `Propagated attribute 'metadata.ui_context' is None. Dropping value.`。改成 None 就整个不传。

**实测量级**：一次工具调用轮 ≈ **$0.00037**（约 570 token 输入 + 两次 generation）。按 ¥3.14 余额（≈$0.44）算够跑 **~1200 轮**——日常开发完全不是瓶颈，Week 4 的评测套件才是。

**当前门禁**：183 passed / 1 skipped / 3.3s；`ruff check` 全绿；`ruff format --check` 31 files already formatted。live run 三轮实测，成本行输出 `turn <id> cost $0.000366 (deepseek-flash=$0.000366)`，数字与手算逐项吻合。

**仍未做**：拿 DeepSeek 控制台的真实账单对一次账。官方说明写明「优先扣赠送额度」，所以「按单价算出来的数」和「余额实际减少的数」本来就未必相等——这个对账是「成本数字不是拍的」的唯一硬证据。

---

## 2026-10-18 · D4 · Jev 接入 + DecisionModel 抽象层（离线部分完成）

落地 `packages/agent/src/paper_agent/routing/` 四个模块 + 156 条新测试。`TYPESAFE_API_KEY` 还没申请，所以**全部验收都是离线的**——走 SDK 的 `transport` 假件，跑真实序列化、真实解码、真实 pydantic 校验，只换掉 socket。

**先核实工具再写集成**，这一步救了三次：

1. **差点以为官方文档是错的。** 文档示例写 `result.nouls["billing"].noul`，但反射 `SystemOneResponse` 的 `model_fields` 只有 `model / usage / answers`——`.nouls` 根本不在里面。**真相是 `.nouls / .choices / .scores / .boxes` 是 `cached_property`，不是 pydantic 字段**，所以按 `model_fields` 反射会漏掉一半 API。照反射结果写就会去读 `answers` 然后自己分类，白写一遍 SDK 已经做好的事。
2. **`.nouls` 是对 `answers` 的过滤视图，不是服务端保证的键。** 而且解码器对不认识的 answer type 是**静默丢弃**（前向兼容，只打一条 warning）。所以 `resp.nouls["path_deep"]` 在服务端漏答或 SDK 版本落后时是 `KeyError`——一个路由细节 500 掉整个请求。全部改 `.get()`，缺失就当「模型没决定」走安全默认值。
3. **`.noul` 返回 0–1 的概率，不是 bool**，且文档明说 0.5 附近是「不确定」而非「随便舍入」。所以需要一个带阈值 + 不确定带的二值化，而**两个问题的安全默认值方向相反**：`needs_clarify` 不确定 → True（猜错槽位产出的是自信的错活，用户得发现并撤销；反问只花一轮），`path_deep` 不确定 → False（重流程是多 agent 管线，且 80% 流量是窄任务，先验也指向这边）。一个模块级默认值必然对其中一个问题是错的。

**计划里两处按实测改了**：

4. **SDK 默认重试会把 800ms 预算吃成 ~3.7s。** 默认 `max_retries=2` + `api_timeout_error=True`（超时**会**被重试）+ `backoff_initial=0.5` 翻倍，实测算下来 `0.8+0.45+0.8+0.9+0.8`。而 `RetryPolicy.timeout=30.0` 那个「总重试预算」拦不住——3.7s 远在 30s 以内。**必须 `RetryPolicy(max_retries=0)`。** 一个以「快」为唯一目的调用，超时后正确反应是换后端回答，不是再等一次。除了钉常量，还加了行为测试：500 时 handler 只被调用一次。
5. **`hash(state)` 是个陷阱。** 内置 `hash()` 对 str 按 `PYTHONHASHSEED` **每进程加盐**：跨 worker 键不一致（共享 Redis 永不命中）、重启即全部失效，而本地单进程跑测试**完全看不出来**。改成 `sha256(canonical JSON, sort_keys=True)`——`sort_keys` 也是必须的，否则客户端换个 ui_context 键顺序就是一次缓存未命中，现象是「缓存好像根本没生效」。测试里钉了一个金标准 digest，脆但值得：这个失败除了看字节没有别的观测方式。

**设计上几个判断**：

6. **Protocol 签名和计划写的不一样。** 计划是 `decide(state, questions)`——那会让每个调用点自己构造 `Noul`/`Choice`，等于全仓库依赖 Jev 的问题 DSL，`SmallLLMBackend` 沦为「重新解释它本不该懂的对象」，抽象就成了装饰。问题集是**随版本固定的**（要拿 eval 语料调、改了准确率数字的含义就变了），不是每次请求现选的，所以它作为数据放在 `decision_model.py`，两个 backend 各自渲染。
7. **`DecisionUnavailable` 是异常，不是哨兵返回值。** 「后端挂了」和「自信地判定 unclear」对调用方是两件事：前者要跳熔断并交给降级，后者是真答案、绝不能重试。合并成一个值会让熔断器把一段难路由的对话统计成宕机，然后在最不该降级的时候降级。
8. **static fallback 绝不进缓存。** 它的含义是「所有后端都失败了」。缓存一小时 = 让宕机比它的成因活得更久，用户在后端恢复之后还在被要求重说一遍。
9. **缓存键里折进了问题集指纹。** criteria **就是** prompt：调完 criteria，旧缓存里存的是「另一个问题的答案」。不加指纹的现象是「照 eval 调了半天 prompt，一小时没效果」，正好把人引去 debug 错误的层。
10. **六种失败模式收敛成一个异常类型。** 实测 0.7.5 的映射：`ReadTimeout`→`TypeSafeAPITimeoutError`、`ConnectError`→`TypeSafeAPIConnectionError`、500→`InternalServerError`、429→`RateLimitError`、401→`AuthenticationError`、非 JSON body→`APIResponseValidationError`。全泄漏上去会让 `resilience.py` 反向 import Jev SDK，熔断器还得按厂商分支。
11. **`SmallLLMBackend` 用裸 JSON 手工解析，不用 `with_structured_output`。** 两个理由：structured output 走 tool calling / JSON-schema 模式，会把**降级路径**绑到「支持它的模型」上；更直接的是 `ScriptedChatModel.bind_tools` 是个返回自身的 no-op，用了就没法离线测。降级后端该有的属性是「除了能吐字之外不要求任何能力」。
12. **`INTENTS` 两份清单加了交叉测试。** evals 门禁**故意**不 import router（不该 import 被它评分的东西），但故意重复仍需测试，否则加标签只改一边、准确率数字会静默失真。做了变异验证：给 router 加一个 `summarize`，**3 个测试同时红**。
13. **`decisions_from_json` 承诺「绝不抛」，第一版漏了 `AttributeError`。** `probabilities` 是 `5` 的时候 `.items()` 就炸——测试当场抓到。缓存如果能抛异常，加缓存反而让路由比不加更不可靠。

**离线测试能证明什么，不能证明什么**：

14. **`MockTransport` 绕过网络层，httpx 不会启计时器**——handler 里 `sleep` 只是晚返回，**超时根本不会触发**。所以「800ms 到点降级」这条**无法离线行为验证**，只能钉住配置值（`_config.timeout`）+ 钉住超时抛出的异常类型会走 `DecisionUnavailable`。这条边界写进测试 docstring 了，不假装覆盖了。
15. **`transport` 这个 seam 值回票价**：跑的是真实请求序列化 + 真实响应解码 + 真实二值化。比 mock `decide` 有用得多——后者测的是 mock。

**eval 语料 24 → 36 条**（计划要求「从今天开始每天攒，别留到 D7 一次性编」）：injection 1 → 3（补了**间接注入**——指令藏在待总结的文献正文里，这正是「检索回来的内容是不可信输入」那条边界的用例；和**越狱式注入**——先角色扮演解除限制再索取整篇代写）。

16. **`path_deep` 此前零覆盖。** 快慢双路径是整个架构的第一条设计判断，却没有任何一条语料能测它。补了 2 真 2 假，并加了两条门禁：deep 行**必须写 note 说明为什么重**（和 boundary 行同一套纪律——「deep」是对成本与 grounding 的判断，不是句子的属性，无说明的 `true` 无法 review）；**boundary 行不许声明 path**（被拒的请求没有路径，给它标路径等于奖励 router 去选一条它本不该启动的管线，还会掩盖「越狱被正确识别为越界、却仍被路由到昂贵管线」这种情况）。

**当前门禁**：344 passed / 1 skipped / 5.0s；`ruff check` 全绿；`ruff format --check` 38 files already formatted。（D3 收尾时是 183 passed / 31 files。）

**仍未做**

- **在线验收**：`TYPESAFE_API_KEY` 未申请，所以计划里「Langfuse trace 能看到 Jev 的延迟和成本」这条**没验**。拿到 key 后要做三件事：真实网络下确认 800ms 够用（离线测不了，见坑 14）、trace 里确认延迟与 token、用真实 `SmallLLMBackend` 跑全量语料得到第一份真实准确率。
- **Jev 的成本没有计价。** `pricing.py` 的规则是「未知模型抛错，绝不打成 $0」，而 Jev 的价格我没有可靠来源，所以只记 token 不算钱。（API 文档说 output token 目前免费，所以 input 才是要看的那个数。）
- **接进图属于 D6。** `make_router()` 已可用，`config.py` 的 `typesafe_api_key / decision_model_backend / jev_timeout_ms` 三个字段早就预留好了，接线时注意 **ms → s 要除以 1000**。
- **词汇表缺口：「处理数据 / 生成统计代码」在 6 个 intent 里没有对应标签。** 按计划「先只 6 个不要贪多」，现在加第 7 个是错的；但这类请求目前只能落到 `unclear`，也就是被当越界拒掉。**刻意没有往语料里塞这一行**——门禁的 `test_negative_and_injection_expect_unclear` 会反过来**保护**这个错误标签，等 Week 3 真做数据处理时加 `data_analysis` 更干净。

---

- [x] ~~装 Docker Desktop~~ → **不需要了**，存储已本地化；Docker 留给 Week 4 部署
- [x] ~~申请 Semantic Scholar API key（无 key 配额低到没法开发）~~ → **D5 不再被它阻塞**：上游 `nature-academic-search` 已确认 CrossRef / arXiv **无需 key**，PubMed 只要联系邮箱。Semantic Scholar 降级为「有了更好」（它提供引用图谱与 field-of-study 过滤）。
- [ ] **申请 TypeSafe AI key（D4）** → **D4 的代码不再被它阻塞**（`transport` 假件可离线全测），但**在线验收被它阻塞**：800ms 超时能否真触发、Langfuse 里的 Jev 延迟与 token、真实准确率，三件事都要真 key。
- [x] ~~注册 Langfuse Cloud（D3）~~ → **已接入并 live 验证**（见 D3 坑 12、13）。key 只在 `.env`。
- [x] ~~**在 Langfuse 建 `deepseek-flash` 的自定义模型定义（填单价），否则成本永远是 0**~~ → **这条路走不通，改成在代码里算**（`paper_agent/pricing.py`，见 D3 补充）。两个原因：Langfuse 托管价格表 190 个条目里**一个 DeepSeek 都没有**；而且 model definition 表达不了 DeepSeek 的**高峰/低峰分时价**（UTC 周一至五 01:00–04:00 与 06:00–10:00，排除中国法定节假日，低峰恒为一半）。**Langfuse 看板的成本从此就是 0，这是已知且刻意的**——别再去后台点模型定义，也别把它当成 bug 修。
- [ ] **拿 DeepSeek 控制台的真实账单对一次账**，验证 `pricing.py` 的单价表。官方说明「优先扣赠送额度」，所以按单价算的数与余额实际减少的数未必相等——对账要连这条一起解释清楚。这是「成本数字不是拍的」的唯一硬证据。
- [ ] **`pricing.py` 的价格表带 `effective` 日期（当前 2026-08-16）与 `source` URL，隔一段时间要回去核一次。** DeepSeek 上一次调价缓存命中涨了约 1114%，而搜索能命中的中文/英文博客大多还是涨价前的数字。
- [ ] **给别人用之前必须给 Langfuse 挂 `mask=` 钩子。** 一条 trace 含完整 prompt/completion，开 Cloud 等于用户未发表的手稿出境。这是上线门禁，不是优化项；Week 4 自托管的真正理由也在这。
- [ ] **Week 2 若加 human-in-the-loop interrupt，`tracer_callbacks` 必须改成 per-`thread_id` 一个 handler**：v5 的 `_pending_resume_trace_contexts` 是实例级的，per-request 新建会让跨请求的 resume 断链。session 分组不受影响。
- [ ] routing.jsonl 目前 **36 条**（D4 从 24 加到 36），D7 要凑满 100 条 —— **每天随手追加，别攒到最后编**（现在少于 100 会显示成一条 pytest skip，不是红 CI）。**种子语料已找到**：本机 `nature-suite/SKILL.md` 的关键词表列了 9 类意图 + 中英触发词，抄分类不抄机制。**新增 `expect_deep` 字段**（快慢路径标签，此前零覆盖），带两条门禁：deep 行必须写 note 说明为什么重、boundary 行不许声明 path。
- [ ] **skill loader 必须保持 `skills/third_party/` 下六个目录互为兄弟**，否则 12 条 `../nature-shared/*.md` 引用会在 prompt 组装期静默失效（不报错，只掉质量）。D6 实现 loader 时这是硬约束。
- [ ] **`nature-statistics` 与 `nature-social-science` 是上游 Draft 级**，却分别是「处理数据」和「提供思路」的主力。要用就得补，改动逐条记进 `skills/third_party/PROVENANCE.md` 的修改日志（Apache-2.0 §4(b)）。
- [ ] 数字保真度 KPI：`nature-shared/scripts/check_consistency.py`（272 行纯 stdlib）是现成参考实现，Verifier 节点可以直接借鉴其 `Finding` 结构，**零 LLM 成本**。
- [x] ~~整个仓库零测试~~ → **D1 收尾补齐：101 passed / 1 skipped，0.9s**。四个测试文件覆盖 storage / config / API 契约 / routing 数据，并当场抓到 `Path("..").name == ".."` 那个真 bug（见坑 13）。
- [ ] `apps/web/agents.md` 是 `next dev` 自动生成并会自己加回来的，**别当成自己的改动删掉**（删了只会变成未提交改动，提交了反而脏）。
- [x] ~~**D3 之前先给 SSE 协议加事件**：`route` / `tool` / `citation`~~ → **`tool` 已在 D3 落地**（start/end + 前端 `dynamic-tool` part）。`route` 等 D4 意图路由，`citation` 等 Week 2 grounding。前端 transport 对未知事件名是忽略而不是报错，所以后端可以先加、前端后跟。
- [x] ~~**Week 3 前要决定向量检索走哪条路**~~ → **已定：本地全 SQLite**（`sqlite-vec` + FTS5 `bm25()` + RRF），部署时换 pgvector。见坑 11。
- [x] ~~`.env` 已从 example 生成，API key 还是空的~~ → **DeepSeek key 已接入并实测通过**（见坑 12）。`CHECKPOINT_BACKEND=sqlite`。
- [ ] **DeepSeek 余额只剩 ¥3.14（2026-10-14 实测）**。日常开发够用（flash 档单次工具调用约 500 token），但 Week 4 的评测套件（100 条 routing × 多轮 + grounding 逐 claim 蕴含校验）会很紧张，**跑评测前先充值**。
- [ ] **轮换 DeepSeek key 和 Langfuse key**：两把都在聊天里明文发过一次，已落进本地 transcript。`.env` 里那两份能用（`.env` 已确认未被 git 跟踪），但都该到控制台重签。DeepSeek 泄露的是付费额度，Langfuse 泄露的是**全部 trace 的读权限——里面是完整 prompt 和用户文本**，后者优先级更高。
- [ ] TypeSafe(Jev) / Semantic Scholar 两个 key 仍为空。Semantic Scholar 卡在 D5（且已降级为「有了更好」）；TypeSafe 只卡 D4 的**在线验收**，代码与离线测试都已完成。**Langfuse 已填。**
- [ ] **D4 的 `Decisions` 尚未接进 `AgentState`。** `graph/state.py` 的 docstring 里预留了 `intent / slots / missing_slots / guard / route_source` 五个字段，D6 接线时再加——现在加就是无人读的字段，每个都要进 checkpoint。`route` SSE 事件同理（前端对未知事件名是忽略而非报错，所以后端可以先加）。
