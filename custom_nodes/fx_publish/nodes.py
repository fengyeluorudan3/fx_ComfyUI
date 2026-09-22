import asyncio
import io
import os
import shutil
import tempfile
import threading
from pathlib import Path

try:
    import folder_paths
except Exception:
    folder_paths = None


def _get_uploader_cls(platform):
    try:
        if platform == "bilibili":
            from .fx_publish_core.plugins.bilibili import BilibiliUploader
            return BilibiliUploader
        if platform == "douyin":
            from .fx_publish_core.plugins.douyin import DouYinUploader
            return DouYinUploader
        if platform == "kuaishou":
            from .fx_publish_core.plugins.kuaishou import KuaiShouUploader
            return KuaiShouUploader
        if platform == "shipinhao":
            from .fx_publish_core.plugins.shipinhao import ShiPinHaoUploader
            return ShiPinHaoUploader
        if platform == "xiaohongshu":
            from .fx_publish_core.plugins.xiaohongshu import XiaoHongShuUploader
            return XiaoHongShuUploader
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("playwright"):
            raise RuntimeError("缺少 playwright 依赖，请先安装 custom_nodes/fx_publish/requirements.txt") from exc
        raise
    raise ValueError(f"不支持的平台: {platform}")


def _browser_attrs(browser_mode, cdp_port):
    """节点的浏览器模式 → 注入 uploader 的属性。

    复用模式下把 cdp_port 传下去，BaseUploader._new_browser 会据此走附加模式；
    新开浏览器模式保持 None，行为与以前完全一致。
    """
    if str(browser_mode or "").strip() == "复用我的Chrome":
        return {"_cdp_port": int(cdp_port or 9222), "_cdp_autostart": True}
    return {"_cdp_port": None}


def _split_tags(value):
    return [item.strip().lstrip("#") for item in str(value).split(",") if item.strip()]


def _run_async(coro):
    result = {}

    def runner():
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            result["value"] = loop.run_until_complete(coro)
        except BaseException as exc:
            result["error"] = exc
        finally:
            loop.close()

    thread = threading.Thread(target=runner, daemon=False)
    thread.start()
    thread.join()
    if "error" in result:
        raise result["error"]
    return result.get("value")


def _path_from_comfy_file(value):
    if value is None:
        return ""
    if isinstance(value, (str, Path)):
        return str(value)
    if isinstance(value, (list, tuple)):
        for item in value:
            item_path = _path_from_comfy_file(item)
            if item_path:
                return item_path
        return ""
    if isinstance(value, dict):
        nested = value.get("video") or value.get("videos") or value.get("result")
        if isinstance(nested, list) and nested:
            nested = nested[0]
        if nested is not None and nested is not value:
            nested_path = _path_from_comfy_file(nested)
            if nested_path:
                return nested_path
        for key in ("path", "filepath", "filename", "file", "name"):
            if value.get(key):
                raw = str(value[key])
                resolved = _resolve_comfy_file_reference(raw, value)
                return resolved or raw
    for attr in ("path", "filepath", "filename", "file", "name"):
        item = getattr(value, attr, None)
        if item:
            raw = str(item)
            resolved = _resolve_comfy_file_reference(raw, value)
            return resolved or raw
    return ""


def _resolve_comfy_file_reference(filename, source):
    path = Path(str(filename)).expanduser()
    if path.is_file():
        return str(path)
    if folder_paths is None:
        return ""

    subfolder = ""
    file_type = "output"
    if isinstance(source, dict):
        subfolder = str(source.get("subfolder") or "")
        file_type = str(source.get("type") or source.get("folder") or "output")
    else:
        subfolder = str(getattr(source, "subfolder", "") or "")
        file_type = str(getattr(source, "type", "") or getattr(source, "folder", "") or "output")

    bases = []
    for getter in ("get_output_directory", "get_input_directory", "get_temp_directory"):
        try:
            bases.append(Path(getattr(folder_paths, getter)()))
        except Exception:
            pass
    if file_type == "input":
        bases = sorted(bases, key=lambda p: 0 if "input" in str(p) else 1)
    elif file_type == "temp":
        bases = sorted(bases, key=lambda p: 0 if "temp" in str(p) else 1)

    for base in bases:
        candidate = (base / subfolder / filename).resolve()
        if candidate.is_file():
            return str(candidate)
    return ""


def _copy_to_temp_file(path_value, prefix, suffix):
    source = Path(str(path_value)).expanduser()
    if not source.is_file():
        return ""
    suffix = source.suffix or suffix
    fd, temp_path = tempfile.mkstemp(prefix=prefix, suffix=suffix)
    os.close(fd)
    shutil.copyfile(source, temp_path)
    return temp_path


def _save_image_tensor(image, temp_paths):
    if image is None:
        return ""
    try:
        import numpy as np
        from PIL import Image

        tensor = image[0] if hasattr(image, "shape") and len(image.shape) == 4 else image
        array = tensor.detach().cpu().numpy() if hasattr(tensor, "detach") else np.asarray(tensor)
        array = np.clip(array * 255.0, 0, 255).astype(np.uint8)
        fd, raw_path = tempfile.mkstemp(prefix="fx_publish_cover_", suffix=".png")
        os.close(fd)
        path = Path(raw_path)
        Image.fromarray(array).save(path)
        temp_paths.append(path)
        return str(path)
    except Exception:
        path = _path_from_comfy_file(image)
        if path:
            copied = _copy_to_temp_file(path, "fx_publish_cover_", ".png")
            if copied:
                temp_paths.append(Path(copied))
                return copied
        return ""


def _save_video_to_temp(video, temp_paths):
    raw_file = _extract_video_from_file_source(video)
    if raw_file is not None:
        saved = _save_raw_video_source(raw_file, temp_paths)
        if saved:
            return saved

    save_to = getattr(video, "save_to", None)
    if callable(save_to):
        fd, temp_path = tempfile.mkstemp(prefix="fx_publish_video_", suffix=".mp4")
        os.close(fd)
        try:
            save_to(temp_path)
            if Path(temp_path).is_file() and Path(temp_path).stat().st_size > 0:
                temp_paths.append(Path(temp_path))
                return temp_path
            Path(temp_path).unlink(missing_ok=True)
        except Exception:
            Path(temp_path).unlink(missing_ok=True)

    path = _path_from_comfy_file(video)
    if path:
        copied = _copy_to_temp_file(path, "fx_publish_video_", ".mp4")
        if copied:
            temp_paths.append(Path(copied))
            return copied
    raise ValueError(
        "传入的 video 无法保存为临时视频文件，请改用 video_path。"
        f" video_debug={_debug_value(video)}"
    )


def _extract_video_from_file_source(video):
    for attr in ("_VideoFromFile__file", "__file"):
        try:
            if hasattr(video, attr):
                return getattr(video, attr)
        except Exception:
            pass
    return None


def _save_raw_video_source(source, temp_paths):
    if isinstance(source, (str, Path)):
        copied = _copy_to_temp_file(source, "fx_publish_video_", ".mp4")
        if copied:
            temp_paths.append(Path(copied))
            return copied
        return ""
    if isinstance(source, io.BytesIO):
        fd, temp_path = tempfile.mkstemp(prefix="fx_publish_video_", suffix=".mp4")
        os.close(fd)
        source.seek(0)
        with open(temp_path, "wb") as f:
            shutil.copyfileobj(source, f)
        source.seek(0)
        if Path(temp_path).stat().st_size > 0:
            temp_paths.append(Path(temp_path))
            return temp_path
        Path(temp_path).unlink(missing_ok=True)
    return ""


def _debug_value(value):
    if value is None:
        return "None"
    parts = [f"type={type(value)!r}", f"repr={repr(value)[:500]}"]
    if isinstance(value, dict):
        parts.append(f"keys={list(value.keys())}")
    else:
        attrs = []
        for name in dir(value):
            if name.startswith("_"):
                continue
            try:
                item = getattr(value, name)
            except Exception:
                continue
            if isinstance(item, (str, int, float, bool, Path)):
                attrs.append(f"{name}={item!r}")
            if len(attrs) >= 20:
                break
        if attrs:
            parts.append("attrs=" + ", ".join(attrs))
    return " | ".join(parts)


def _choose_path(path_value, object_value, temp_paths, *, is_image=False):
    direct = str(path_value or "").strip()
    if direct:
        return direct
    if object_value is None:
        return ""
    if is_image:
        return _save_image_tensor(object_value, temp_paths)
    return _save_video_to_temp(object_value, temp_paths)


async def _publish_async(platform, video, video_path, title, content, tags, cover, cover_path,
                         cookies_path, headed, keep_open_seconds, no_publish,
                         uploader_attrs=None):
    temp_paths = []
    uploader = None
    try:
        resolved_video = _choose_path(video_path, video, temp_paths)
        video_file = Path(str(resolved_video)).expanduser()
        if not video_file.is_file():
            raise ValueError(f"视频文件不存在: {video_file}")

        resolved_cover = _choose_path(cover_path, cover, temp_paths, is_image=True)
        if resolved_cover and not Path(resolved_cover).expanduser().is_file():
            raise ValueError(f"封面文件不存在: {resolved_cover}")

        if headed == "yes":
            os.environ["SPREADO_KEEP_BROWSER_OPEN_SECONDS"] = str(int(keep_open_seconds))

        cookie_path = str(cookies_path).strip() or None
        uploader = _get_uploader_cls(platform)(
            cookie_file_path=Path(cookie_path).expanduser() if cookie_path else None,
            headless=headed != "yes",
        )
        uploader._skip_publish = no_publish == "yes"
        # 平台特有参数（如 B站分区/创作声明）以属性形式注入 uploader
        for key, value in (uploader_attrs or {}).items():
            setattr(uploader, key, value)

        ok = await uploader.upload_video_flow(
            file_path=video_file.resolve(),
            title=str(title),
            content=str(content),
            tags=_split_tags(tags),
            thumbnail_path=Path(resolved_cover).expanduser().resolve() if resolved_cover else None,
            auto_login=True,
        )
        if not ok:
            raise RuntimeError(f"{platform} 发布失败")
        return {
            "status": "success",
            "run_id": uploader.run_id,
            "debug_dir": str(uploader.debug_dir),
        }
    except Exception as exc:
        if uploader is not None:
            raise RuntimeError(
                f"{exc} | run_id={uploader.run_id} | debug_dir={uploader.debug_dir}"
            ) from exc
        raise
    finally:
        for path in temp_paths:
            try:
                Path(path).unlink(missing_ok=True)
            except Exception:
                pass


def _publish(platform, video, video_path, title, content, tags, cover, cover_path, cookies_path,
             headed, keep_open_seconds, no_publish, uploader_attrs=None):
    result = _run_async(_publish_async(
        platform,
        video,
        video_path,
        title,
        content,
        tags,
        cover,
        cover_path,
        cookies_path,
        headed,
        keep_open_seconds,
        no_publish,
        uploader_attrs=uploader_attrs,
    ))
    return {"result": (result["status"], result["run_id"], result["debug_dir"])}


class _BasePublishNode:
    PLATFORM = ""

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "title": ("STRING", {"default": "", "multiline": False}),
                "content": ("STRING", {"default": "", "multiline": True, "dynamicPrompts": False}),
                "tags": ("STRING", {"default": "", "multiline": False}),
                "browser_mode": (
                    ["复用我的Chrome", "新开浏览器"],
                    {
                        "default": "复用我的Chrome",
                        "tooltip": "复用我的Chrome=附加到你已开着的浏览器，直接用里面现成的登录态，"
                                   "不再新开窗口、不用扫码。前提是 Chrome 以调试端口启动过；"
                                   "没开且 Chrome 当前没运行时会自动带端口拉起。",
                    },
                ),
                "headed": (["yes", "no"], {"default": "yes"}),
                "keep_open_seconds": ("INT", {"default": 60, "min": 0, "max": 3600}),
                "no_publish": (["no", "yes"], {"default": "no"}),
            },
            "optional": {
                "cdp_port": ("INT", {"default": 9222, "min": 1024, "max": 65535,
                                     "tooltip": "browser_mode=复用我的Chrome 时用的调试端口"}),
                "video": ("VIDEO",),
                "video_path": ("STRING", {"default": "", "multiline": False}),
                "cover": ("IMAGE",),
                "cover_path": ("STRING", {"default": "", "multiline": False}),
                "cookies_path": ("STRING", {"default": "", "multiline": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING", "STRING")
    RETURN_NAMES = ("status", "run_id", "debug_dir")
    FUNCTION = "run"
    CATEGORY = "FX Publish"
    OUTPUT_NODE = True

    def run(self, title, content, tags, browser_mode, headed, keep_open_seconds,
            no_publish, cdp_port=9222, video=None, video_path="", cover=None,
            cover_path="", cookies_path=""):
        return _publish(
            self.PLATFORM,
            video,
            video_path,
            title,
            content,
            tags,
            cover,
            cover_path,
            cookies_path,
            headed,
            keep_open_seconds,
            no_publish,
            uploader_attrs=_browser_attrs(browser_mode, cdp_port),
        )


class FXPublishDouyin(_BasePublishNode):
    PLATFORM = "douyin"


class FXPublishXiaohongshu(_BasePublishNode):
    PLATFORM = "xiaohongshu"


class FXPublishKuaishou(_BasePublishNode):
    PLATFORM = "kuaishou"


class FXPublishShipinhao(_BasePublishNode):
    PLATFORM = "shipinhao"


class FXPublishBilibili(_BasePublishNode):
    PLATFORM = "bilibili"

    # B站创作声明的可选项（投稿页下拉的完整列表）
    STATEMENT_OPTIONS = [
        "含AI生成内容",
        "内容无需标注",
        "含虚构演绎内容",
        "内容含营销信息",
        "个人观点，仅供参考",
        "内容为转载",
    ]

    @classmethod
    def INPUT_TYPES(cls):
        types = super().INPUT_TYPES()
        types["optional"]["partition"] = (
            "STRING",
            {
                "default": "",
                "multiline": False,
                "tooltip": "B站分区名称（如 vlog/游戏/娱乐），留空则使用账号记忆的默认分区",
            },
        )
        types["optional"]["statement"] = (
            cls.STATEMENT_OPTIONS,
            {"default": "含AI生成内容", "tooltip": "创作声明（B站必填项）"},
        )
        return types

    def run(self, title, content, tags, browser_mode, headed, keep_open_seconds,
            no_publish, cdp_port=9222, video=None, video_path="", cover=None,
            cover_path="", cookies_path="", partition="", statement="含AI生成内容"):
        return _publish(
            self.PLATFORM,
            video,
            video_path,
            title,
            content,
            tags,
            cover,
            cover_path,
            cookies_path,
            headed,
            keep_open_seconds,
            no_publish,
            uploader_attrs={
                "partition_pref": str(partition or "").strip(),
                "statement_pref": str(statement or "").strip(),
                **_browser_attrs(browser_mode, cdp_port),
            },
        )


NODE_CLASS_MAPPINGS = {
    "publish_douyin": FXPublishDouyin,
    "publish_xiaohongshu": FXPublishXiaohongshu,
    "publish_kuaishou": FXPublishKuaishou,
    "publish_shipinhao": FXPublishShipinhao,
    "publish_bilibili": FXPublishBilibili,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "publish_douyin": "发布抖音",
    "publish_xiaohongshu": "发布小红书",
    "publish_kuaishou": "发布快手",
    "publish_shipinhao": "发布视频号",
    "publish_bilibili": "发布B站",
}
