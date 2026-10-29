// fx_video 前端：按 cookie 来源 + 高级选项动态显隐输入桩点。
// 与 fx_crawler.js 同套路（改 type + computeSize），只作用于 fx_video_ 前缀的节点。
import { app } from "../../scripts/app.js";

const PREFIX = "fx_video_";
const COOKIE_PREFIX = "fx_browser_";

function showWidget(w, show) {
  if (!w) return;
  if (w.__fxOrigType === undefined) {
    w.__fxOrigType = w.type;
    w.__fxOrigCompute = w.computeSize;
  }
  if (show) {
    w.type = w.__fxOrigType;
    w.computeSize = w.__fxOrigCompute;
  } else {
    w.type = "hidden";
    w.computeSize = () => [0, -4];
  }
  if (w.element) w.element.style.display = show ? "" : "none";
}

function get(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

// 「高级选项」控制的项（probe 节点没有该开关，则这些项常显）
const ADVANCED = ["代理", "合集第几个", "缓存目录"];

function refresh(node) {
  const advWidget = get(node, "高级选项");
  const adv = advWidget ? !!advWidget.value : true;
  const src = get(node, "cookie来源")?.value || "不用";

  // cookie 相关：只显示当前来源真正要填的那个
  showWidget(get(node, "cookie来源"), adv);
  showWidget(get(node, "cookie"), adv && src === "cookie文本");
  showWidget(get(node, "cookies文件"), adv && src === "cookies.txt文件");

  for (const name of ADVANCED) showWidget(get(node, name), adv);

  const sz = node.computeSize();
  node.setSize([Math.max(node.size[0], sz[0]), sz[1]]);
  node.setDirtyCanvas?.(true, true);
}

function refreshCookieNode(node) {
  const src = get(node, "来源")?.value || "本机Chrome(推荐)";
  showWidget(get(node, "CDP端口"), String(src).includes("连我的Chrome"));
  showWidget(get(node, "缓存目录"), true);
  const sz = node.computeSize();
  node.setSize([Math.max(node.size[0], sz[0]), sz[1]]);
  node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "fx_crawler.browser_cookie_inputs",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData?.name?.startsWith(COOKIE_PREFIX)) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      const node = this;
      const w = get(node, "来源");
      if (w) {
        const prev = w.callback;
        w.callback = function () {
          const ret = prev?.apply(this, arguments);
          refreshCookieNode(node);
          return ret;
        };
      }
      setTimeout(() => refreshCookieNode(node), 0);
      return r;
    };
  },
});

app.registerExtension({
  name: "fx_crawler.video_inputs",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData?.name?.startsWith(PREFIX)) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      const node = this;
      for (const name of ["cookie来源", "高级选项"]) {
        const w = get(node, name);
        if (!w) continue;
        const prev = w.callback;
        w.callback = function () {
          const ret = prev?.apply(this, arguments);
          refresh(node);
          return ret;
        };
      }
      setTimeout(() => refresh(node), 0);
      return r;
    };

    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      setTimeout(() => refresh(this), 0);
      return r;
    };
  },
});
