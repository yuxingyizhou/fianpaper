---
name: paper-pusher
description: 用 paper-pusher 管理学术论文订阅。两个核心场景：(1) 配置/切换 **长期关注的领域** 让它定时推送；(2) 对话中临时触发 **单次查询 → digest 列表卡推到飞书 → 用户挑出感兴趣的几条要 AI 摘要**（两步式）。当用户提到"订阅某领域的论文""推送 X 方向的新论文""列一下 X 方向的论文""查最近的 Y 论文""推第 N 篇/那篇 XXX 的论文摘要"时启用此 skill。
---

# Paper Pusher Skill

paper-pusher 是个**定时调度 + 对话临时触发**双轨的论文订阅工具。运行环境：**Linux + Python 3.11+**。

## 当前用户画像 & 关注方向

启动会话时先 `cat config.yaml | grep -A 1 "^field\|^  - label"` 或读 `config.yaml` 的 `field` / `queries` 字段，了解用户当前关注的方向。**当用户的请求模糊时，优先按这个方向理解**。

例如 config 里 `field: "TinyML / 边缘智能"`、queries 集中在 TinyML/农业/算法方向，此时：
- 用户说"查最近的论文" → 默认结合 config 里的方向（如 TinyML 或具身智能）
- 用户说"看看 NAS 进展" → 走单次查询触发，因为 NAS 是用户已关注方向，但不必每次定时推
- 用户说"换成生物医学方向" → 这是要改 config 的 `field` + `queries`，进入**场景 A**

## 环境约定

所有命令都假设你处在 paper-pusher 的项目根目录（即 `find/` 这一级）下。约定用环境变量 `PAPER_PUSHER_DIR` 指向它，agent 启动会话时先做：

```bash
cd "$PAPER_PUSHER_DIR"        # 比如 /opt/paper-pusher 或 ~/paper-pusher
# 如果用了 venv：source .venv/bin/activate
```

**两个入口脚本**（项目已拆分）：

- `python subscribe.py ...` —— 定时订阅推送（`--once` / 守护 / `--stats`）
- `python query.py ...`     —— 临时查询（`--search` / `--search-more` / `--search-clear` / `--push-item`）

老命令 `python main.py ...` 仍可用（薄包装，按参数自动路由到上面两个之一）。配置文件就是 `./config.yaml`，数据库就是 `./paper_pusher.db`。

> 如果不确定路径：`echo $PAPER_PUSHER_DIR`；没设的话，让用户告诉你部署路径，并建议他们把 `export PAPER_PUSHER_DIR=...` 加进 shell rc。

---

## 你能做的两件事

### 场景 A：长期关注（定时推送）

用户说 *"以后帮我盯 RAG 这块"* / *"换成生物医学方向"* / *"加一个具身智能维度"* —— 改 `config.yaml`，下次定时任务生效。

### 场景 B：对话式查询（**两步**：列表卡 → 用户挑 → 单篇摘要）

用户说 *"查一下最近 X 方向的论文"* / *"列一下 X 的经典论文"* / *"看看 LeCun 最近发了啥"* —— 走单次查询，结果做成一张飞书列表卡推到群里（**不调 LLM**），剩余的存 session 队列。

用户**看到列表卡后**挑几条让 agent 推 AI 摘要：*"推第 3 篇"* / *"推第 3 和第 7 篇"* / *"把 Toolformer 那篇推一下"* —— agent 把用户的自然语言翻译成 position 编号，跑 `--push-item N[,N,...]`，对每条调 LLM 生成中文摘要 + 飞书单篇卡 + 入主去重表。

**这是两步式的工作流**：
1. **`--search ...`** — 探索阶段：飞书收到一张列表卡列出 1-25 条元数据（题目/作者/引用/DOI/PDF 链接）
2. **`--push-item N[,N,...]`** — 精读阶段：用户挑出感兴趣的 1-N 条，agent 帮他们要 AI 摘要

中间用户可以多次说 *"继续/下一页"* 走 `--search-more` 看更多结果，也可以中途说 *"够了/换一个"* 走 `--search-clear` 清掉队列。

---

## 场景 A：配置定时推送（修改 config.yaml）

**步骤**：

1. 读 `find/config.yaml`，定位 `field:` 和 `queries:` 两个键
2. 改 `field`（顶层分类，比如 `"AI/LLM"`、`"生物医学/AI"`）
3. 替换或追加 `queries` 里的条目，每条包含：
   - `label`（必填，写人类可读的维度名）
   - `query`（必填，DSL 字符串，见下面"DSL 速查"）
   - `category`（可选，覆盖 `field`）
4. 用 `python query.py --search "..." --limit 5` 试跑一条新 query，确认搜得到结果（列表卡会推到飞书）
5. 询问用户是否要清空数据库（`rm paper_pusher.db`），默认**保留历史**
6. 跑 `python subscribe.py --once` 验证完整链路

**多领域并行处理**：如果用户想同时盯多个领域，**不要**拼成一条超长 query。两种推荐做法：
- 在同一 config 里给每条 query 设不同 `category` 字段
- 用多份 config + 不同 `--db` + 不同飞书 webhook：`python subscribe.py --once -c bio.yaml --db bio.db`

**例子**：把领域换成"网络安全"

```yaml
field: "网络安全"

queries:
  - label: "LLM 漏洞挖掘"
    query: "([vulnerability detection] OR [fuzzing]) AND [large language model]"
  - label: "对抗样本"
    query: "[adversarial example] AND ([robustness] OR [defense])"
  - label: "侧信道攻防"
    query: "[side channel] AND ([attack] OR [defense])"
  - label: "顶会 venue"
    query: "src[USENIX Security] OR src[CCS] OR src[S&P]"
```

---

## 场景 B：单次查询 + 列表卡 + 按需 AI 摘要

### B.1 第一步：跑查询，结果做成列表卡推到飞书

**命令模板**：

```bash
python query.py --search "DSL_STRING" [--since YYYY-MM-DD] [--sort {date,citations}] [--limit N] [--digest-size N]
```

参数：
- `--search "..."` —— 必填，DSL 查询字符串
- `--since YYYY-MM-DD` —— 发表日期下限。**不指定时默认 1 年窗口**（独立于 config 的 `max_age_days`——后者只管定时推送）
- `--sort {date,citations}` —— 排序方式。默认 `date`（最新优先）；**用户说"经典/高引用/影响力大"时换成 `citations`**
- `--limit N` —— 每个数据库返回多少篇（默认 20；挖经典时建议 50）
- `--digest-size N` —— 单张列表卡几条（默认 15，**硬上限 25**）；用户没特别要求就别指定

**`--sort` 怎么选**：
- 用户说"挖经典工作"、"X 方向的奠基论文"、"高引用论文"、"必读论文"、"做综述要看 X 这几年的工作" → `--sort citations` 配合较宽时间窗 `--since 2022-01-01` 之类
- 用户说"最近"、"最新"、"近期"、"看看 X 方向有什么新进展" → 保持默认 `--sort date`

**`--since` 怎么选**：
- 用户没指定时间 → 不加 `--since`，用默认 1 年窗口
- 用户说"近两年/这几年" → `--since 2024-01-01` / `--since 2023-01-01`
- 用户说"经典/必读"且没指定时间 → 通常 `--since 2020-01-01` ~ `--since 2022-01-01`

**流程**（agent 主动报告进度）：

1. 第一次跑查询后告诉用户：
   > 在 X 个数据库找到 N 篇，按引用量排序后做成飞书列表卡推送了首批 15 条，剩余 M 条。
   > **想要哪几篇的 AI 摘要就告诉我编号**（比如"推第 3 篇"或"推 3、7、12"）；想看下一页就说"继续"；不想看了就说"清掉"。

2. 用户说"继续/下一页/再来一张"时跑 `python query.py --search-more`，回复：
   > 已推第 K 批 15 条，剩余 M 条。

3. 末页推完时（日志会出现 "末页推送完成"）：
   > 已推送完毕共 N 条，session 已清理。

4. 用户中途说"够了/换一个/不看了"，跑 `python query.py --search-clear` 然后回复：
   > 已清掉当前 digest 队列。

**边界要会处理**：
- 用户在 session 还没推完时又说"查 X"——直接跑新 `--search`，旧 session 会被顶掉（这是预期行为），但**先提醒一句** "上一批 X 还剩 M 条没推完，要顶掉吗？"
- 用户说"继续"但根本没有 active session——`--search-more` 会自己回 "无活跃 digest 会话"，agent 转告并问要不要重新查
- 用户说"卡片太长 / 想短一点" —— `--digest-size 8` 之类
- 用户说"卡片想长一点" —— 最多 `--digest-size 25`（**硬上限**）

### B.2 第二步：用户挑出感兴趣的几条要 AI 摘要

用户看到列表卡后，会用自然语言指代要看摘要的论文。**agent 的任务是翻译成 position 编号**（飞书卡上每条前的 `[N]` 数字就是 position）。

**命令**：

```bash
python query.py --push-item N            # 单篇：推第 N 条
python query.py --push-item N1,N2,N3     # 多篇：逗号分隔
```

**翻译规则**：

| 用户措辞 | 翻译为 |
|---|---|
| "推第 3 篇" / "把第 3 个推一下" | `--push-item 3` |
| "推 3 和 7" / "3、7、12 都要" | `--push-item 3,7,12` |
| "前三篇都要" | `--push-item 1,2,3` |
| "Toolformer 那篇" | agent 看卡片标题找出 position（如 `[1]`）→ `--push-item 1` |
| "LeCun 作者那两篇" | 同上，agent 帮用户找出对应 position |

如果用户给的标题模糊（"那篇讲推理的"），**先回问一句"是 [3] Self-Consistency 那篇还是 [7] Chain-of-Thought 那篇？"** 不要猜。

**单次最多挑几条？**：没有硬上限，但**实际建议 ≤5 条**（每条都走 LLM 摘要 + 单独飞书卡，5 条已经要 ~2 分钟 + 5 张飞书卡刷屏）。用户要 10+ 条时**反问一次**："这一次会调 N 次 LLM 摘要 + N 张飞书卡刷屏，确认要这么多吗？"

**返回**：
- 日志会有 `[push-item] 完成，成功推送 X/N 篇`
- agent 回复模板：
  > 已对第 3/7/12 三篇生成 AI 摘要并推到飞书。

**这些情况要会处理**：
- 没活跃 session 时跑 `--push-item` → 日志 `无活跃 digest 会话` → agent 回："还没有查询结果可挑，先告诉我要查什么。"
- 用户给的编号超过了 session 范围（如只有 47 条但说"推第 50 篇"）→ 日志 `这些 position 不在 session 中，已跳过` → agent 转告。
- `--push-item` 不会消耗 session（不影响 `--search-more` 的进度），同一篇可以多次推（但通常没必要）。
- 推过的论文**会入主去重表**，定时订阅看到同一篇 DOI 就不会再推一次。

---

## DSL 速查（写 query 必备）

每条 `query` 是字符串，遵守 findpapers 语法。

### 词

**所有词必须放方括号内**：`[term]`、`[large language model]`

### 字段过滤（前缀贴在 `[` 之前，无空格）

| 前缀 | 字段 | 例 |
|---|---|---|
| 无 | 标题 + 摘要（默认） | `[diffusion]` |
| `ti` | 只标题 | `ti[RAG]` |
| `abs` | 只摘要 | `abs[benchmark]` |
| `key` | 关键词 | `key[reinforcement learning]` |
| `au` | 作者 | `au[Yoshua Bengio]` |
| `src` | 期刊/会议名 | `src[NeurIPS]` |
| `aff` | 作者单位 | `aff[MIT]` |
| `tiabskey` | 标题+摘要+关键词 | `tiabskey[LLM]` |

### 连接符（必须大写、两侧空格）

- `[A] AND [B]` —— 都要
- `[A] OR [B]` —— 任一
- `[A] AND NOT [B]` —— A 有 B 没（NOT **只能**跟 AND 后）

### 分组

```
([RAG] OR [retrieval augmented]) AND [evaluation]
src([NeurIPS] OR [ICML]) AND [reasoning]
```

### 通配符（可选）

- `transform*` —— 末尾 0+ 字符（`*` 前至少 3 字符）
- `colo?r` —— 单字符
- 一个 term 内最多一个通配符；不能放词首

---

## 自然语言 → DSL 翻译参考

| 用户说 | DSL |
|---|---|
| "最近的 LLM agent 论文" | `[large language model] AND [agent]` |
| "RAG 评估相关" | `([RAG] OR [retrieval augmented generation]) AND [evaluation]` |
| "Yann LeCun 最近发了啥" | `au[Yann LeCun]` |
| "NeurIPS 上的推理工作" | `[reasoning] AND src[NeurIPS]` |
| "扩散模型生视频，排除综述" | `[diffusion model] AND [video] AND NOT [survey]` |
| "MIT 出的具身智能" | `aff[MIT] AND [embodied]` |
| "多模态大模型" | `[vision language model] OR [multimodal large language model]` |

**常见翻译陷阱**：
- 用户说"和/与" → 翻译成 `AND`
- 用户说"或" → 翻译成 `OR`
- 用户说"不要/排除" → `AND NOT`，不能单独 `NOT`
- 用户给术语 → 优先用学术常见说法（"大模型" → `[large language model]`，不是 `[LLM]`，因为论文标题/摘要里全称命中率更高）
- 用户给多个同义词 → 用 `OR` 全列上提高召回（如 `[RAG] OR [retrieval augmented generation]`）
- 用户说"经典/高引用/必读" → DSL 本身不变，**改用 `--sort citations` 并放宽 `--since`**（如 `--since 2020-01-01`）

---

## 其它常用命令

| 用户意图 | 命令 |
|---|---|
| "把现在的订阅跑一次给我看" | `python subscribe.py --once` |
| "启动定时推送" | `python subscribe.py`（前台守护，Ctrl+C 停） |
| "看看推送过多少论文" | `python subscribe.py --stats` |
| "随便看一下 X 有没有新论文" | `python query.py --search "DSL" --limit 5`（同样推到飞书列表卡）|
| "列一下 X 方向的经典论文" | `python query.py --search "DSL" --since 2022-01-01 --sort citations --limit 50` |
| "继续/下一页/再来一张" | `python query.py --search-more` |
| "清掉当前 digest 队列" | `python query.py --search-clear` |
| "推第 N 篇的摘要" | `python query.py --push-item N` |
| "推 N1、N2、N3 这几篇" | `python query.py --push-item N1,N2,N3` |
| "清空记录重新开始" | `rm paper_pusher.db`（**必须先问用户确认**） |
| "config 改了，验证一下能跑通" | `python query.py --search "<config 里某条 query>" --limit 3` |

---

## 错误处理

### `ModuleNotFoundError: No module named 'curl_cffi'`
环境没装依赖。回复用户：跑 `pip install -r requirements.txt`。如果 `curl_cffi` 死活装不上（某些 musl libc 发行版上会），在 `config.yaml` 把 `enrichment_databases.web_scraping.enabled` 设为 `false`。

### `QueryValidationError: ...`
DSL 写错了。常见原因：
- 忘了方括号 → `[transformer]` 而不是 `transformer`
- 连接符小写 → 改成大写 `AND` / `OR`
- 连接符两侧没空格
- `NOT` 单独用 → 改成 `AND NOT`

可用离线校验：

```bash
python -c "
import sys, types
for n in ['curl_cffi','curl_cffi.requests','curl_cffi.requests.errors']:
    sys.modules[n] = types.ModuleType(n)
sys.modules['curl_cffi.requests'].Response = type('R',(),{})
sys.modules['curl_cffi.requests'].Session = type('S',(),{})
sys.modules['curl_cffi.requests.errors'].RequestsError = type('E',(Exception,),{})
from findpapers.query.validator import QueryValidator
QueryValidator().validate('YOUR_DSL_HERE')
print('OK')
"
```

### 飞书返回 `code 11232`
推送频率太高。代码已自动重试，如果还是失败，调高 `config.yaml` 的 `notify.feishu.min_interval_sec` 到 2.0+ 或在 `--push-item` 时减少一次挑的篇数。

### Semantic Scholar 超时 / 403 / 连接错
国内网络访问 Semantic Scholar 经常卡。**先建议用户开代理**：在 `config.yaml` 最底下打开 `proxy: "http://127.0.0.1:7890"` 那两行（具体端口看代理软件，详见 README 的「4. 走代理」）。代理软件没在跑也不会崩——失败的库会被跳过。如果用户没代理，临时把 `search_databases.semantic_scholar.enabled` 设为 `false` 即可。

### 某数据库被自动跳过 / 日志 `Skipping 'X': a required API key was not provided`
对应 `search_databases.X.api_key` 是 `${VAR}` 占位符且环境变量没设。建议用户**直接把 key 字符串粘贴进 config**（最简单），或者按 README 的「附：用环境变量代替直填」设环境变量。各 key 申请地址见 README 的「3. 申请 API key」。

### LLM 返回乱七八糟，反馈 reflection 后还是不达标
说明 prompt 在当前模型上效果不稳。检查 `config.yaml` 的 `llm.deepseek.api_key` 是否有效；或换 `model: "deepseek-reasoner"` 试试。

---

## 不要做的事

- **不要**在没问用户的情况下 `rm paper_pusher.db`
- **不要**编辑 `find/findpapers/` 下任何文件（这是 vendored 副本，会被覆盖）
- **不要**自作主张帮用户挑要推 AI 摘要的论文。`--push-item` 的语义是 **"用户从列表卡里指定推送"**——用户没明确说哪几篇就不要跑 `--push-item`，先把列表卡推给他们看。
- **不要**在已经有 active session 时直接跑新的 `--search` 而不告知用户（旧 session 会被顶掉）
- **不要**在用户没明确同意的情况下 `--push-item` 超过 5 条（每条 LLM 摘要 + 单独飞书卡，刷屏 + 等久）
- **不要**把用户的 API key / webhook 写到日志、commit message、或对话以外的任何地方
- **不要**为了"看起来更专业"把用户的简单意图翻译成超复杂的 query —— 简单 AND/OR 组合通常召回更稳
- **不要**跳过 `--limit` 试跑 —— 先用 `--search ... --limit 5` 看几条标题，确认 query 写得对再放大召回
