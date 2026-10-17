# fx_publish Stability Model

## Goals

- Every publish run has a `run_id`.
- Every critical browser step logs start, success, failure, and retry attempt.
- Every failed step captures traceable evidence before retrying.
- ComfyUI node outputs include `status`, `run_id`, and `debug_dir`.

## Evidence

Failure evidence is saved under:

```text
logs/fx_publish_debug/<platform>/<run_id>/
```

Each failed step writes:

- `.json`: step name, attempt, URL, failure reason
- `.png`: full-page screenshot when available
- `.html`: page HTML when available

## Retry Policy

Critical steps retry before moving to the next step:

- navigation: retry 2 times
- file upload: retry 2 times
- upload completion wait: retry 2 times
- metadata filling: retry 3 times
- final publish submit: one attempt; an unconfirmed result must be checked in the platform backend before any retry

Optional steps such as cover upload may fail without blocking when the platform supports publishing without them.

`status=preview_ready` means form filling passed with `no_publish=yes`; it does not mean the platform saved a draft. `status=success` means the platform showed a submission-success signal, not that moderation has finished. The node also writes this receipt to ComfyUI history so FXAgent can read it.

## Verification Status (2026-10-17)

An 8-second H.264 video from `ComfyUI/output` was used for a Douyin `no_publish=yes` node smoke test. The existing Chrome process occupied port 9222 but returned HTTP 404 for `/json/version`, so CDP attachment was unavailable. A new visible browser reached `creator.douyin.com`; the run then timed out waiting for login and **did not upload or publish** the video. The uploader now falls back from an unavailable Chrome attachment to a visible login browser. The timeout evidence showed the login form still visible despite a `/content/upload` URL; URL alone is not proof of authentication.

A separate live browser test in the user's already authenticated Chrome uploaded that same output video to Douyin, filled the title and description, showed both in the preview, and reached the platform's `作品未见异常` check result. The final publish and save-draft buttons were **not clicked**. This confirms the current site accepts the file and exposes the expected edit fields. It does **not** count as an authenticated `fx_publish` node pass because the node cannot attach to this Chrome session; the 2026-07-14 node results below are historical evidence, not a current pass.

## Troubleshooting

When a node fails, use its returned or raised `run_id` and `debug_dir`. Inspect the latest `.png`,
then use `.html` to update selectors if the platform page changed.

## Verification Status (2026-07-14)

后台自动化实测（`no_publish=yes`，完整走到"发布"前一刻，含自定义封面）：

| 平台 | 状态 | 说明 |
|------|------|------|
| 抖音 douyin | ✅ 端到端验证通过 | 上传/标题/正文/标签/自定义竖封面/关闭横封面二次弹窗 全部通过；发布按钮可点 |
| 小红书 xiaohongshu | ✅ 端到端验证通过 | 上传/标题/正文/标签/自定义封面 全部通过；含上传失败自动"重新上传"重试 |
| 快手 kuaishou | ⚠️ 待登录验证 | 代码已审查、no_publish 已接入、无 cookie 无法后台跑 |
| 视频号 shipinhao | ⚠️ 待登录验证 | 同上；注意平台已移除发布页封面入口，封面需发布后在列表页手动设置（平台限制） |
| B站 bilibili | ⚠️ 待登录验证 | 原为未实现 stub，本次已补齐完整实现，需登录后验证选择器 |

**未验证平台的验证方法**：在 ComfyUI 根目录执行
```bash
python custom_nodes/fx_publish/runner.py <payload.json>
```
payload 里设 `"headed": true, "no_publish": true`，首次会打开浏览器让你扫码登录（cookie 存入
`cookies/<platform>_uploader/account.json`），之后完整走完填充流程但不真正发布。看
`debug_dir` 里 `*_skip_publish_no_publish.png` 确认页面填充无误，再把 `no_publish` 改回 `false`
正式发布。任何步骤失配时，`debug_dir` 里有该步的 `.png/.html/.json` 现场可用于修选择器。
