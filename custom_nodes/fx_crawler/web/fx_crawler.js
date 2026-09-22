// fx_crawler 前端：按"采集模式"场景 + "高级选项"动态显隐输入桩点。
// 仿 fxllm 的"选场景→渲染适配桩点"。纯前端显隐，值仍会被序列化传给后端。
import { app } from "../../scripts/app.js";

const PREFIX = "fx_crawl_";

// —— 稳健的 widget 显隐（跨前端版本）：改 type + computeSize，值照常保留 ——
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
    w.computeSize = () => [0, -4]; // 折叠掉高度
  }
  // 关联的多行输入元素(textarea)也一起显隐
  if (w.element) w.element.style.display = show ? "" : "none";
}

function get(node, name) {
  return (node.widgets || []).find((w) => w.name === name);
}

// 平台专属 widget 名(只在对应平台节点上存在)
const PLATFORM_WIDGETS = ["排序方式", "搜索类型", "限定吧名"];
const ADVANCED = ["登录方式", "浏览器模式", "无头运行", "保存格式", "cookie", "输出目录"];

function refresh(node) {
  const mode = get(node, "采集模式")?.value || "关键词搜索";
  const adv = !!get(node, "高级选项")?.value;
  const isSearch = mode === "关键词搜索";
  const isDetail = mode === "指定链接";
  const isCreator = mode === "指定用户";

  showWidget(get(node, "关键词"), isSearch);
  showWidget(get(node, "链接或ID"), isDetail);
  showWidget(get(node, "用户主页"), isCreator);
  // 条数上限：详情模式用不到(按给定列表抓)
  showWidget(get(node, "内容条数上限"), !isDetail);

  // 平台专属项：仅搜索模式显示
  for (const name of PLATFORM_WIDGETS) showWidget(get(node, name), isSearch);

  // 高级项：跟随"高级选项"
  for (const name of ADVANCED) showWidget(get(node, name), adv);

  // 重新计算节点尺寸
  const sz = node.computeSize();
  node.setSize([Math.max(node.size[0], sz[0]), sz[1]]);
  node.setDirtyCanvas?.(true, true);
}

app.registerExtension({
  name: "fx_crawler.dynamic_inputs",
  beforeRegisterNodeDef(nodeType, nodeData) {
    if (!nodeData?.name?.startsWith(PREFIX)) return;

    const onCreated = nodeType.prototype.onNodeCreated;
    nodeType.prototype.onNodeCreated = function () {
      const r = onCreated?.apply(this, arguments);
      const node = this;
      // 给"采集模式""高级选项"挂回调
      for (const name of ["采集模式", "高级选项"]) {
        const w = get(node, name);
        if (!w) continue;
        const prev = w.callback;
        w.callback = function () {
          const ret = prev?.apply(this, arguments);
          refresh(node);
          return ret;
        };
      }
      // 首次渲染
      setTimeout(() => refresh(node), 0);
      return r;
    };

    // 载入已保存的工作流时也按存下的模式恢复显隐
    const onConfigure = nodeType.prototype.onConfigure;
    nodeType.prototype.onConfigure = function () {
      const r = onConfigure?.apply(this, arguments);
      setTimeout(() => refresh(this), 0);
      return r;
    };
  },
});
