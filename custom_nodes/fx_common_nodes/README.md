# fx_common_nodes

ComfyUI 节点：**Jina 爬虫**、**Google 搜索**、**Todo 格式化**，写法参考 `jina_reader_tool` / `google_search_tool` / `todo_tool`。

## 节点

| 节点 | 分类 | 说明 |
|------|------|------|
| **FX 网页正文提取 (Jina)** | fx_common/crawler | 输入 URL，用 Jina Reader API 提取网页正文，输出 STRING（Markdown）。 |
| **FX Google 搜索** | fx_common/search | 输入关键词，用 SerpAPI 搜索，输出 STRING（精选摘要 + 知识图谱 + 结果列表）。需环境变量 `SERPAPI_API_KEY`。 |
| **FX Todo 列表格式化** | fx_common/todo | 输入多行任务（每行 `content` 或 `status: content`），输出与 todo_tool 一致的格式化 STRING。 |

## Todo 节点输入格式

每行一条任务，支持两种写法：

- `内容` → 使用默认状态（默认 pending）
- `status: 内容` → status 取 `pending` / `in_progress` / `completed`

示例：

```
第一步
in_progress: 第二步
completed: 第三步
```

## 依赖

- `pip install requests`
- Google 搜索节点需配置环境变量：`SERPAPI_API_KEY`

## 使用

1. 重启 ComfyUI 或刷新节点列表。
2. **fx_common/crawler**：网页正文提取 (Jina)
3. **fx_common/search**：Google 搜索
4. **fx_common/todo**：Todo 列表格式化
