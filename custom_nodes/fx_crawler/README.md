# fx_crawler

ComfyUI 多平台数据采集节点，移植自 [MediaCrawler](https://github.com/NanmiCoder/MediaCrawler)。
与 `fx_publish`（发布/写）互为镜像——本插件负责采集/读。每个平台一个独立节点：

- `爬取小红书` (`fx_crawl_xhs`)
- `爬取抖音` (`fx_crawl_douyin`)
- `爬取快手` (`fx_crawl_kuaishou`)
- `爬取B站` (`fx_crawl_bilibili`)
- `爬取微博` (`fx_crawl_weibo`)
- `爬取贴吧` (`fx_crawl_tieba`)
- `爬取知乎` (`fx_crawl_zhihu`)

## 三种采集模式（`crawler_type`）

- **search**：关键词搜索（填 `keywords`，英文逗号分隔）
- **detail**：指定内容 ID/URL（填 `specified_input`，一行一个）
- **creator**：指定创作者主页 ID/URL（填 `specified_input`，一行一个）

## 节点输入

- `crawler_type` / `keywords` / `specified_input`：见上
- `max_count`：最多采集多少条内容
- `enable_comments` / `enable_sub_comments`：是否抓一级/二级评论
- `login_type`：`qrcode`（扫码）或 `cookie`（配合 `cookie_str`）
- `browser_mode`：`cdp`（连你已登录的真实 Chrome，风控最低，推荐）或 `standard`（独立 Playwright）
- `headless`：无头运行
- `save_option`：`json` / `jsonl` / `csv` / `sqlite`
- `cookie_str`（可选）：`login_type=cookie` 时的 cookie 字符串
- `save_path`（可选）：结果输出目录，留空用插件 `_output/`

## 节点输出

- `contents_json`：采集到的内容（帖子/视频）JSON 数组字符串
- `comments_json`：评论 JSON
- `creators_json`：创作者信息 JSON
- `content_count`：内容条数
- `data_dir`：结果文件所在目录

输出的 JSON 字符串可直接接下游 LLM/分析节点，或与 `fx_publish` 组成
「采集竞品 → AI 改写 → 多平台发布」闭环。

## 视频下载节点（video_nodes.py）

底层 yt-dlp，把一条链接变成 ComfyUI 的 `VIDEO`，直接接 Save Video / Trim Video /
Get Video Components。

| 节点 | 作用 |
| --- | --- |
| **下载视频(URL)** `fx_video_download` | 链接 → `VIDEO` / `AUDIO` / 标题 / 文件路径 / 信息JSON |
| **解析视频信息(URL)** `fx_video_probe` | 只解析不下载，看标题/时长/有哪些清晰度 |
| **从采集结果取链接** `fx_video_pick_url` | 采集节点的 `contents_json` → 第 N 条的链接 |

- **支持站点**：yt-dlp 覆盖的都行 —— B站 / 抖音 / 小红书 / 快手 / 微博 / 西瓜 /
  AcFun / 好看 / 微视 / YouTube / TikTok / X / Instagram / Vimeo / Twitch …
  **另：微信视频号**（`weixin.qq.com/sph/...`、`channels.weixin.qq.com/finder-preview/...`）
  走独立解析，不依赖 yt-dlp。
- **直接吃分享文案**：抖音、小红书复制出来那一大段带表情和文字的内容可以整段粘贴，
  节点自己抽 URL；也支持裸 `BV号` / `av号`。
- **缓存**：按 `链接+画质` 落到 `input/fx_video/`，同参数重跑工作流秒出，不重复下载。
- **cookie**：小红书/抖音部分内容、B站 1080P+ 需要登录态。四种来源 ——
  `cookie文本`（DevTools 里 copy 的 `k=v; k=v`，节点转成 Netscape 格式）、
  `cookies.txt文件`、`本机Chrome`、`fx_crawler浏览器`（复用采集节点登录过的
  `browser_data/` 那个 Chrome 用户目录，macOS 首次可能弹钥匙串授权）。
  **视频号**：分享链匿名拿不到直链。请登录 [腾讯元宝](https://yuanbao.tencent.com)，
  把 Cookie 填到节点 `cookie`（来源选「cookie文本」），或设环境变量
  `FX_CRAWLER_YUANBAO_COOKIE`。若链接本身已带 `token`+`eid`，可不用元宝 cookie。
- **ffmpeg**：B站等站点音视频分轨，合并必须有 ffmpeg。优先用 PATH 里的，
  没有则回落到 `imageio-ffmpeg` 自带的二进制。
- 各平台接口变动频繁，**抖音/小红书下载失败第一步是 `pip install -U yt-dlp`**。
  视频号失败则优先检查元宝 cookie 是否过期。

## 依赖

```bash
pip install -r custom_nodes/fx_crawler/requirements.txt
python -m playwright install chromium
```

## 说明与许可

- 本插件的 `fx_crawler_core/` 移植自 MediaCrawler，遵循其
  **NON-COMMERCIAL LEARNING LICENSE 1.1**（见 `fx_crawler_core/LICENSE`），
  仅供学习研究，不得用于商业用途；使用时应遵守目标平台使用条款、robots.txt、
  合理控制频率。签名算法依赖 `xhshow`（Cloxl）等开源库。
- 移植改动：绝对 import 改写为包内绝对（`fx_crawler_core.*`）以避免与其它
  ComfyUI 插件命名冲突；`libs/`、`docs/` 资源路径改为包内相对（不依赖 cwd）；
  节点层通过运行前覆盖全局 `config` 注入参数。

## 架构

```
fx_crawler/
  __init__.py          # 注册节点 + 注入 sys.path
  nodes.py             # 7 个平台采集节点（ComfyUI 封装）
  crawler_runner.py    # 设置注入 config → 跑采集 → 读回结果
  runner.py            # 独立测试入口（payload.json）
  fx_crawler_core/     # vendored MediaCrawler（base/tools/model/store/proxy/
                       #   cache/database/constant/config/media_platform/libs/docs）
```
