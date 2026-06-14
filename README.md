# Paper Pusher

基于 [findpapers](https://github.com/jonatasgrosman/findpapers) 的论文订阅推送：**搜索 → AI 摘要 → 飞书推送**，固定关注领域、多维度抓取新论文，LLM 自动生成中文摘要后推送到飞书群。

```
┌──────────────┐    ┌─────────────┐    ┌─────────────┐    ┌──────────┐
│ findpapers   │──▶│  去重存储    │──▶│  LLM 摘要    │──▶│ 飞书卡片  │
│ 7 个搜索库   │    │  (SQLite)   │    │ (DeepSeek)  │    │ + PDF 按钮│
└──────────────┘    └─────────────┘    └─────────────┘    └──────────┘
```

支持 7 个搜索库：arXiv、OpenAlex、PubMed、Semantic Scholar、IEEE、Scopus、Web of Science。arXiv 免 key，其他缺 key 自动跳过。

---

## 快速开始

### 1. 安装

需要 **Python 3.11+**。

```bash
cd find
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

> `curl-cffi` 在部分 Windows 环境装不上的话，在 `config.yaml` 把 `enrichment_databases.web_scraping.enabled` 设为 `false` 即可。

### 2. 配置

打开 `config.yaml`，改 3 项即可运行：

```yaml
llm:
  deepseek:
    api_key: "sk-你的key"           # https://platform.deepseek.com 申请

notify:
  feishu:
    webhook_url: "https://open.feishu.cn/..."  # 飞书群 → 群机器人 → 自定义机器人

search_databases:
  semantic_scholar:
    api_key: "你的key"             # 强烈建议申请（免费），https://api.semanticscholar.org
```

不想把 key 写进文件？用 `${VAR}` 占位符 + 环境变量：`api_key: "${DEEPSEEK_API_KEY}"`。

需要代理时在 `config.yaml` 末尾取消注释 `proxy` 行：`proxy: "http://127.0.0.1:7890"`。

其他数据库（PubMed / IEEE / Scopus / WoS）不填 key 会自动跳过。

### 3. 运行

项目拆成两个入口：

```bash
# —— 定时订阅 ——
python subscribe.py --once    # 单次跑（先用这个验证）
python subscribe.py           # 按配置周期持续跑
python subscribe.py --stats   # 查看数据库统计

# —— 临时查询 ——
python query.py --search "[LLM] AND [agent]"           # 查询 → 飞书列表卡
python query.py --search "[TinyML]" --sort citations   # 按引用量挖经典
python query.py --search-more                          # 续推下一页
python query.py --push-item 3                          # 从列表卡选第 3 篇生成 AI 摘要
python query.py --push-item 3,7,12                     # 一次推多篇
```

> `python main.py ...` 仍可用，会自动路由到上面两个。

---

## 命令行参数

### `subscribe.py` —— 定时订阅

| 参数 | 说明 |
|------|------|
| `--config PATH` / `-c PATH` | 配置文件路径（默认 `config.yaml`） |
| `--once` | 只跑一次，不启动定时调度 |
| `--stats` | 显示数据库统计 |
| `--db PATH` | SQLite 文件路径（默认 `paper_pusher.db`） |

### `query.py` —— 临时查询（两步式）

**步骤 1：查询 → 飞书列表卡（不调 LLM）**

| 参数 | 说明 |
|------|------|
| `--search "QUERY"` | 跑查询，结果做成飞书列表卡推送 |
| `--limit N` | 每数据库最多返回 N 篇（默认 20） |
| `--since YYYY-MM-DD` | 发表日期下限（不指定默认 1 年窗口） |
| `--sort {date,citations}` | 按日期（默认）或引用量排序 |
| `--digest-size N` | 每张列表卡条数（默认 15，硬上限 25） |
| `--search-more` | 续推上一 session 的下一页 |
| `--search-clear` | 清掉当前 session |

**步骤 2：挑论文 → AI 摘要 → 单篇飞书卡**

| 参数 | 说明 |
|------|------|
| `--push-item N[,N,...]` | 按列表卡的 `[N]` 编号挑论文，生成 AI 摘要后单篇推送 |
| `--config PATH` | 同 subscribe.py |
| `--db PATH` | 同 subscribe.py |

**典型流程**：`--search` 搜一把 → 飞书看到列表卡 → 看中 `[3]` `[7]` → `--push-item 3,7`。

---

## 查询 DSL 速查

所有查询使用 findpapers DSL，运行时自动翻译到各数据库。

### 字段过滤器

| 前缀 | 匹配字段 | 示例 |
|------|----------|------|
| （无）| 标题 + 摘要 | `[diffusion]` |
| `ti` | 只标题 | `ti[RAG]` |
| `abs` | 只摘要 | `abs[benchmark]` |
| `key` | 关键词 | `key[reinforcement learning]` |
| `au` | 作者 | `au[Yoshua Bengio]` |
| `src` | 期刊/会议 | `src[NeurIPS]` |

### 连接符

| 写法 | 含义 |
|------|------|
| `A AND B` | 同时出现 |
| `A OR B` | 任一出现 |
| `A AND NOT B` | A 出现且 B 不出现 |

**规则**：连接符必须大写、两边有空格；NOT 只能跟在 AND 后面；可用 `()` 分组：`([RAG] OR [retrieval]) AND [evaluation]`。

### 通配符

`*` 右端通配（`transform*` 命中 transformer/transformation），前缀 ≥ 3 字符。`?` 单字符通配。每对方括号内最多一个通配符。

### 验证

```bash
python -c "from findpapers.query.validator import QueryValidator; \
           QueryValidator().validate('[RAG] AND ti[evaluation]'); print('OK')"
```

---

## 常见问题

**Q: 飞书报 `code 11232` 限频？**
飞书机器人限速 ≤5 msg/sec。调大 `notify.feishu.min_interval_sec`（如 2.0），或减少 `schedule.max_articles_per_run`。

**Q: `ModuleNotFoundError: No module named 'curl_cffi'`？**
依赖没装全，先 `pip install -r requirements.txt`。实在装不上就关掉 `enrichment_databases.web_scraping.enabled`。

**Q: 想换 LLM 模型？**
`config.yaml` → `llm.deepseek.model` 改成 `deepseek-reasoner`（推理更强）或 `deepseek-chat`（快、便宜）。

**Q: 怎么加新查询维度？**
`config.yaml` 的 `queries` 列表里加一行 `{label: "...", query: "..."}` 即可，无须改代码。

**Q: 怎么彻底重置数据库？**
`rm paper_pusher.db`，下次运行自动重建。

---

## 去重 / 质量控制

- **DOI 去重**：同一 DOI 不论从哪个库返回都视为同一篇；无 DOI 时用 `sha256(title + first_author)`
- **LLM 摘要校验**：检查长度（≥60 字）、emoji 标记（≥3 个）、拒绝词（"我无法"等），不合格自动 reflection 修正（最多 2 轮）
- **pending 队列**：当天超出配额但未推送的论文自动入队，下次补出

---

## License

`paper_pusher/` 部分与本仓库父项目一致。`findpapers/` 部分为 MIT License。
