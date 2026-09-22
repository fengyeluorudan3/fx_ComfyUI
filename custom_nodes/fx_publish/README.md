# fx_publish

ComfyUI 本地发布节点，内置浏览器自动化发布能力。每个平台是一个独立节点：

- `发布抖音` (`publish_douyin`)
- `发布小红书` (`publish_xiaohongshu`)
- `发布快手` (`publish_kuaishou`)
- `发布视频号` (`publish_shipinhao`)
- `发布B站` (`publish_bilibili`)

节点执行时会先校验 Cookie；如果 Cookie 无效，会打开浏览器让你登录，然后继续发布。

## 输入

- `video`: 外部节点传入的视频对象，可选；支持 ComfyUI `LoadVideo` 输出，会先 `save_to` 临时视频文件，发布结束后删除
- `video_path`: 外部流程生成好的视频绝对路径，可选；和 `video` 二选一
- `title`: 标题
- `content`: 正文/描述
- `tags`: 标签，英文逗号分隔
- `cover`: 外部节点传入的封面图片，可选；会先保存为临时 PNG，发布结束后删除
- `cover_path`: 封面路径，可选；和 `cover` 二选一
- `headed`: 是否显示浏览器，建议 `yes`
- `keep_open_seconds`: 有头模式发布完成后保留浏览器的秒数
- `no_publish`: 调试用，只上传填写，不点击最终发布
- `cookies_path`: 自定义 Cookie 路径，可选

## 依赖

插件代码已内置在 `fx_publish_core`，不依赖外部 Spreado 项目目录。需要在 ComfyUI 使用的 Python
环境中安装：

```bash
pip install -r custom_nodes/fx_publish/requirements.txt
python -m playwright install chromium
```

## 说明

该插件不会保存平台账号密码。真实登录、上传和发布都在本机浏览器自动化环境中完成。
